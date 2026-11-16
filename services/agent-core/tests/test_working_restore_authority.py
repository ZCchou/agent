import json
from types import SimpleNamespace

import pytest

from agent.execution.results import ResultStore
from agent.runtime import ChatRunService
from agent.storage import CoreDataError
from agent.tool_contracts import ToolCall
from test_tool_repository import started_run


@pytest.mark.parametrize('source', ['rag', 'removed_attachment', 'changed_attachment'])
def test_restore_revalidates_saved_source_before_reusing_observations(tmp_path, source):
    core, tools, run, attempt = started_run(tmp_path)
    attachment_id = '597922fb-90a2-41e6-8c48-3532e89563ef'
    name = 'rag.read' if source == 'rag' else 'document.read_attachment'
    arguments = {'evidence_id': 'evidence'} if source == 'rag' else {'attachment_id': attachment_id}
    operation = tools.create(run['id'], attempt['id'], 1, ToolCall('original', name, arguments))
    store = ResultStore(core.database_path)
    result = store.save(operation['id'], '{"source_sha256":"old-hash","content":"protected old body"}', success=True)
    wrapper = tools.create(run['id'], attempt['id'], 2,
        ToolCall('wrapper', 'tools.read_result', {'result_id': result['result_id']}))
    store.save(wrapper['id'], 'protected old body', success=True)
    attachments = [] if source == 'removed_attachment' else [{'id': attachment_id, 'sha256': 'new-hash'}]
    def validate(_run_id):
        raise CoreDataError('rag_evidence_stale', 'Revoked evidence')
    service = ChatRunService.__new__(ChatRunService)
    service.repository = SimpleNamespace(get_model_run=lambda _: run,
        get_run_conversation=lambda _: {'messages': [{'id': run['user_message_id'], 'attachments': attachments}]})
    service.result_store = store
    service.rag_grounding = SimpleNamespace(validate=validate)
    service.document_attachments = SimpleNamespace(_original=lambda _: None)
    service._validate_saved_result_source = ChatRunService._validate_saved_result_source.__get__(service)
    saved = {'model_id': run['model_id'], 'result_dependencies': [wrapper['id']], 'messages': [
        {'role': 'assistant', '_working_notes': True, 'content': 'protected old body'},
        {'role': 'user', '_document_read_result': True, 'content': 'protected old body'}]}
    with pytest.raises(CoreDataError) as failure:
        service._restored_working_messages(run, saved, ('tools.read_result',))
    assert failure.value.code == {'rag': 'rag_evidence_stale',
        'removed_attachment': 'attachment_reference_forbidden',
        'changed_attachment': 'attachment_changed'}[source]


def test_restore_preserves_complete_allowed_tool_pairs_and_notes():
    service = ChatRunService.__new__(ChatRunService)
    service._validate_saved_result_source = lambda *args, **kwargs: None
    saved = {'model_id': 'model', 'result_dependencies': [], 'messages': [
        {'role': 'assistant', 'tool_calls': [
            {'id': 'a', 'function': {'name': 'fs.read'}}, {'id': 'b', 'function': {'name': 'fs.propose_changes'}}]},
        {'role': 'tool', 'tool_call_id': 'a', 'content': 'a'},
        {'role': 'tool', 'tool_call_id': 'b', 'content': 'b'},
        {'role': 'assistant', '_working_notes': True, 'content': 'resume notes'}]}
    assert service._restored_working_messages({'id': 'run', 'model_id': 'model'}, saved, ('fs.read',)) == [saved['messages'][-1]]


def test_legacy_checkpoint_validates_results_before_restoring_notes():
    service = ChatRunService.__new__(ChatRunService)
    seen = []
    service._validate_saved_result_source = lambda run_id, result_id, **kwargs: seen.append((run_id, result_id))
    class Database:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, *args): return self
        def fetchall(self): return [{'operation_id': 'old-result'}]
    service.repository = SimpleNamespace(_connect=Database)
    saved = {'model_id': 'model', 'messages': [{'role': 'assistant', '_working_notes': True, 'content': 'old notes'}]}
    assert service._restored_working_messages({'id': 'run', 'model_id': 'model'}, saved, ()) == saved['messages']
    assert seen == [('run', 'old-result')]


def test_checkpoint_records_result_dependencies_at_save_time(tmp_path):
    core, tools, run, attempt = started_run(tmp_path)
    first = tools.create(run['id'], attempt['id'], 1, ToolCall('first', 'fs.read', {'path': 'a'}))
    store = ResultStore(core.database_path)
    store.save(first['id'], 'source data', success=True)
    service = ChatRunService.__new__(ChatRunService)
    service.repository = core
    service._save_working_context(run['id'], attempt['id'], [
        {'role': 'assistant', '_working_notes': True, 'content': 'derived notes'}])
    second = tools.create(run['id'], attempt['id'], 2, ToolCall('second', 'fs.read', {'path': 'b'}))
    store.save(second['id'], 'later source data', success=True)
    with core._connect() as connection:
        saved = json.loads(connection.execute('SELECT context_json FROM run_working_contexts WHERE run_id=?',
            (run['id'],)).fetchone()[0])
    assert saved['result_dependencies'] == [first['id']]


def test_restore_checks_shared_attachment_content_once(tmp_path):
    core, tools, run, attempt = started_run(tmp_path)
    attachment = {'id': '597922fb-90a2-41e6-8c48-3532e89563ef', 'sha256': 'source-hash'}
    store = ResultStore(core.database_path)
    dependencies = []
    for number in (1, 2):
        operation = tools.create(run['id'], attempt['id'], number,
            ToolCall(str(number), 'document.read_attachment', {'attachment_id': attachment['id'], 'offset': number}))
        store.save(operation['id'], '{"source_sha256":"source-hash"}', success=True)
        dependencies.append(operation['id'])
    checked = []
    service = ChatRunService.__new__(ChatRunService)
    service.repository = SimpleNamespace(get_model_run=lambda _: run, get_run_conversation=lambda _: {
        'messages': [{'id': run['user_message_id'], 'attachments': [attachment]}]})
    service.result_store = store
    service.document_attachments = SimpleNamespace(_original=lambda value: checked.append(value['id']))
    saved = {'model_id': run['model_id'], 'result_dependencies': dependencies,
        'messages': [{'role': 'assistant', '_working_notes': True, 'content': 'derived notes'}]}
    assert service._restored_working_messages(run, saved, ()) == saved['messages']
    assert checked == [attachment['id']]

def test_restore_source_cache_keeps_attachment_authorization_and_versions_distinct():
    service = ChatRunService.__new__(ChatRunService)
    attachments = [{'id': 'attachment-a', 'sha256': 'same-hash'},
                   {'id': 'attachment-b', 'sha256': 'same-hash'}]
    sources = {
        'first': {'tool': 'document.read_attachment', 'arguments': {'attachment_id': 'attachment-a'},
                  'range': {'offset': 0}, 'source_sha256': 'same-hash'},
        'other': {'tool': 'document.read_attachment', 'arguments': {'attachment_id': 'attachment-b'},
                  'range': {'offset': 0}, 'source_sha256': 'same-hash'},
        'stale': {'tool': 'document.read_attachment', 'arguments': {'attachment_id': 'attachment-a'},
                  'range': {'offset': 1}, 'source_sha256': 'old-hash'},
    }
    service.result_store = SimpleNamespace(metadata=lambda _run, result: {'source': sources[result]})
    service.repository = SimpleNamespace(get_model_run=lambda _: {'user_message_id': 'message'},
        get_run_conversation=lambda _: {'messages': [{'id': 'message', 'attachments': attachments}]})
    checked = []
    service.document_attachments = SimpleNamespace(_original=lambda attachment: checked.append(attachment['id']))
    validated = set()
    service._validate_saved_result_source('run', 'first', validated_sources=validated)
    service._validate_saved_result_source('run', 'other', validated_sources=validated)
    assert checked == ['attachment-a', 'attachment-b']
    with pytest.raises(CoreDataError) as error:
        service._validate_saved_result_source('run', 'stale', validated_sources=validated)
    assert error.value.code == 'attachment_changed'
