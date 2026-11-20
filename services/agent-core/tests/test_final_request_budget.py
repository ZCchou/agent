import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from agent.execution.compiler import InvocationCompiler
from agent.local_provider import ManagedLocalProvider
from agent.providers import ProviderProfile
from agent.runtime import ChatRunService
from agent.storage import CoreDataError
from test_unified_invocation_v3 import model_spec


def test_actual_count_replaces_estimate_without_widening_context():
    compiler = InvocationCompiler()
    spec = model_spec(max_output_tokens=2048)
    messages = [{'role': 'user', 'content': 'x' * 18000}]
    with pytest.raises(CoreDataError):
        compiler.compile_model(spec, messages, context_window=8192)
    result = compiler.compile_model(spec, messages, context_window=8192, measured_input_tokens=3000)
    assert result.input_budget['request_tokens'] == 3064
    assert result.input_budget['input_hard_limit'] == 4915
    assert result.input_budget['measurement'] == 'engine_template_tokenizer'
    with pytest.raises(CoreDataError):
        compiler.compile_model(spec, messages, context_window=8192, measured_input_tokens=4915)


@pytest.mark.parametrize('measurement,expected,requested', [(3000, 2048, 2048), (5000, 1899, 2048), (None, 1586, 2048), (3000, 128, 128)])
def test_exact_preparation_restores_only_original_output_allowance(monkeypatch, measurement, expected, requested):
    async def scenario():
        profile = ProviderProfile('managed:model', 'model', 'openai_compatible', 'http://127.0.0.1:1/v1',
                                  model_capabilities={'model': {'context_length': 8192}})
        provider = ManagedLocalProvider(profile, None, None)
        compiler = InvocationCompiler()
        counts = []

        async def measure(request):
            counts.append(request.options.max_output_tokens)
            return measurement

        @asynccontextmanager
        async def session(_):
            yield SimpleNamespace(measure_request=measure)

        service = SimpleNamespace(invocation_compiler=compiler)
        monkeypatch.setattr('agent.runtime.ModelInvocationService.provider_session', session)
        observed = []
        spec, prepared = await ChatRunService._prepare_measured_model_request(service, provider,
            model_spec(max_output_tokens=1586), [{'role': 'user', 'content': 'hello'}],
            requested_output_tokens=requested, refresh_snapshot=lambda value: observed.append(value.max_output_tokens))
        assert spec.max_output_tokens == prepared.options.max_output_tokens == expected
        assert observed[-1] == expected
        assert spec.spec_hash != model_spec(max_output_tokens=1586).spec_hash or expected == 1586
        if measurement is not None:
            assert prepared.input_budget['request_tokens'] + expected <= int(8192 * .85)

    asyncio.run(scenario())


def test_exact_overflow_exposes_only_budget_scalars(monkeypatch):
    async def scenario():
        profile = ProviderProfile('managed:model', 'model', 'openai_compatible', 'http://127.0.0.1:1/v1',
                                  model_capabilities={'model': {'context_length': 16128}})
        provider = ManagedLocalProvider(profile, None, None)
        async def measure(_):
            return 14000
        @asynccontextmanager
        async def session(_):
            yield SimpleNamespace(measure_request=measure)
        monkeypatch.setattr('agent.runtime.ModelInvocationService.provider_session', session)
        service = SimpleNamespace(invocation_compiler=InvocationCompiler())
        with pytest.raises(CoreDataError) as caught:
            await ChatRunService._prepare_measured_model_request(service, provider,
                model_spec(max_output_tokens=4032), [{'role': 'user', 'content': 'private original'}],
                requested_output_tokens=4032)
        assert caught.value.code == 'provider_context_limit_exceeded'
        assert caught.value.details == {
            'context_window': 16128, 'measured_input_tokens': 14000,
            'input_hard_limit': 13196, 'minimum_output_reserve': 512,
            'requested_output_tokens': 4032, 'safety_margin_tokens': 64,
            'available_output_tokens': -356,
        }
    asyncio.run(scenario())
