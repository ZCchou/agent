"""Apply managed-process leases to the existing local HTTP provider transport."""
from contextlib import asynccontextmanager
import asyncio
import logging
import re
from urllib.parse import urlsplit

import httpx

from agent.providers import (ModelProvider, ProviderError, ProviderProfile, closing_provider_stream,
                             _provider_messages, _external_tool_definitions, _map_http_error)
from agent.infrastructure.logging import log_event


_TOKEN_COUNT_TIMEOUT = 3.0


def _preflight_diagnostic(prepared, **fields):
    roles = [message.get('role') if message.get('role') in {
        'system', 'developer', 'user', 'assistant', 'tool', 'function',
    } else 'unknown' for message in prepared.messages]
    level = logging.DEBUG
    if 'error_code' in fields:
        level = logging.WARNING
    elif 'fallback_reason' in fields:
        level = logging.INFO
    log_event(logging.getLogger('agent.core'), level, 'model_template_preflight',
              'Managed local template preflight completed',
              spec_id=getattr(prepared, 'spec_id', None), protocol_id=prepared.protocol_id,
              model_id=prepared.model_id, message_roles=roles, **fields)


async def _next_managed_event(stream):
    # Shield the read so a second cancellation cannot interrupt its transport
    # finally block after the first cancellation has started cleanup.
    read = asyncio.ensure_future(anext(stream))
    try:
        return await asyncio.shield(read)
    except asyncio.CancelledError:
        read.cancel()
        while not read.done():
            try:
                await asyncio.shield(read)
            except asyncio.CancelledError:
                pass
        try:
            read.result()
        except BaseException:
            pass
        raise


async def guarded_managed_events(stream):
    async with closing_provider_stream(stream):
        while True:
            try:
                event = await _next_managed_event(stream)
            except StopAsyncIteration:
                break
            yield event


class _LeasedLocalProvider(ModelProvider):
    """Keep the captured profile while using an already-reserved model slot."""
    def __init__(self, profile, inner, measure_request):
        super().__init__(profile)
        self.inner = inner
        self.measure_request = measure_request

    def stream_events(self, model_id, messages, *, tools, options=None):
        return guarded_managed_events(
            self.inner.stream_events(model_id, messages, tools=tools, options=options).__aiter__(),
        )


class ManagedLocalProvider(ModelProvider):
    def __init__(self, profile: ProviderProfile, inner: ModelProvider, models):
        super().__init__(profile)
        self.inner = inner
        self.models = models

    @asynccontextmanager
    async def _session(self):
        async with self.models.invocation(self.profile.id) as current:
            if (current.base_url, current.api_key) != (self.profile.base_url, self.profile.api_key):
                raise ProviderError("managed_session_changed", "本地模型会话已变化，请重新提交当前请求。")
            yield self.inner

    @asynccontextmanager
    async def request_session(self):
        async with self._session() as inner:
            yield _LeasedLocalProvider(self.profile, inner, self._measure_request)

    @asynccontextmanager
    async def tokenizer_session(self):
        """Count a pinned engine's input without reserving its generation slot."""
        runtime = self.models._runtime
        session = runtime.session_id
        def check_session():
            profile = self.models.get_profile(self.profile.id)
            if (self.models._runtime is not runtime or runtime.session_id != session
                    or (profile.base_url, profile.api_key) != (self.profile.base_url, self.profile.api_key)):
                raise ProviderError("managed_session_changed", "分词期间本地模型会话已变化。")
        check_session()
        yield _LeasedLocalProvider(self.profile, self.inner, self._measure_request)
        check_session()

    async def measure_request(self, prepared):
        async with self._session():
            return await self._measure_request(prepared)

    async def _measure_request(self, prepared):
        runtime = getattr(self.models, '_runtime', None)
        if not getattr(runtime, 'supports_token_counting', False) or prepared.protocol_id != 'openai_chat':
            _preflight_diagnostic(prepared, fallback_reason='counting_unavailable')
            return None
        endpoint = urlsplit(self.profile.base_url)
        if not self.profile.local_strict or endpoint.scheme != 'http' or endpoint.hostname not in {'127.0.0.1', '::1'}:
            _preflight_diagnostic(prepared, fallback_reason='unsupported_endpoint')
            return None
        if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment or endpoint.path.rstrip('/') != '/v1':
            _preflight_diagnostic(prepared, fallback_reason='unsupported_endpoint')
            return None
        for message in prepared.messages:
            content = message.get('content')
            if content is None or isinstance(content, str):
                continue
            if not isinstance(content, list) or any(not isinstance(part, dict) or part.get('type') != 'text'
                                                    or not isinstance(part.get('text'), str) for part in content):
                _preflight_diagnostic(prepared, fallback_reason='non_text_content')
                return None
        body = {'model': prepared.model_id, 'messages': _provider_messages(list(prepared.messages)),
                'add_generation_prompt': True}
        if prepared.tools_enabled:
            body['tools'] = _external_tool_definitions(prepared.options.allowed_tools)
        if prepared.options.response_format == 'json_object':
            body['response_format'] = {'type': 'json_object'}
        elif prepared.options.response_format == 'json_schema' and prepared.options.response_schema:
            body['response_format'] = {'type': 'json_schema', 'json_schema': {
                'name': 'conversation_checkpoint' if prepared.options.purpose == 'context_compaction' else 'rag_answer',
                'strict': True, 'schema': prepared.options.response_schema}}
        base_url = self.profile.base_url.rstrip('/').removesuffix('/v1')
        phase = 'apply_template'
        status = None
        try:
            async with asyncio.timeout(_TOKEN_COUNT_TIMEOUT):
                async with httpx.AsyncClient(timeout=_TOKEN_COUNT_TIMEOUT, trust_env=False,
                        follow_redirects=False, headers={'Authorization': 'Bearer ' + self.profile.api_key}) as client:
                    # Locked b12110 uses the inference parser and server thinking
                    # defaults here, then counts text prompts with both flags true.
                    rendered = await client.post(base_url + '/apply-template', json=body)
                    status = rendered.status_code
                    rendered.raise_for_status()
                    value = rendered.json()
                    if not isinstance(value, dict) or not isinstance(value.get('prompt'), str):
                        _preflight_diagnostic(prepared, phase=phase, http_status=status,
                                              fallback_reason='malformed_template_response')
                        return None
                    phase = 'tokenize'
                    status = None
                    tokenized = await client.post(base_url + '/tokenize', json={
                        'content': value['prompt'], 'add_special': True, 'parse_special': True})
                    status = tokenized.status_code
                    tokenized.raise_for_status()
                    value = tokenized.json()
                    tokens = value.get('tokens') if isinstance(value, dict) else None
                    if isinstance(tokens, list) and all(type(token) is int and token >= 0 for token in tokens):
                        _preflight_diagnostic(prepared, phase=phase, http_status=status,
                                              token_count=len(tokens), outcome='counted')
                        return len(tokens)
                    _preflight_diagnostic(prepared, phase=phase, http_status=status,
                                          fallback_reason='malformed_tokenizer_response')
        except httpx.HTTPStatusError as error:
            mapped = _map_http_error(error.response)
            lowered = error.response.text.lower()
            missing_model = error.response.status_code == 404 and (
                any(marker in lowered for marker in ('model_not_found', 'unknown model', 'no such model'))
                or re.search(r'\bmodel\b[^\r\n]{0,160}\b(?:not found|does not exist|not loaded)\b', lowered)
            )
            if mapped.code in {'model_not_found', 'invalid_provider_response', 'provider_unavailable'} \
                    and error.response.status_code in {404, 405, 501} and not missing_model:
                _preflight_diagnostic(prepared, phase=phase, http_status=error.response.status_code,
                                      fallback_reason='endpoint_unsupported')
                return None
            mapped.details.update(http_status=error.response.status_code, phase=phase)
            _preflight_diagnostic(prepared, error_code=mapped.code, **mapped.details)
            raise mapped from None
        except (httpx.TimeoutException, TimeoutError):
            _preflight_diagnostic(prepared, phase=phase, http_status=status, fallback_reason='count_timeout')
        except httpx.RequestError:
            _preflight_diagnostic(prepared, phase=phase, http_status=status, error_code='network_unavailable')
            raise ProviderError('network_unavailable', 'The local model service is unavailable.',
                                details={'phase': phase}) from None
        except (ValueError, TypeError):
            _preflight_diagnostic(prepared, phase=phase, http_status=status, fallback_reason='malformed_response')
        return None

    async def list_models(self):
        async with self._session() as provider:
            return await provider.list_models()

    async def discover_models(self):
        async with self._session() as provider:
            if hasattr(provider, "discover_models"):
                return await provider.discover_models()
            return await provider.list_models(), self.profile.base_url

    async def stream(self, model_id, messages):
        async with self._session() as provider:
            async with closing_provider_stream(provider.stream(model_id, messages).__aiter__()) as stream:
                while True:
                    try:
                        text = await _next_managed_event(stream)
                    except StopAsyncIteration:
                        break
                    yield text

    async def stream_events(self, model_id, messages, *, tools, options=None):
        async with self._session() as provider:
            events = guarded_managed_events(
                provider.stream_events(model_id, messages, tools=tools, options=options).__aiter__(),
            )
            async with closing_provider_stream(events) as stream:
                async for event in stream:
                    yield event
