import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from agent.providers import ProviderError
from agent.runtime import ChatRunService


def summary_service(invoke):
    service = ChatRunService.__new__(ChatRunService)
    service.execution_monitor = SimpleNamespace(start_task=lambda **_: {'id': 'task'})
    service.task_plans = SimpleNamespace(get=lambda _: {}, completion=lambda _: {})
    service._invoke_planned_step = invoke
    async def emit(*args):
        pass
    service._emit = emit
    service.saved = []
    service._save_working_context = lambda *args: service.saved.append(deepcopy(args[-1]))
    return service


def length_error():
    return ProviderError('provider_response_incomplete', 'Truncated summary',
        details={'incomplete_reason': 'length', 'text_character_count': 3356})


@pytest.mark.parametrize('ceiling,expected', [(None, [1024, 2048, 4096]), (1500, [1024, 1500])])
@pytest.mark.parametrize('succeeds', [False, True])
def test_summary_truncation_has_bounded_budget_retry_and_atomic_messages(ceiling, expected, succeeds):
    calls = []
    messages = [{'role': 'user', 'content': 'Original request'},
                {'role': 'assistant', 'content': 'Derived historical answer'}]
    original = deepcopy(messages)
    error = length_error()
    async def invoke(*args, **kwargs):
        assert messages == original
        calls.append(kwargs)
        if not succeeds or len(calls) < len(expected):
            raise error
        request = json.loads(args[5][1]['content'])
        return json.dumps({'notes': 'bounded note', **request['protected_record_ids']}), {}, []
    service = summary_service(invoke)
    provider = SimpleNamespace(profile=SimpleNamespace(model_capabilities={'model': {
        'max_output_tokens': ceiling, 'context_length': 16128}}))
    call = service._summarize_working_context({'id': 'run', 'model_id': 'model'}, {}, {}, 'attempt', 1,
        provider, messages, [{'role': 'assistant', 'content': original[1]['content']}], 12000, 7)
    if succeeds:
        assert asyncio.run(call) == 7 + len(expected)
        assert service.saved
    else:
        with pytest.raises(ProviderError) as caught:
            asyncio.run(call)
        assert caught.value is error
        assert getattr(error, 'working_context_summary_failed', False)
        assert messages == original and not service.saved
    assert [call['max_output_tokens'] for call in calls] == expected
    assert [call['round_no'] for call in calls] == list(range(7, 7 + len(expected)))


def test_non_length_summary_error_does_not_retry_or_modify_sources():
    calls = []
    error = ProviderError('permission_denied', 'Revoked')
    async def invoke(*args, **kwargs):
        calls.append(kwargs)
        raise error
    service = summary_service(invoke)
    messages = [{'role': 'user', 'content': 'Original request'}]
    original = deepcopy(messages)
    with pytest.raises(ProviderError) as caught:
        asyncio.run(service._summarize_working_context({'id': 'run'}, {}, {}, 'attempt', 1,
            None, messages, [{'role': 'assistant', 'content': 'derived'}], 12000, 7))
    assert caught.value is error and len(calls) == 1
    assert messages == original and not service.saved


def test_exhausted_summary_does_not_reenter_model_closing_compaction():
    error = length_error()
    error.working_context_summary_failed = True
    turns, events = [], []
    service = ChatRunService.__new__(ChatRunService)
    async def generate(*args, **kwargs):
        turns.append(kwargs)
        raise error
    async def emit(*args):
        events.append(args)
    service._generate_tool_turns = generate
    service._emit = emit
    service._compaction_event = emit
    service.rag_grounding = SimpleNamespace(runs={})
    service.repository = SimpleNamespace(get_model_run=lambda _: {'draft': {}},
        transition_model_run=lambda *args: None)
    operations = [{'tool_name': 'document.inspect_template', 'status': 'completed'}]
    service.tool_loop = SimpleNamespace(repository=SimpleNamespace(list_for_run=lambda _: operations), interrupt_run=lambda _: None)
    service.execution_repository = SimpleNamespace(get_execution_plan=lambda _: {'invocations': [
        {'step_id': 'step', 'attempt_no': 1, 'round_no': 1, 'dispatched_at': 'now'}]})
    service._validate_context_dispatch_authority = lambda *args: None
    run = {'id': 'run', 'conversation_id': 'conversation'}
    with pytest.raises(ProviderError) as caught:
        asyncio.run(service._generate_with_tools(run, 'attempt', object(),
            [{'role': 'user', 'content': 'Original request'}], True, 'native',
            execution_plan={'id': 'plan'}, execution_step={'id': 'step'}, attempt_number=1, max_output_tokens=4096))
    assert caught.value is error
    assert error.explanation_saved  # Outer run emits a deterministic failed receipt.
    assert len(turns) == 1  # No closing model invocation or second compaction.
    assert not run.get('_response_recovered')



def test_summary_failure_remains_failed_in_real_run_and_never_calls_explanation_model(tmp_path, monkeypatch):
    from test_tool_continuation_retry import fixture, install_http, start
    requests = install_http(monkeypatch, [])
    async def scenario():
        events = []
        async def sink(event):
            events.append(event)
        repository, _, work, service = await fixture(tmp_path, sink)
        error = length_error()
        error.working_context_summary_failed = True
        async def failed_summary(*args, **kwargs):
            raise error
        monkeypatch.setattr(service, '_generate_tool_turns', failed_summary)
        run_id = await start(service, work)
        run = repository.get_model_run(run_id)
        assert run['status'] == 'failed'
        assert run['error_code'] == 'provider_response_incomplete'
        assert not requests
        assert any(event['event_type'] == 'run.failed' and event['data']['fallback_content'] for event in events)
        assert not any(event['event_type'] == 'run.completed' for event in events)
    asyncio.run(scenario())



def test_length_and_json_corrections_share_three_audited_calls():
    calls = []
    async def invoke(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise length_error()
        if len(calls) == 2:
            return '{}', {}, []
        request = json.loads(args[5][1]['content'])
        return json.dumps({'notes': 'valid', **request['protected_record_ids']}), {}, []
    service = summary_service(invoke)
    messages = [{'role': 'user', 'content': 'Original request'}]
    assert asyncio.run(service._summarize_working_context({'id': 'run'}, {}, {}, 'attempt', 1,
        None, messages, [{'role': 'assistant', 'content': 'derived'}], 12000, 7)) == 10
    assert [call['max_output_tokens'] for call in calls] == [1024, 2048, 2048]
    assert [call['round_no'] for call in calls] == [7, 8, 9]


def test_later_piece_exhaustion_preserves_all_original_messages_and_protocol_groups():
    messages = [{'role': 'user', 'content': 'Original request'},
        {'role': 'assistant', 'content': 'first derived'},
        {'role': 'assistant', 'content': 'later derived'},
        {'role': 'assistant', 'tool_calls': [{'id': 'tool', 'function': {'name': 'fs.read', 'arguments': '{}'}}]},
        {'role': 'tool', 'name': 'fs.read', 'tool_call_id': 'tool', 'content': 'original result'}]
    original, calls = deepcopy(messages), []
    async def invoke(*args, **kwargs):
        assert messages == original
        calls.append(kwargs)
        if len(calls) > 1:
            raise length_error()
        request = json.loads(args[5][1]['content'])
        return json.dumps({'notes': 'first piece complete', **request['protected_record_ids']}), {}, []
    service = summary_service(invoke)
    with pytest.raises(ProviderError):
        asyncio.run(service._summarize_working_context({'id': 'run'}, {}, {}, 'attempt', 1, None,
            messages, [{'role': 'assistant', 'content': 'first derived'},
                       {'role': 'assistant', 'content': 'later derived'}], 12000, 7))
    assert messages == original and not service.saved
    assert len(calls) == 4
