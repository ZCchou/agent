import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent.execution.compiler import PreparedRequest
from agent.local_provider import ManagedLocalProvider
from agent.providers import GenerationOptions, ProviderEvent, ProviderProfile
from agent.runtime import ChatRunService
from agent.storage import CoreDataError


@pytest.mark.parametrize('remaining_tokens', [300, 1100])
def test_local_measurement_recompiles_before_dispatch_with_final_fingerprint(monkeypatch, remaining_tokens):
    async def scenario():
        identity = {'backend': 'cpu', 'context_length': 1000, 'session_id': 'session'}
        profile = ProviderProfile('managed:model', 'model', 'openai_compatible', 'http://127.0.0.1:1/v1')
        provider = ManagedLocalProvider(profile, None, None)
        measurements = iter([1100, remaining_tokens])
        measured_requests, dispatched, updates, compiled, snapshots = [], [], [], [], []

        async def measure(request):
            measured_requests.append(request)
            return next(measurements)

        ready = SimpleNamespace(profile=profile, measure_request=measure)

        @asynccontextmanager
        async def session(*args):
            yield ready

        async def monitored(factory, **kwargs):
            async for event in factory():
                yield event

        async def stream(active, specification, request):
            assert active is ready
            assert request.request_fingerprint == 'recompiled'
            yield ProviderEvent('text_delta', text='answer')

        async def emit(*args):
            pass

        messages = [{'role': 'user', 'content': 'Preserve the original task requirement.'}]
        for index in range(2):
            messages.extend([{'role': 'assistant', 'tool_calls': [{'id': str(index),
                'function': {'name': 'fs.read', 'arguments': '{"path":"a"}'}}]},
                {'role': 'tool', 'tool_call_id': str(index),
                 'content': json.dumps({'result_id': str(index), 'preview': 'body' * 1000})}])
        prepared = PreparedRequest(spec_id='spec', adapter_id='openai.chat', protocol_id='openai_chat',
            model_id='model', messages=tuple(messages), tools_enabled=False, tool_transport='none',
            tool_schema_hash='', options=GenerationOptions(), request_fingerprint='original',
            input_budget={'input_hard_limit': 1000})

        def compile_request(run_id, spec, values):
            compiled.append(values)
            return replace(prepared, messages=tuple(values), request_fingerprint='recompiled')

        service = SimpleNamespace(local_models=SimpleNamespace(_runtime=SimpleNamespace(alive=True),
            runtime_identity=lambda _: identity, get_profile=lambda _: profile, observe_activity=session),
            repository=SimpleNamespace(),
            _provider=lambda _: provider, _validate_context_dispatch_authority=lambda *args, **kwargs: None,
            _compile_model_request=compile_request,
            execution_repository=SimpleNamespace(mark_invocation_dispatched=lambda *args: dispatched.append(args)),
            context=SimpleNamespace(context_repository=SimpleNamespace(next_snapshot_round=lambda _: 1,
                save_tool_round_snapshot=lambda *args, **kwargs: snapshots.append(kwargs['request_budget']))),
            _emit=emit, _invocation_outcome_data=lambda _: {}, model_invocation=SimpleNamespace(stream_prepared=stream),
            _model_diagnostic=lambda _: None, _managed_transport_failure=lambda _: False)
        spec = SimpleNamespace(managed_runtime=identity, model_id='model', spec_id='spec', round_no=1)
        monkeypatch.setattr('agent.runtime.monitored_events', monitored)
        monkeypatch.setattr('agent.runtime.record_invocation_diagnostics', lambda *args, **kwargs: None)
        monkeypatch.setattr('agent.runtime.ModelInvocationService.provider_session', session)

        async def collect():
            return [event.text async for event in ChatRunService._recovering_local_events(service,
                {'id': 'run'}, 'attempt', provider, None, spec, prepared, messages,
                update_spec=lambda *args: updates.append(args), safe_to_retry=True)]

        if remaining_tokens > 1000:
            with pytest.raises(CoreDataError) as failure:
                await collect()
            assert failure.value.code == 'provider_context_limit_exceeded'
            assert dispatched == []
        else:
            assert await collect() == ['answer']
            assert dispatched == [('spec', 'recompiled')]
            assert updates[-1][1].input_budget['request_tokens'] == 364
            assert updates[-1][1].input_budget['remaining_input_tokens'] == 636
            assert snapshots[-1]['measurement'] == 'engine_template_tokenizer'
        assert len(compiled) == 1 and len(measured_requests) == 2
        assert measured_requests[-1].request_fingerprint == 'recompiled'
        assert all(not key.startswith('_') for value in compiled[0] for key in value)
        assert next(message for message in messages if message['role'] == 'user')['content'] == 'Preserve the original task requirement.'
        calls = {call['id'] for message in messages for call in message.get('tool_calls', [])}
        assert all(message['tool_call_id'] in calls for message in messages if message['role'] == 'tool')

    asyncio.run(scenario())


def test_pinned_tokenizer_counts_without_acquiring_busy_generation_slot(monkeypatch):
    from agent.execution.compiler import InvocationCompiler
    from test_unified_invocation_v3 import model_spec

    async def scenario():
        profile = ProviderProfile('managed:model', 'model', 'openai_compatible', 'http://127.0.0.1:1/v1',
                                  model_capabilities={'model': {'context_length': 8192}})
        models = SimpleNamespace(pinned=lambda model: model == 'model', get_profile=lambda _: profile,
                                 _runtime=SimpleNamespace(session_id='active'))
        provider = ManagedLocalProvider(profile, None, models)

        async def measure(_):
            return 123

        monkeypatch.setattr(provider, '_measure_request', measure)
        spec, request = await ChatRunService._prepare_measured_model_request(
            SimpleNamespace(invocation_compiler=InvocationCompiler()), provider, model_spec(max_output_tokens=512),
            [{'role': 'user', 'content': 'Review the attachment'}], requested_output_tokens=512)
        assert request.input_budget['measurement'] == 'engine_template_tokenizer'
        assert request.input_budget['request_tokens'] == 123 + 64

        async def expired(_):
            models._runtime.session_id = 'new'
            return 123

        monkeypatch.setattr(provider, '_measure_request', expired)
        from agent.providers import ProviderError
        with pytest.raises(ProviderError) as caught:
            await ChatRunService._prepare_measured_model_request(
                SimpleNamespace(invocation_compiler=InvocationCompiler()), provider, spec,
                [{'role': 'user', 'content': 'Review the attachment'}], requested_output_tokens=512)
        assert caught.value.code == 'managed_session_changed'

    asyncio.run(scenario())
