import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from agent.local_provider import ManagedLocalProvider
from agent.providers import GenerationOptions, ProviderError
from test_managed_provider import Inner, Sessions, profile


def prepared(*, messages=None, tools=False):
    return SimpleNamespace(model_id='model', protocol_id='openai_chat',
        messages=tuple(messages or [{'role': 'user', 'content': 'hello'}]),
        tools_enabled=tools, options=GenerationOptions(allowed_tools=('fs.read',)))


def install_transport(monkeypatch, sessions, respond):
    original = httpx.AsyncClient
    settings = []
    requests = []

    def handler(request):
        assert sessions.active == 1
        assert request.headers['Authorization'] == 'Bearer session-secret'
        assert request.url.host == '127.0.0.1'
        requests.append(request)
        return respond(request)

    def client(**kwargs):
        settings.append(kwargs)
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, 'AsyncClient', client)
    return settings, requests


@pytest.mark.parametrize('leased', [False, True])
def test_exact_local_count_uses_template_tokenizer_and_one_session(monkeypatch, leased):
    async def scenario():
        sessions = Sessions()
        sessions._runtime = SimpleNamespace(supports_token_counting=True)

        def respond(request):
            body = json.loads(request.content)
            if request.url.path == '/apply-template':
                assert body['messages'][0]['tool_calls'][0]['function']['name'] == 'fs_read'
                assert body['tools'][0]['function']['name'] == 'fs_read'
                assert body['add_generation_prompt'] is True
                return httpx.Response(200, json={'prompt': '<template>hello<assistant>'})
            assert request.url.path == '/tokenize'
            assert body == {'content': '<template>hello<assistant>', 'add_special': True, 'parse_special': True}
            return httpx.Response(200, json={'tokens': [1, 2, 3, 4]})

        settings, requests = install_transport(monkeypatch, sessions, respond)
        provider = ManagedLocalProvider(profile(), Inner(), sessions)
        request = prepared(messages=[{'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': 'call', 'type': 'function', 'function': {'name': 'fs.read', 'arguments': '{"path":"a"}'}}]},
            {'role': 'tool', 'tool_call_id': 'call', 'content': 'hello'}], tools=True)
        if leased:
            async with provider.request_session() as active:
                assert await active.measure_request(request) == 4
                assert sessions.active == 1
        else:
            assert await provider.measure_request(request) == 4
        assert len(requests) == 2
        assert settings[0]['trust_env'] is False
        assert settings[0]['follow_redirects'] is False
        assert sessions.maximum == 1 and sessions.active == 0

    asyncio.run(scenario())


@pytest.mark.parametrize('case', ['unsupported', 'image', 'remote', 'protocol'])
def test_inapplicable_count_makes_no_http_requests(monkeypatch, case):
    async def scenario():
        sessions = Sessions()
        sessions._runtime = SimpleNamespace(supports_token_counting=case != 'unsupported')
        request = prepared()
        current = profile()
        if case == 'image':
            request.messages = ({'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,a'}}]},)
        if case == 'remote':
            current = replace(current, base_url='https://example.com/v1')
            sessions.current = current
        if case == 'protocol':
            request.protocol_id = 'openai_responses'

        def forbidden(**kwargs):
            raise AssertionError('Token measurement must stay on the supported local text route')

        monkeypatch.setattr(httpx, 'AsyncClient', forbidden)
        assert await ManagedLocalProvider(current, Inner(), sessions).measure_request(request) is None
        assert sessions.active == 0

    asyncio.run(scenario())


@pytest.mark.parametrize('response', [httpx.Response(404), httpx.Response(200, json={}),
    httpx.Response(200, json={'tokens': [True]}), httpx.Response(200, json={'tokens': 'bad'})])
def test_unavailable_or_malformed_tokenizer_falls_back(monkeypatch, response):
    async def scenario():
        sessions = Sessions()
        sessions._runtime = SimpleNamespace(supports_token_counting=True)
        def respond(request):
            return httpx.Response(200, json={'prompt': 'hello'}) if request.url.path == '/apply-template' else response
        install_transport(monkeypatch, sessions, respond)
        assert await ManagedLocalProvider(profile(), Inner(), sessions).measure_request(prepared()) is None
        assert sessions.active == 0
    asyncio.run(scenario())


def test_measurement_rejects_changed_session_without_sending_prompt(monkeypatch):
    async def scenario():
        sessions = Sessions()
        sessions._runtime = SimpleNamespace(supports_token_counting=True)
        provider = ManagedLocalProvider(profile(), Inner(), sessions)
        sessions.current = replace(sessions.current, api_key='new-session')
        with pytest.raises(ProviderError, match='会话'):
            await provider.measure_request(prepared())
        assert sessions.active == 0
    asyncio.run(scenario())


@pytest.mark.parametrize('response_format', ['json_object', 'json_schema'])
def test_measurement_preserves_json_response_constraints_in_template(monkeypatch, response_format):
    async def scenario():
        sessions = Sessions()
        sessions._runtime = SimpleNamespace(supports_token_counting=True)
        request = prepared()
        request.options = GenerationOptions(response_format=response_format,
            response_schema={'type': 'object'}, purpose='context_compaction')
        expected = {'type': 'json_object'} if response_format == 'json_object' else {
            'type': 'json_schema', 'json_schema': {'name': 'conversation_checkpoint',
                'strict': True, 'schema': {'type': 'object'}}}
        def respond(http_request):
            if http_request.url.path == '/apply-template':
                body = json.loads(http_request.content)
                assert body['model'] == 'model'
                assert body['response_format'] == expected
                return httpx.Response(200, json={'prompt': 'constrained prompt'})
            return httpx.Response(200, json={'tokens': [1, 2]})
        install_transport(monkeypatch, sessions, respond)
        assert await ManagedLocalProvider(profile(), Inner(), sessions).measure_request(request) == 2
    asyncio.run(scenario())


def test_token_count_timeout_falls_back_and_releases_lease(monkeypatch):
    import agent.local_provider as local_provider
    monkeypatch.setattr(local_provider, '_TOKEN_COUNT_TIMEOUT', .01)

    async def scenario():
        sessions = Sessions()
        sessions._runtime = SimpleNamespace(supports_token_counting=True)
        async def respond(request):
            await asyncio.sleep(1)
            return httpx.Response(200, json={'prompt': 'late'})
        install_transport(monkeypatch, sessions, respond)
        assert await ManagedLocalProvider(profile(), Inner(), sessions).measure_request(prepared()) is None
        assert sessions.active == 0 and not sessions.lock.locked()
    asyncio.run(scenario())


def test_cancelling_token_count_preserves_cancellation_and_releases_lease(monkeypatch):
    async def scenario():
        sessions = Sessions()
        sessions._runtime = SimpleNamespace(supports_token_counting=True)
        entered = asyncio.Event()
        async def respond(request):
            entered.set()
            await asyncio.Event().wait()
        install_transport(monkeypatch, sessions, respond)
        task = asyncio.create_task(ManagedLocalProvider(profile(), Inner(), sessions).measure_request(prepared()))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert sessions.active == 0 and not sessions.lock.locked()
    asyncio.run(scenario())


@pytest.mark.parametrize('chat_template', [None, '{{ messages }}'])
def test_runtime_enables_token_count_only_with_readiness_template(tmp_path, monkeypatch, chat_template):
    from agent.local_models.runtime import ManagedRuntime
    from test_local_models_runtime import fake_runtime, finish, imported, service_at
    original = ManagedRuntime._readiness
    async def readiness(self, *args):
        props = await original(self, *args)
        if chat_template:
            props['chat_template'] = chat_template
        return props
    monkeypatch.setattr(ManagedRuntime, '_readiness', readiness)

    async def scenario():
        service = service_at(tmp_path, runtime_root=fake_runtime(tmp_path / 'runtime'))
        try:
            model = await imported(service, tmp_path / 'model.gguf')
            job = await finish(service, await service.handle('runtime.load', {'model_id': model['id']}))
            assert job['status'] == 'completed', job
            assert service._runtime.supports_token_counting is bool(chat_template)
            await service.handle('runtime.unload', {})
            assert service._runtime.supports_token_counting is False
        finally:
            await service.close()
    asyncio.run(scenario())
