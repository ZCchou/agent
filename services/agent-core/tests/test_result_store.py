import json

import pytest

from agent.storage import CoreDataError
from test_tool_repository import started_run


def saved(tmp_path, text='原文\n' * 5000):
    from agent.execution.results import ResultStore
    from agent.tool_contracts import ToolCall
    core, tools, run, attempt = started_run(tmp_path)
    op = tools.create(run['id'], attempt['id'], 1, ToolCall('read', 'fs.read', {'path': 'a.txt'}))
    store = ResultStore(core.database_path)
    result = store.save(op['id'], text, success=True)
    return store, run, op, result


def test_full_result_survives_reload_and_cursor_is_bound_to_identity(tmp_path):
    from agent.execution.results import ResultStore
    store, run, op, result = saved(tmp_path)
    page = store.read(run['id'], result['result_id'], length=100)
    assert len(page['content']) == 100 and page['next_cursor']
    restored = ResultStore(store.database_path)
    assert restored.read(run['id'], result['result_id'], cursor=page['next_cursor'], length=100)['offset'] == 100
    with pytest.raises(CoreDataError):
        store.read('another-run', result['result_id'])
    with pytest.raises(CoreDataError):
        store.read(run['id'], result['result_id'], cursor='forged')
    with pytest.raises(CoreDataError, match='不可替换'):
        store.save(op['id'], 'changed', success=True)


def test_search_preserves_offsets_and_reports_incomplete_search(tmp_path):
    store, run, _, result = saved(tmp_path, 'abc 发动机 abc 发动机')
    found = store.search(run['id'], result['result_id'], '发动机', limit=1)
    assert found['matches'][0]['offset'] == 4
    assert found['truncated']
    assert store.read(run['id'], result['result_id'], length=100)['content'] == 'abc 发动机 abc 发动机'


def test_corrupt_result_is_rejected_not_silently_served(tmp_path):
    store, run, _, result = saved(tmp_path)
    store.path(result['sha256']).write_text('corrupt', encoding='utf-8')
    with pytest.raises(CoreDataError, match='校验'):
        store.read(run['id'], result['result_id'])


def test_envelope_is_valid_bounded_json(tmp_path):
    store, run, _, result = saved(tmp_path)
    output = store.envelope(result, max_characters=600)
    assert len(output) <= 600
    value = json.loads(output)
    assert value['result_id'] == result['result_id'] and value['complete']


def test_wrapped_result_cannot_bypass_revoked_evidence(tmp_path):
    from types import SimpleNamespace
    from agent.execution.results import ResultStore
    from agent.runtime import ChatRunService
    from agent.tool_contracts import ToolCall
    core, tools, run, attempt = started_run(tmp_path)
    store = ResultStore(core.database_path)
    original = tools.create(run['id'], attempt['id'], 1, ToolCall('evidence', 'rag.read', {'evidence_id': 'e'}))
    store.save(original['id'], 'protected original', success=True)
    wrapped = tools.create(run['id'], attempt['id'], 2, ToolCall('wrapper', 'tools.read_result', {'result_id': original['id']}))
    store.save(wrapped['id'], 'protected original', success=True)
    def revoked(_):
        raise CoreDataError('rag_revoked', 'revoked')
    service = SimpleNamespace(result_store=store, rag_grounding=SimpleNamespace(validate=revoked))
    service._validate_saved_result_source = ChatRunService._validate_saved_result_source.__get__(service)
    with pytest.raises(CoreDataError, match='revoked'):
        ChatRunService._saved_result_tool(service, run['id'], ToolCall('read', 'tools.read_result', {'result_id': wrapped['id']}))
