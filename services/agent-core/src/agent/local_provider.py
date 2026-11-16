"""Apply managed-process leases to the existing local HTTP provider transport."""
from contextlib import asynccontextmanager
import asyncio
from urllib.parse import urlsplit

import httpx

from agent.providers import (ModelProvider, ProviderError, ProviderProfile, closing_provider_stream,
                             _provider_messages, _external_tool_definitions)


_TOKEN_COUNT_TIMEOUT = 3.0


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

    async def measure_request(self, prepared):
        async with self._session():
            return await self._measure_request(prepared)

    async def _measure_request(self, prepared):
        runtime = getattr(self.models, '_runtime', None)
        if not getattr(runtime, 'supports_token_counting', False) or prepared.protocol_id != 'openai_chat':
            return None
        endpoint = urlsplit(self.profile.base_url)
        if not self.profile.local_strict or endpoint.scheme != 'http' or endpoint.hostname not in {'127.0.0.1', '::1'}:
            return None
        if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment or endpoint.path.rstrip('/') != '/v1':
            return None
        for message in prepared.messages:
            content = message.get('content')
            if content is None or isinstance(content, str):
                continue
            if not isinstance(content, list) or any(not isinstance(part, dict) or part.get('type') != 'text'
                                                    or not isinstance(part.get('text'), str) for part in content):
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
        try:
            async with asyncio.timeout(_TOKEN_COUNT_TIMEOUT):
                async with httpx.AsyncClient(timeout=_TOKEN_COUNT_TIMEOUT, trust_env=False,
                        follow_redirects=False, headers={'Authorization': 'Bearer ' + self.profile.api_key}) as client:
                    # Locked b12110 uses the inference parser and server thinking
                    # defaults here, then counts text prompts with both flags true.
                    rendered = await client.post(base_url + '/apply-template', json=body)
                    rendered.raise_for_status()
                    value = rendered.json()
                    if not isinstance(value, dict) or not isinstance(value.get('prompt'), str):
                        return None
                    tokenized = await client.post(base_url + '/tokenize', json={
                        'content': value['prompt'], 'add_special': True, 'parse_special': True})
                    tokenized.raise_for_status()
                    value = tokenized.json()
                    tokens = value.get('tokens') if isinstance(value, dict) else None
                    if isinstance(tokens, list) and all(type(token) is int and token >= 0 for token in tokens):
                        return len(tokens)
        except (httpx.HTTPError, ValueError, TypeError, TimeoutError):
            pass
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
