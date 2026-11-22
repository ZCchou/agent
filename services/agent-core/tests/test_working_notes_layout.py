"""Derived memory precedes current requests without changing source authority or tools."""
import asyncio
from copy import deepcopy
import json

import pytest

from agent import runtime
from agent.message_assembly import extend_application_messages as extend, validate_system_message_prefix
from test_working_summary_recovery import summary_service


def pair():
    return [{'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'call', 'type': 'function',
        'function': {'name': 'tools.discover', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'call', 'name': 'tools.discover', 'content': '{"enabled":[]}'}]


def base():
    return [{'role': 'system', 'content': 'authority'},
            {'role': 'user', 'content': 'Review the selected formal content and save the report'},
            {'role': 'user', '_document_read_result': True, 'content': 'authorized original observation'},
            *pair()]


def assert_layout(messages, original):
    notes = [message for message in messages if message.get('_working_notes')]
    assert len(notes) == 1
    assert notes[0]['role'] == 'assistant' and not notes[0].get('tool_calls')
    assert messages.index(notes[0]) < next(i for i, message in enumerate(messages) if message['role'] == 'user')
    assert messages[-1]['role'] == 'tool'
    assert [message for message in messages if message.get('role') == 'user'] == [message for message in original if message.get('role') == 'user']
    assert [message for message in messages if message.get('tool_calls') or message.get('role') == 'tool'] == pair()
    validate_system_message_prefix(messages)


def service_for_summary():
    async def invoke(*args, **kwargs):
        value = json.loads(args[5][1]['content'])
        return json.dumps({'notes': 'validated derived summary', **value['protected_record_ids']}), {}, []
    service = summary_service(invoke)
    return service


def summarize(service, messages, round_no=1):
    sources = [{'role': 'assistant', 'content': str(message.get('content') or '')}
               for message in messages if message['role'] == 'assistant' and not message.get('tool_calls')]
    if not sources:
        sources = [{'role': 'assistant', 'content': 'completed prior work'}]
    return asyncio.run(service._summarize_working_context({'id': 'run', 'model_id': 'model'}, {}, {},
        'attempt', 1, None, messages, sources, 12000, round_no))


def test_successful_summary_precedes_current_user_observation_and_tool_exchange():
    messages = base()
    original = deepcopy(messages)
    service = service_for_summary()
    summarize(service, messages)
    assert_layout(messages, original)
    assert service.saved[-1] == messages


def test_multiple_validated_summaries_leave_one_notes_without_changing_protocol():
    messages = base()
    original = deepcopy(messages)
    service = service_for_summary()
    round_no = 1
    for _ in range(3):
        round_no = summarize(service, messages, round_no)
        assert_layout(messages, original)
    assert round_no == 4


@pytest.mark.parametrize('phase', ['main', 'legacy'])
def test_legacy_tail_notes_restore_before_current_user_with_dependencies_intact(phase):
    notes = {'role': 'assistant', '_working_notes': True, '_working_context_phase': 'main',
             '_protected_records': {'source_refs': ['result-original']}, 'content': 'derived checkpoint notes'}
    refs = {'role': 'system', '_working_results': True, 'content': 'exact source/result references'}
    saved = {'model_id': 'model', 'messages': [refs, *pair(), notes], 'result_dependencies': ['result-original']}
    if phase == 'main':
        saved['phase'] = phase
    immutable = deepcopy(saved)
    service = runtime.ChatRunService.__new__(runtime.ChatRunService)
    checked = []
    service._validate_saved_result_source = lambda run, result, **kwargs: checked.append(result)
    restored = service._restored_working_messages({'id': 'run', 'model_id': 'model'}, saved, ('tools.discover',))
    messages = base()[:3]
    extend(messages, restored)
    assert_layout(messages, base())
    assert refs in messages and checked == ['result-original']
    assert saved == immutable
    assert next(message for message in messages if message.get('_working_notes')) == notes


def test_closing_checkpoint_notes_remain_quarantined_and_protocol_is_reused():
    notes = {'role': 'assistant', '_working_notes': True, 'content': 'closing derived notes'}
    saved = {'model_id': 'model', 'phase': 'closing', 'messages': [*pair(), notes], 'result_dependencies': []}
    service = runtime.ChatRunService.__new__(runtime.ChatRunService)
    messages = base()[:3]
    extend(messages, service._restored_working_messages({'id': 'run', 'model_id': 'model'}, saved, ('tools.discover',)))
    assert not any(message.get('_working_notes') for message in messages)
    assert messages[-2:] == pair()


def test_unmarked_assistant_and_flagged_user_are_not_moved_or_promoted():
    messages = base()
    additions = [{'role': 'assistant', 'content': 'ordinary assistant'},
                 {'role': 'user', '_working_notes': True, 'content': 'user data'}]
    extend(messages, additions)
    assert messages[-2:] == additions
