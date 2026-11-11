import asyncio
import json
import math
import pytest
from agent.context_engine import policy
from agent.context_engine.domain import CompactionTarget, ContextEngineError
from agent.context_engine.service import ContextEngineService
from agent.providers import ProviderError, ProviderEvent
from agent.storage import CoreRepository
from test_context_compaction_lifecycle import seed, CheckpointProvider


def test_schema_checkpoint_uses_short_sources_and_application_trust(tmp_path):
    from agent.providers import ProviderProfile
    class Provider:
        profile = ProviderProfile('local', 'Local', 'openai-compatible', 'http://127.0.0.1:9000')
        async def stream_events(self, model, messages, *, tools, options):
            assert options.response_format == 'json_schema'
            schema = options.response_schema
            assert schema['properties']['items']['items']['properties']['source_ids']['items']['enum'][0] == 's1'
            envelope = json.loads(messages[1]['content'])
            source = envelope['sources'][0]
            assert source['source_id'] == 's1'
            yield ProviderEvent('text_delta', text=json.dumps({'items': [
                {'category': 'constraints', 'text': '保留原始要求', 'source_ids': ['s1']}]}))
    async def scenario():
        repo = CoreRepository(tmp_path / 'schema-checkpoint.db')
        result = await ContextEngineService(repo).compact_once(seed(repo), Provider(), context_window=8192, max_output_tokens=1024,
            target=CompactionTarget('local', 'local-model', 'openai_chat', 8192, 1024))
        assert result['content']['constraints'][0]['trust'] == 'user_authored'
        assert result['content']['constraints'][0]['source_ids'] != ['s1']
    asyncio.run(scenario())


def test_checkpoint_diagnostics_identify_invalid_source_without_content():
    from agent.context_engine.compaction import parse_checkpoint
    with pytest.raises(ContextEngineError) as caught:
        parse_checkpoint(json.dumps({'items': [{'category': 'goal', 'text': 'PRIVATE CONTENT', 'source_ids': ['unknown']}]}),
                         {'real-source': 'user_authored'}, source_aliases={'s1': 'real-source'})
    assert caught.value.details['validation_reason'] == 'invalid_source'
    assert 'PRIVATE CONTENT' not in str(caught.value.details)


def test_local_compaction_uses_local_first_output_and_total_timeouts(tmp_path, monkeypatch):
    from agent.providers import ProviderProfile
    import agent.context_engine.service as module
    delays = []
    real_timeout = asyncio.timeout
    def capture_timeout(delay):
        delays.append(delay)
        return real_timeout(delay)
    monkeypatch.setattr(module.asyncio, 'timeout', capture_timeout)
    async def scenario():
        repo = CoreRepository(tmp_path / 'local-timeouts.db')
        provider = CheckpointProvider()
        provider.profile = ProviderProfile('local', 'Local', 'openai-compatible', 'http://127.0.0.1:9000')
        await ContextEngineService(repo).compact_once(seed(repo), provider, context_window=32768, max_output_tokens=512)
        assert 899 < delays[0] <= 900
        assert 299 < delays[1] <= 300
        assert 60 in delays
    asyncio.run(scenario())

@pytest.mark.parametrize('window', [2048, 4096, 8192, 32768])
@pytest.mark.parametrize('limit', [512, 4096, 8192, None])
def test_complete_request_fits_window_and_model(window, limit):
    assert hasattr(policy, 'resolve_compaction_request')
    request = policy.resolve_compaction_request(
        [{'id': 'm1', 'role': 'user', 'content': '事实🙂' * 30000, 'sequence_no': 1}], None,
        CompactionTarget('p', 'm', 'openai_chat', window, limit))
    assert request.output_budget.initial_tokens <= (limit or 8192)
    assert request.estimated_input_tokens + request.output_budget.reservation_tokens + request.safety_margin_tokens <= window
    assert request.estimated_input_tokens >= math.ceil(len(json.dumps(request.messages, ensure_ascii=False).encode('utf-8')) / 3)
    assert request.sources[0]['content']

@pytest.mark.parametrize('window', [0, -1, 128])
def test_no_space_never_calls_provider(tmp_path, window):
    async def scenario():
        repo = CoreRepository(tmp_path / 'no-space.db')
        provider = CheckpointProvider()
        with pytest.raises(ContextEngineError) as caught:
            await ContextEngineService(repo).compact_once(seed(repo), provider, context_window=window, max_output_tokens=512)
        assert caught.value.code == 'context_compaction_budget_exhausted'
        assert provider.calls == []
    asyncio.run(scenario())

def test_correction_usage_and_actual_request_budget(tmp_path):
    class Provider(CheckpointProvider):
        async def stream_events(self, model, messages, *, tools, options):
            assert options.max_output_tokens <= 512
            assert math.ceil(len(json.dumps(messages, ensure_ascii=False).encode('utf-8')) / 3) + options.max_output_tokens + 205 <= 4096
            if not self.calls:
                self.calls.append('bad')
                yield ProviderEvent('usage', usage={'input_tokens': 11, 'output_tokens': 7})
                yield ProviderEvent('text_delta', text='bad')
            else:
                self.usage = {'input_tokens': 13, 'output_tokens': 5}
                async for event in super().stream_events(model, messages, tools=tools, options=options):
                    yield event
    async def scenario():
        repo = CoreRepository(tmp_path / 'usage.db')
        service = ContextEngineService(repo)
        run = seed(repo)
        await service.compact_once(run, Provider(), context_window=4096, max_output_tokens=512)
        status = service.context_repository.compaction_status(run['conversation_id'])
        assert (status['input_tokens'], status['output_tokens']) == (24, 12)
    asyncio.run(scenario())

def test_transient_fallback_and_correction_share_three_calls(tmp_path):
    async def scenario():
        repo = CoreRepository(tmp_path / 'calls.db')
        provider = CheckpointProvider([ProviderError('provider_request_not_sent', 'safe'), ProviderError('response_format_unsupported', 'safe'), 'bad'])
        with pytest.raises(ContextEngineError) as caught:
            await ContextEngineService(repo).compact_once(seed(repo), provider, context_window=32768, max_output_tokens=512)
        assert caught.value.code == 'context_compaction_call_limit'
        assert len(provider.calls) == 3
    asyncio.run(scenario())

def test_old_checkpoint_and_oversized_correction_are_not_discarded():
    target = CompactionTarget('p', 'm', 'openai_chat', 2048, 512)
    source = [{'id': 'm', 'role': 'user', 'content': 'fact', 'sequence_no': 1}]
    with pytest.raises(ContextEngineError, match='budget'):
        policy.resolve_compaction_request(source, {'id': 'cp', 'content_json': '🙂' * 10000}, target)
    request = policy.resolve_compaction_request(source, None, target, protocol_limit=127)
    assert request.output_budget.initial_tokens <= 127
    with pytest.raises(ContextEngineError, match='budget'):
        policy.resolve_compaction_request(request.sources, None, target, correction='bad' * 10000)


def test_correction_cannot_overflow_and_failed_usage_is_saved(tmp_path):
    class Provider:
        calls = 0
        async def stream_events(self, *args, **kwargs):
            self.calls += 1
            yield ProviderEvent('usage', usage={'input_tokens': 100, 'output_tokens': 512})
            yield ProviderEvent('text_delta', text='x' * 20000)
    async def scenario():
        repo = CoreRepository(tmp_path / 'oversized.db')
        run, provider = seed(repo), Provider()
        service = ContextEngineService(repo)
        with pytest.raises(ContextEngineError) as caught:
            await service.compact_once(run, provider, context_window=2048, max_output_tokens=512)
        assert caught.value.code == 'context_compaction_budget_exhausted'
        assert provider.calls == 1
        status = service.context_repository.compaction_status(run['conversation_id'])
        assert (status['input_tokens'], status['output_tokens']) == (100, 512)
    asyncio.run(scenario())


def test_retries_share_deadline_and_subtimeouts(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from agent.context_engine import service as module
    elapsed, limits = [0.0], []
    timeout = asyncio.timeout
    def capture_timeout(delay):
        limits.append(delay)
        return timeout(delay)
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: elapsed[0]))
    monkeypatch.setattr(module.asyncio, 'timeout', capture_timeout)
    class Provider:
        calls = 0
        async def stream_events(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                elapsed[0] += 80
                raise ProviderError('response_format_unsupported', 'safe')
            elapsed[0] += 41
            raise ProviderError('provider_request_not_sent', 'safe')
            yield
    async def scenario():
        repo = CoreRepository(tmp_path / 'deadline.db')
        provider = Provider()
        with pytest.raises(ProviderError) as caught:
            await ContextEngineService(repo).compact_once(seed(repo), provider, context_window=4096, max_output_tokens=512)
        assert caught.value.code == 'request_timeout'
        assert provider.calls == 2
        assert limits == [120, 60, 40, 40]
    asyncio.run(scenario())


def test_full_wire_envelope_is_included_in_estimate():
    request = policy.resolve_compaction_request(
        [{'id': 'm', 'role': 'user', 'content': 'fact', 'sequence_no': 1}], None,
        CompactionTarget('p', 'model-' + 'x' * 600, 'openai_chat', 4096, 512))
    wire = {'model': 'model-' + 'x' * 600, 'messages': request.messages, 'stream': True,
            'max_tokens': request.output_budget.initial_tokens, 'response_format': {'type': 'json_object'}}
    assert request.estimated_input_tokens >= math.ceil(len(json.dumps(wire, ensure_ascii=False).encode('utf-8')) / 3)

@pytest.mark.parametrize('code', [None, 'connection_reset', 'provider_response_incomplete'])
def test_unknown_or_truncated_result_is_not_replayed(tmp_path, code):
    class Provider:
        calls = 0
        async def stream_events(self, *args, **kwargs):
            self.calls += 1
            if code:
                raise ProviderError(code, 'uncertain outcome')
            yield ProviderEvent('finish', finish_reason='stop')
    async def scenario():
        repo = CoreRepository(tmp_path / 'uncertain.db')
        provider = Provider()
        with pytest.raises((ContextEngineError, ProviderError)):
            await ContextEngineService(repo).compact_once(seed(repo), provider, context_window=4096, max_output_tokens=512)
        assert provider.calls == 1
    asyncio.run(scenario())


def test_resolved_budget_reaches_real_chat_transport(tmp_path, monkeypatch):
    import httpx
    from agent.providers import OpenAICompatibleProvider, ProviderProfile
    captured = []
    class Response:
        is_error = False
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def aiter_lines(self):
            checkpoint = {key: [] for key in ('goal', 'success_criteria', 'constraints', 'decisions', 'completed', 'relative_files', 'failed_approaches', 'open_tasks', 'next_step')}
            yield 'data: ' + json.dumps({'choices': [{'delta': {'content': json.dumps(checkpoint)}, 'finish_reason': 'stop'}]})
            yield 'data: [DONE]'
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def stream(self, *args, **kwargs):
            captured.append(kwargs['json'])
            return Response()
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: Client())
    async def scenario():
        repo = CoreRepository(tmp_path / 'transport.db')
        provider = OpenAICompatibleProvider(ProviderProfile('p', 'Test', 'openai_compatible', 'https://synthetic.invalid/v1'))
        await ContextEngineService(repo).compact_once(seed(repo), provider, context_window=2048, max_output_tokens=512)
    asyncio.run(scenario())
    assert len(captured) == 1
    assert captured[0]['max_tokens'] <= 512
    assert math.ceil(len(json.dumps(captured[0], ensure_ascii=False).encode('utf-8')) / 3) + captured[0]['max_tokens'] + 128 <= 2048

def test_expired_deadline_during_authority_guard_never_dispatches(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from agent.context_engine import service as module
    elapsed = [0]
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: elapsed[0]))
    async def scenario():
        repo = CoreRepository(tmp_path / 'guard-deadline.db')
        provider = CheckpointProvider()
        def guard(): elapsed[0] = 121
        with pytest.raises(ProviderError):
            await ContextEngineService(repo).compact_once(seed(repo), provider, context_window=4096, max_output_tokens=512, dispatch_guard=guard)
        assert provider.calls == []
    asyncio.run(scenario())

@pytest.mark.parametrize('contents', [('', 'later fact'), ('first fact', '', 'later fact')])
def test_empty_historical_sources_do_not_block_frontier(contents):
    sources = [{'id': f'm{i}', 'role': 'user', 'content': content, 'sequence_no': i + 1}
               for i, content in enumerate(contents)]
    request = policy.resolve_compaction_request(sources, None, CompactionTarget('p', 'm', 'openai_chat', 32768, 512))
    assert request.through_sequence == len(contents)
    assert [source['content'] for source in request.sources] == list(contents)
    assert request.estimated_input_tokens + request.output_budget.reservation_tokens + request.safety_margin_tokens <= 32768


def test_attachment_rejection_only_history_compacts_through_later_message(tmp_path):
    async def scenario():
        repo = CoreRepository(tmp_path / 'empty-history.db')
        conversation = repo.create_conversation('standard_chat', 'History', 'casual')
        old = repo.start_model_run(conversation_id=conversation['id'], content='', idempotency_key='old',
            provider_profile_id='builtin-demo', model_id='demo',
            attachment_rejections=[{'receipt_id': 'synthetic', 'display_name': 'synthetic.txt', 'error_code': 'unsupported'}])
        repo.transition_model_run(old['id'], {'queued'}, 'failed')
        later = repo._insert_message(conversation['id'], 'assistant', 'later fact')
        run = repo.start_model_run(conversation_id=conversation['id'], content='continue', idempotency_key='current', provider_profile_id='builtin-demo', model_id='demo')
        provider = CheckpointProvider()
        result = await ContextEngineService(repo).compact_once(run, provider, context_window=32768, max_output_tokens=512)
        assert result['through_sequence'] == 2
        assert len(provider.calls) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['500', '502', '503', '504', 'connect', 'late_connect'])
def test_real_http_outcome_controls_replay(tmp_path, monkeypatch, failure):
    import httpx
    from agent.providers import OpenAICompatibleProvider, ProviderProfile
    posts = []
    class Response:
        is_error = failure != 'late_connect'
        status_code = int(failure) if failure.isdigit() else 504
        headers = {}
        text = 'Gateway timed out'
        def json(self): return {}
        async def aread(self): return b''
        async def __aenter__(self):
            if failure == 'connect':
                raise httpx.ConnectError('synthetic connection refused')
            return self
        async def __aexit__(self, *args): pass
        async def aiter_lines(self):
            raise httpx.ConnectError('synthetic failure after response opened')
            yield
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def stream(self, *args, **kwargs):
            posts.append(kwargs['json'])
            return Response()
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: Client())
    async def scenario():
        repo = CoreRepository(tmp_path / 'http-outcome.db')
        provider = OpenAICompatibleProvider(ProviderProfile('p', 'Test', 'openai_compatible', 'https://synthetic.invalid/v1'))
        with pytest.raises(ProviderError) as caught:
            await ContextEngineService(repo).compact_once(seed(repo), provider, context_window=32768, max_output_tokens=512)
        if failure.isdigit():
            assert caught.value.code == 'provider_unavailable'
            assert len(posts) == 1
        elif failure == 'late_connect':
            assert caught.value.code == 'connection_reset'
            assert len(posts) == 1
        else:
            assert caught.value.code == 'provider_request_not_sent'
            assert len(posts) == 3
    asyncio.run(scenario())
