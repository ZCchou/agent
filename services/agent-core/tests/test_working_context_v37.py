import json

import pytest

from agent.execution.working_context import compact_tool_history, validate_working_summary
from agent.storage import CoreDataError


def _pair(identity, value):
    return [
        {'role': 'assistant', 'tool_calls': [{'id': identity, 'function': {
            'name': 'document.inspect_template', 'arguments': '{"attachment_id":"attachment-a"}'}}]},
        {'role': 'tool', 'tool_call_id': identity, 'name': 'document.inspect_template',
         'content': json.dumps(value, ensure_ascii=False)},
    ]


def test_inspection_facts_survive_externalization_before_preview_is_shortened():
    original = {'template_id': 'template-a', 'total': 1730,
                'fields': [{'id': 'p-0', 'text': '原始内容甲'}, {'id': 'p-1', 'text': '原始内容乙'}]}
    messages = [{'role': 'system', 'content': 'stable rules'}, {'role': 'user', 'content': '只取前两项，不审阅'}]
    messages += _pair('first', {'result_id': 'r1', 'result_sha256': 'a' * 64,
        'source_ref': {'source_sha256': 'b' * 64}, 'preview': json.dumps(original, ensure_ascii=False) + ' ' * 4000})
    messages += _pair('last', {'result_id': 'r2', 'preview': 'recent'})
    compact_tool_history(messages, input_budget=1000, measure=lambda _: 1000)
    record = next(m for m in messages if m.get('_working_results'))
    facts = record['_references'][0]['facts']
    assert facts['template_id'] == 'template-a'
    assert facts['total'] == 1730
    assert facts['fields'] == original['fields']
    assert record['_references'][0]['result_sha256'] == 'a' * 64
    assert record['_references'][0]['source_ref']['source_sha256'] == 'b' * 64
    assert messages[0]['content'] == 'stable rules'
    assert messages[1] is record


def test_pending_group_never_loses_any_completed_member():
    messages = [{'role': 'system', 'content': 'rules'}]
    group = _pair('a', {'result_id': 'ra', 'preview': 'x' * 4000})
    group[0]['tool_calls'].append({'id': 'pending', 'function': {'name': 'fs.read', 'arguments': '{}'}})
    messages += group + _pair('recent', {'result_id': 'rb', 'preview': 'recent'})
    compact_tool_history(messages, input_budget=1000, measure=lambda _: 1000)
    assert any(m.get('tool_call_id') == 'a' for m in messages)
    assert any(c.get('id') == 'pending' for m in messages for c in m.get('tool_calls', []))


def test_summary_requires_goal_completed_remaining_and_source_identity():
    required = {'goals': ['first two nonempty positions'], 'completed': ['read p-0'],
                'remaining': ['read p-1'], 'source_refs': ['result-a']}
    valid = {'notes': 'next read p-1', **required}
    validate_working_summary(valid, required)
    for key in required:
        with pytest.raises(CoreDataError, match='上下文'):
            validate_working_summary({**valid, key: []}, required)

def test_summary_retry_keeps_authoritative_records_in_saved_notes():
    import asyncio
    from types import SimpleNamespace
    from agent.runtime import ChatRunService

    service = ChatRunService.__new__(ChatRunService)
    service.execution_monitor = SimpleNamespace(start_task=lambda **_: {'id': 'task'})
    goal = {'revision': 2, 'request_digest': 'request-digest', 'source_scopes': [
        {'source_id': 'attachment-a', 'operation': 'read', 'scope': 'selected',
         'selector': {'kind': 'prefix_nonempty', 'count': 2}, 'target_ids': []}],
        'success_criteria': ['only first two positions'], 'deliverables': []}
    service.task_plans = SimpleNamespace(get=lambda _: {'goal_requirements': goal},
        completion=lambda _: {'completed': 1, 'remaining': 1, 'total': 2, 'ready': False})
    calls = []
    async def invoke(*args, **kwargs):
        request = json.loads(args[5][1]['content'])
        calls.append(request)
        value = {'notes': 'derived note'}
        if len(calls) > 1:
            value.update(request['protected_record_ids'])
        return json.dumps(value), None, None
    async def emit(*args):
        return None
    service._invoke_planned_step = invoke
    service._emit = emit
    service._save_working_context = lambda *args: None
    messages = [{'role': 'user', 'content': 'Only first two positions, do not review'}]
    next_round = asyncio.run(service._summarize_working_context({'id': 'run'}, {}, {}, 'attempt', 1,
        None, messages, [{'role': 'tool', 'content': 'observed body'}], 4096, 7))
    assert next_round == 9
    assert len(calls) == 2
    saved = next(message for message in messages if message.get('_working_notes'))['content']
    assert 'prefix_nonempty' in saved
    assert 'only first two positions' in saved
    assert json.loads(json.loads(saved.split('\n', 1)[1])['remaining'][0])['remaining'] == 1
    assert next(message for message in messages if message['role'] == 'user')['content'] == 'Only first two positions, do not review'

def test_summary_protects_current_tool_source_before_history_compaction():
    from agent.execution.working_context import summary_requirements
    messages = _pair('live', {'result_id': 'live-result', 'result_sha256': 'c' * 64,
                             'source_ref': {'source_sha256': 'd' * 64}, 'total': 1730})
    required = summary_requirements(None, None, messages)
    reference = json.loads(required['source_refs'][0])
    assert reference['result_id'] == 'live-result'
    assert reference['result_sha256'] == 'c' * 64
    assert reference['source_ref']['source_sha256'] == 'd' * 64

def test_original_input_pages_keep_latest_active_reference_and_cursor():
    messages = [{'role': 'system', 'content': 'rules'}]
    for number in range(3):
        messages += _pair(str(number), {'result_id': f'page-{number}',
            'result_sha256': str(number) * 64,
            'source_ref': {'kind': 'original_user_message', 'run_id': 'same-task'},
            'offset': number * 1000, 'next_cursor': f'cursor-{number}', 'total': 5000})
    compact_tool_history(messages, input_budget=5000, measure=lambda _: 5000)
    records = next(message for message in messages if message.get('_working_results'))['_references']
    assert len(records) == 1
    assert records[0]['result_id'] == 'page-2'
    assert records[0]['facts']['next_cursor'] == 'cursor-2'


def test_historical_saved_result_hash_takes_precedence_over_raw_source_hash():
    messages = [{'role': 'system', 'content': 'rules'}]
    messages += _pair('old', {'result_id': 'saved-operation', 'result_sha256': 'a' * 64,
        '_saved_result': {'sha256': 'b' * 64},
        'source_ref': {'source_sha256': 'a' * 64}, 'preview': 'x' * 3000})
    messages += _pair('recent', {'result_id': 'recent-operation', 'preview': 'latest'})
    compact_tool_history(messages, input_budget=1000, measure=lambda _: 1000)
    reference = next(m for m in messages if m.get('_working_results'))['_references'][0]
    assert reference['result_sha256'] == 'b' * 64
    assert reference['source_ref']['source_sha256'] == 'a' * 64

def test_original_input_search_keeps_its_own_result_identity_without_read_cursor():
    messages = [{'role': 'system', 'content': 'rules'}]
    source = {'kind': 'original_user_message', 'run_id': 'same-task'}
    messages += _pair('read', {'result_id': 'read-result', 'source_ref': source,
        'offset': 1000, 'next_cursor': 'read-cursor', 'total': 6000})
    messages += _pair('search', {'result_id': 'search-result', 'source_ref': source,
        'matches': [{'offset': 20, 'content': 'match'}]})
    compact_tool_history(messages, input_budget=2000, measure=lambda _: 2000)
    records = next(message for message in messages if message.get('_working_results'))['_references']
    assert {reference['result_id'] for reference in records} == {'read-result', 'search-result'}
    search = next(reference for reference in records if reference['result_id'] == 'search-result')
    assert 'next_cursor' not in search.get('facts', {})


@pytest.mark.parametrize('valid', [True, False])
def test_summary_reclaims_covered_answers_atomically_without_losing_protocol_or_sources(valid):
    import asyncio
    from copy import deepcopy
    from types import SimpleNamespace
    from agent.runtime import ChatRunService
    from agent.execution.working_context import summary_sources

    service = ChatRunService.__new__(ChatRunService)
    service.execution_monitor = SimpleNamespace(start_task=lambda **_: {'id': 'task'})
    service.task_plans = SimpleNamespace(get=lambda _: {}, completion=lambda _: {})
    async def invoke(*args, **kwargs):
        request = json.loads(args[5][1]['content'])
        return json.dumps({'notes': 'short verified notes', **(request['protected_record_ids'] if valid else {})}), None, None
    async def emit(*args):
        pass
    service._invoke_planned_step = invoke
    service._emit = emit
    saved = []
    service._save_working_context = lambda *args: saved.append(deepcopy(args[-1]))
    messages = [{'role': 'system', 'content': 'policy'},
        {'role': 'user', 'content': 'original earlier request'},
        {'role': 'assistant', 'content': 'long previous answer ' * 600},
        {'role': 'assistant', 'content': 'another previous answer ' * 600},
        {'role': 'user', 'content': 'Only cover title; no RAG. ' + 'attachment text ' * 80}]
    messages += _pair('live', {'result_id': 'live-result', 'result_sha256': 'c' * 64,
        'source_ref': {'source_sha256': 'd' * 64}, 'preview': 'cover facts'})
    messages.append({'role': 'user', '_document_read_result': True, 'content': 'latest cover observation'})
    original = deepcopy(messages)
    # Deterministic tokenizer stand-in: the complete working request exceeds a
    # 16K context budget, while bounded summary chunks individually fit.
    measure = lambda values: sum(len(m.get('content', '')) for m in values)
    input_limit = 12684
    assert measure(messages) > input_limit
    call = service._summarize_working_context({'id': 'run'}, {}, {}, 'attempt', 1,
        None, messages, summary_sources(messages), input_limit, 1)
    if not valid:
        with pytest.raises(CoreDataError):
            asyncio.run(call)
        assert messages == original
        assert not saved
        return
    asyncio.run(call)
    assert measure(messages) < input_limit
    assert [m for m in messages if m['role'] == 'user'] == [m for m in original if m['role'] == 'user']
    assert messages[0] == original[0]
    assert all(m in messages for m in original if m.get('tool_calls') or m['role'] == 'tool')
    assert not any('long previous answer' in m.get('content', '') for m in messages)
    notes = next(m for m in messages if m.get('_working_notes'))
    assert 'live-result' in notes['content']
    assert saved[-1] == messages
    # Persistent history represented by the independent original remains intact.
    assert original[2]['content'].startswith('long previous answer')
