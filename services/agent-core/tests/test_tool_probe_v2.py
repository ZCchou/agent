import asyncio
import json

import pytest

from agent.providers import ProviderError, ProviderEvent
from agent.tool_contracts import ToolCall
from agent.tool_probe_v2 import probe_route


class SemanticProvider:
    def __init__(self, mode, *, behavior='correct', malformed=0):
        self.mode = mode
        self.behavior = behavior
        self.malformed = malformed
        self.calls = []
        self.results = []

    async def stream_events(self, model_id, messages, *, tools, options):
        self.calls.append((messages, tools, options))
        result_messages = [message for message in messages if message['role'] == 'tool'
                           or (message.get('content') or '').startswith('Synthetic fs.list result:')]
        if not result_messages:
            if self.malformed:
                self.malformed -= 1
                yield ProviderEvent('text_delta', text='I cannot format this')
                return
            call = ToolCall('listing-1', 'fs.list', {'path': '.'})
            if self.mode == 'native':
                yield ProviderEvent('tool_call', tool_call=call)
            else:
                yield ProviderEvent('text_delta', text=json.dumps({
                    'type': 'tool_call', 'id': call.provider_call_id, 'name': call.name,
                    'arguments': call.arguments}))
            return
        raw = result_messages[0]['content'].removeprefix('Synthetic fs.list result:')
        observed = json.loads(raw)
        self.results.append(observed)
        if self.behavior == 'loops' and tools:
            yield ProviderEvent('tool_call', tool_call=ToolCall('again', 'fs.list', {'path': '.'}))
            return
        selected = observed['entries'][:2]
        if self.behavior == 'over_scope':
            selected = observed['entries']
        answer = {'nonce': observed['nonce'], 'identifiers': [entry['name'] for entry in selected]}
        if self.behavior == 'ignores_observation':
            answer = {'nonce': 'guessed', 'identifiers': ['alpha', 'beta']}
        content = json.dumps(answer)
        if self.mode == 'structured' and options.allowed_tools:
            content = json.dumps({'type': 'text', 'content': content})
        yield ProviderEvent('text_delta', text=content)


@pytest.mark.parametrize('mode', ['native', 'structured'])
def test_round_trip_uses_fresh_observation_and_bounded_scope(mode):
    provider = SemanticProvider(mode)
    result = asyncio.run(probe_route(provider, 'model', mode))
    assert result['usable']
    assert all(result[field] for field in (
        'format_ok', 'observation_ok', 'scope_ok', 'forced_final_ok', 'voluntary_stop'))
    assert result['actual_calls'] == 3
    assert result['attempts'] == {'request': 1, 'observation': 1, 'forced_final': 1}
    assert provider.calls[-1][1] is False
    assert provider.calls[-1][0][0]['role'] == 'user'  # same tool-free contract as application finalization
    assert all(call[2].allowed_tools in {('fs.list',), ()} for call in provider.calls)
    assert len(provider.results[0]['entries']) == 3
    second = SemanticProvider(mode)
    asyncio.run(probe_route(second, 'model', mode))
    assert second.results[0]['nonce'] != provider.results[0]['nonce']


def test_native_voluntary_loop_still_allows_forced_final():
    result = asyncio.run(probe_route(SemanticProvider('native', behavior='loops'), 'model', 'native'))
    assert result['usable']
    assert result['voluntary_stop'] is False
    assert result['forced_final_ok'] is True


@pytest.mark.parametrize('mode', ['native', 'structured'])
@pytest.mark.parametrize('behavior,failed_field', [
    ('ignores_observation', 'observation_ok'), ('over_scope', 'scope_ok'),
])
def test_well_formed_but_semantically_wrong_answers_fail(mode, behavior, failed_field):
    result = asyncio.run(probe_route(SemanticProvider(mode, behavior=behavior), 'model', mode))
    assert result['format_ok']
    assert not result['usable']
    assert result[failed_field] is False


@pytest.mark.parametrize('mode', ['native', 'structured'])
def test_request_format_retries_have_a_hard_per_stage_bound(mode):
    provider = SemanticProvider(mode, malformed=10)
    result = asyncio.run(probe_route(provider, 'model', mode))
    assert result['status'] == 'unsupported'
    assert result['format_ok'] is False
    assert result['actual_calls'] == 3
    assert result['attempts']['request'] == 3


@pytest.mark.parametrize('code', ['provider_unavailable', 'model_out_of_memory', 'worker_failed',
                                  'invalid_provider_response', 'provider_response_incomplete'])
def test_execution_failures_propagate_instead_of_becoming_unsupported(code):
    class BrokenProvider:
        async def stream_events(self, *args, **kwargs):
            raise ProviderError(code, 'failed')
            yield

    with pytest.raises(ProviderError) as error:
        asyncio.run(probe_route(BrokenProvider(), 'model', 'native'))
    assert error.value.code == code


def test_explicit_unsupported_native_transport_is_classified():
    class UnsupportedProvider:
        async def stream_events(self, *args, **kwargs):
            raise ProviderError('unsupported_capability', 'no native tools')
            yield

    result = asyncio.run(probe_route(UnsupportedProvider(), 'model', 'native'))
    assert result['status'] == 'unsupported'
    assert result['actual_calls'] == 1
    assert result['error']['code'] == 'unsupported_capability'


@pytest.mark.parametrize('mode', ['native', 'structured'])
def test_forced_final_rejects_another_tool_call(mode):
    class NeverStops(SemanticProvider):
        async def stream_events(self, model_id, messages, *, tools, options):
            if options.allowed_tools == ():
                if self.mode == 'native':
                    yield ProviderEvent('tool_call', tool_call=ToolCall('extra', 'fs.list', {'path': '.'}))
                else:
                    yield ProviderEvent('text_delta', text=json.dumps({
                        'type': 'tool_call', 'id': 'extra', 'name': 'fs.list', 'arguments': {'path': '.'}}))
                return
            async for event in super().stream_events(model_id, messages, tools=tools, options=options):
                yield event

    result = asyncio.run(probe_route(NeverStops(mode), 'model', mode))
    assert result['format_ok']
    assert result['voluntary_stop']
    assert result['forced_final_ok'] is False
    assert result['usable'] is False


@pytest.mark.parametrize('mode', ['native', 'structured'])
def test_only_format_failures_retry_each_stage_and_count_every_call(mode):
    class FinalFormatFails(SemanticProvider):
        async def stream_events(self, model_id, messages, *, tools, options):
            if options.allowed_tools == ():
                yield ProviderEvent('text_delta', text='malformed final')
                return
            async for event in super().stream_events(model_id, messages, tools=tools, options=options):
                yield event

    result = asyncio.run(probe_route(FinalFormatFails(mode, malformed=2), 'model', mode))
    assert result['attempts'] == {'request': 3, 'observation': 1, 'forced_final': 3}
    assert result['actual_calls'] == 7
    assert result['format_ok']
    assert not result['forced_final_ok']
    assert result['error']['stage'] == 'forced_final'


def test_opaque_reasoning_is_preserved_for_native_tool_continuation():
    class ReasoningProvider(SemanticProvider):
        async def stream_events(self, model_id, messages, *, tools, options):
            if len(messages) == 1:
                yield ProviderEvent('reasoning_delta', opaque_reasoning_delta='opaque-state')
            else:
                assistant = next(message for message in messages if message['role'] == 'assistant')
                assert assistant['reasoning_content'] == 'opaque-state'
            async for event in super().stream_events(model_id, messages, tools=tools, options=options):
                yield event

    assert asyncio.run(probe_route(ReasoningProvider('native'), 'model', 'native'))['usable']
