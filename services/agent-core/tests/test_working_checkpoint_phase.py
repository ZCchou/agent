import asyncio
import json
from copy import deepcopy

import pytest

from agent.execution.run_recovery import closing_messages
from agent.runtime import ChatRunService
from test_working_summary_recovery import summary_service


@pytest.mark.parametrize('phase', ['main', 'closing', 'legacy_exact', 'legacy_near', 'legacy_user'])
def test_restore_isolates_only_proven_closing_notes_and_keeps_references_protocol(phase):
    policy = closing_messages([], '', 'provider_response_incomplete', [
        {'status': 'completed'}, {'status': 'failed'}])[0]
    if phase == 'legacy_near':
        policy = {**policy, 'content': policy['content'] + ' Additional arbitrary instruction.'}
    if phase == 'legacy_user':
        policy = {**policy, 'role': 'user'}
    references = {'role': 'system', '_working_results': True, '_references': [{'result_id': 'r1'}], 'content': 'exact result references'}
    protocol = [{'role': 'assistant', 'tool_calls': [{'id': 'tool', 'function': {'name': 'fs.read'}}]},
        {'role': 'tool', 'name': 'fs.read', 'tool_call_id': 'tool', 'content': 'original result'}]
    read = {'role': 'user', '_document_read_result': True, 'content': 'authorized source data'}
    notes = {'role': 'assistant', '_working_notes': True, 'content': 'derived notes'}
    saved = {'model_id': 'model', 'messages': [policy, references, *protocol, read, notes],
             'result_dependencies': ['r1']}
    if phase in {'main', 'closing'}:
        saved['phase'] = phase
    original = deepcopy(saved)
    service = ChatRunService.__new__(ChatRunService)
    checked = []
    service._validate_saved_result_source = lambda run, result, **kwargs: checked.append(result)
    restored = service._restored_working_messages({'id': 'run', 'model_id': 'model'}, saved, ('fs.read',))
    assert references in restored and read in restored
    assert all(message in restored for message in protocol)
    assert (notes in restored) == (phase not in {'closing', 'legacy_exact'})
    assert checked == ['r1']
    assert saved == original  # No deletion of saved system policy or DB history.


def test_response_only_save_cannot_overwrite_main_checkpoint(tmp_path):
    from test_tool_repository import started_run
    core, _, run, attempt = started_run(tmp_path)
    service = ChatRunService.__new__(ChatRunService)
    service.repository = core
    messages = [{'role': 'assistant', '_working_notes': True, 'content': 'main work'}]
    service._save_working_context(run['id'], attempt['id'], messages)
    with core._connect() as db:
        before = dict(db.execute('SELECT * FROM run_working_contexts').fetchone())
    assert json.loads(before['context_json'])['phase'] == 'main'
    service._save_working_context(run['id'], attempt['id'],
        [{'role': 'assistant', '_working_notes': True, 'content': 'closing work'}], phase='closing')
    with core._connect() as db:
        assert dict(db.execute('SELECT * FROM run_working_contexts').fetchone()) == before


def test_response_only_summary_never_saves_closing_notes_over_main_memory():
    async def invoke(*args, **kwargs):
        value = json.loads(args[5][1]['content'])
        return json.dumps({'notes': 'closing note', **value['protected_record_ids']}), {}, []
    service = summary_service(invoke)
    messages = [{'role': 'user', 'content': 'original request'}]
    asyncio.run(service._summarize_working_context({'id': 'run'}, {}, {}, 'attempt', 1,
        None, messages, [{'role': 'assistant', 'content': 'derived'}], 12000, 1, checkpoint_phase='closing'))
    assert not service.saved
    notes = next(message for message in messages if message.get('_working_notes'))
    assert notes['_working_context_phase'] == 'closing'
