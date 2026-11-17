import asyncio
import json
import logging
from types import SimpleNamespace

import httpx
import pytest

from agent.execution.retry import should_retry_transient_generation
from agent.local_provider import ManagedLocalProvider
from agent.providers import ProviderError, _map_http_error
from test_managed_provider import Inner, Sessions, profile
from test_managed_token_counting import install_transport, prepared


@pytest.mark.parametrize('status', [400, 422, 500])
@pytest.mark.parametrize('message,category', [
    ('System message must be at the beginning', 'system_message_order'),
    ('Conversation roles must alternate user/assistant', 'conversation_role_order'),
    ('jinja2.exceptions.TemplateError: private prompt secret', 'jinja_template_error'),
    ('Failed to render chat template: private prompt secret', 'chat_template_render'),
    ('Failed to render the chat template: private prompt secret', 'chat_template_render'),
    ('Jinja template error: private prompt secret', 'jinja_template_error'),
])
def test_explicit_template_errors_have_safe_deterministic_classification(status, message, category):
    error = _map_http_error(httpx.Response(status, json={'error': {'message': message}}))
    assert error.code == 'model_template_render_failed'
    assert error.details == {'http_status': status, 'phase': 'provider_response', 'template_error': category}
    assert 'private' not in str(error) + json.dumps(error.details)


@pytest.mark.parametrize('status,message,code', [
    (500, 'unknown internal error', 'provider_unavailable'),
    (500, 'out of memory rendering chat template', 'resource_exhausted'),
    (401, 'System message must be at the beginning', 'authentication_failed'),
    (500, 'template settings were supplied', 'provider_unavailable'),
])
def test_unrelated_errors_keep_existing_classification(status, message, code):
    assert _map_http_error(httpx.Response(status, text=message)).code == code


@pytest.mark.parametrize('status,body,code', [
    (400, 'System message must be at the beginning private-secret', 'model_template_render_failed'),
    (422, 'Failed to render chat template private-secret', 'model_template_render_failed'),
    (500, 'jinja2.exceptions.TemplateError private-secret', 'model_template_render_failed'),
    (401, 'private-secret', 'authentication_failed'),
    (503, 'private-secret', 'provider_unavailable'),
    (500, 'out of memory private-secret', 'resource_exhausted'),
    (501, 'out of memory private-secret', 'resource_exhausted'),
    (404, 'model not found private-secret', 'model_not_found'),
    (404, '{"error":{"code":"model_not_found"}}', 'model_not_found'),
])
def test_template_preflight_errors_propagate_and_log_safe_structure(monkeypatch, caplog, status, body, code):
    async def scenario():
        sessions = Sessions()
        sessions._runtime = SimpleNamespace(supports_token_counting=True)
        _, requests = install_transport(monkeypatch, sessions, lambda request: httpx.Response(status, text=body))
        request = prepared(messages=[{'role': 'system', 'content': 'private-secret'},
                                     {'role': 'user', 'content': 'private-secret'}])
        request.spec_id = 'spec-1'
        with caplog.at_level(logging.DEBUG, logger='agent.core'):
            with pytest.raises(ProviderError) as caught:
                await ManagedLocalProvider(profile(), Inner(), sessions).measure_request(request)
        assert caught.value.code == code
        assert caught.value.details['phase'] == 'apply_template'
        assert len(requests) == 1 and sessions.active == 0
        records = [record for record in caplog.records if record.event == 'model_template_preflight']
        assert len(records) == 1
        assert records[0].fields['spec_id'] == 'spec-1'
        assert records[0].fields['message_roles'] == ['system', 'user']
        assert records[0].fields['http_status'] == status
        assert records[0].levelno == logging.WARNING
        assert 'private-secret' not in json.dumps(records[0].fields)
    asyncio.run(scenario())


@pytest.mark.parametrize('status', [404, 405, 501])
def test_unsupported_template_endpoint_falls_back_with_safe_reason(monkeypatch, caplog, status):
    async def scenario():
        sessions = Sessions()
        sessions._runtime = SimpleNamespace(supports_token_counting=True)
        _, requests = install_transport(monkeypatch, sessions, lambda request: httpx.Response(status))
        with caplog.at_level(logging.DEBUG, logger='agent.core'):
            assert await ManagedLocalProvider(profile(), Inner(), sessions).measure_request(prepared()) is None
        assert len(requests) == 1
        record = next(record for record in caplog.records if record.event == 'model_template_preflight')
        assert record.fields['fallback_reason'] == 'endpoint_unsupported'
        assert record.fields['http_status'] == status
        assert record.levelno == logging.INFO
    asyncio.run(scenario())


@pytest.mark.parametrize('code', ['model_template_render_failed', 'model_message_order_invalid'])
def test_deterministic_template_errors_never_enter_transient_replay(code):
    assert not should_retry_transient_generation(error_code=code, produced_output=False,
                                                tool_names=[], retry_count=0)


@pytest.mark.parametrize('code', ['model_template_render_failed', 'model_message_order_invalid'])
def test_deterministic_template_errors_skip_failure_explanation(code):
    from agent.execution.run_recovery import recovery_allowed, safe_code
    error = ProviderError(code, 'safe summary')
    assert safe_code(error) == code
    assert not recovery_allowed(error, partial='partial result', operations=[])


def test_preflight_connection_failure_preserves_network_error(monkeypatch):
    async def scenario():
        sessions = Sessions()
        sessions._runtime = SimpleNamespace(supports_token_counting=True)
        def respond(request):
            raise httpx.ConnectError('private-secret', request=request)
        install_transport(monkeypatch, sessions, respond)
        with pytest.raises(ProviderError) as caught:
            await ManagedLocalProvider(profile(), Inner(), sessions).measure_request(prepared())
        assert caught.value.code == 'network_unavailable'
        assert caught.value.details == {'phase': 'apply_template'}
        assert 'private-secret' not in str(caught.value)
        assert sessions.active == 0
    asyncio.run(scenario())


def test_tokenizer_service_failure_is_not_a_count_fallback(monkeypatch):
    async def scenario():
        sessions = Sessions()
        sessions._runtime = SimpleNamespace(supports_token_counting=True)
        def respond(request):
            if request.url.path == '/apply-template':
                return httpx.Response(200, json={'prompt': 'private-secret'})
            return httpx.Response(503, text='private-secret')
        _, requests = install_transport(monkeypatch, sessions, respond)
        with pytest.raises(ProviderError) as caught:
            await ManagedLocalProvider(profile(), Inner(), sessions).measure_request(prepared())
        assert caught.value.code == 'provider_unavailable'
        assert caught.value.details == {'http_status': 503, 'phase': 'tokenize'}
        assert len(requests) == 2 and sessions.active == 0
    asyncio.run(scenario())


def test_template_error_in_tool_probe_stays_incomplete_without_retry_or_route_fallback(monkeypatch, tmp_path):
    from agent.providers import OpenAICompatibleProvider
    from agent.runtime import ChatRunService
    from agent.storage import CoreRepository
    from agent.tool_verification import ToolVerification
    async def scenario():
        original = httpx.AsyncClient
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(500, json={'error': {'message': 'System message must be at the beginning'}})
        monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(
            transport=httpx.MockTransport(respond), **kwargs))
        current = profile()
        service = SimpleNamespace(_profile=lambda _: current,
                                  _provider=lambda _: OpenAICompatibleProvider(current))
        verification = ToolVerification(CoreRepository(tmp_path / 'probe.db'))
        with pytest.raises(ProviderError) as caught:
            await verification.ensure('identity', lambda: ChatRunService._probe_tool_capability(
                service, current.id, 'model'))
        assert caught.value.code == 'model_template_render_failed'
        assert len(requests) == 1
        saved = verification.get('identity')
        assert saved['status'] == 'incomplete' and saved['verified'] is False
        assert saved['error']['code'] == 'model_template_render_failed'
    asyncio.run(scenario())


def test_managed_template_preflight_failure_blocks_runtime_dispatch(monkeypatch):
    from agent.runtime import ChatRunService
    async def scenario():
        identity = {'backend': 'cpu', 'context_length': 1000, 'session_id': 'session'}
        sessions = Sessions()
        sessions._runtime = SimpleNamespace(supports_token_counting=True, alive=True)
        sessions.runtime_identity = lambda _: identity
        sessions.get_profile = lambda _: profile()
        inner = Inner()
        provider = ManagedLocalProvider(profile(), inner, sessions)
        _, requests = install_transport(monkeypatch, sessions, lambda request: httpx.Response(
            500, text='System message must be at the beginning'))
        dispatched = []
        service = SimpleNamespace(local_models=sessions, _provider=lambda _: provider,
            _validate_context_dispatch_authority=lambda *args, **kwargs: None,
            execution_repository=SimpleNamespace(mark_invocation_dispatched=lambda *args: dispatched.append(args)),
            _model_diagnostic=lambda _: None,
            _managed_transport_failure=ChatRunService._managed_transport_failure)
        request = prepared()
        request.spec_id = 'spec'
        request.input_budget = {'input_hard_limit': 1000}
        spec = SimpleNamespace(managed_runtime=identity, model_id='model')

        async def monitored(factory, **kwargs):
            async for event in factory():
                yield event

        monkeypatch.setattr('agent.runtime.monitored_events', monitored)
        with pytest.raises(ProviderError) as caught:
            _ = [event async for event in ChatRunService._recovering_local_events(service,
                {'id': 'run'}, 'attempt', provider, None, spec, request, list(request.messages),
                update_spec=lambda *args: None, safe_to_retry=True)]
        assert caught.value.code == 'model_template_render_failed'
        assert dispatched == [] and inner.calls == 0
        assert len(requests) == 1 and sessions.active == 0
    asyncio.run(scenario())
