import sqlite3

from agent.storage import CoreRepository


def seed(path):
    repo = CoreRepository(path)
    project = repo.create_project('Project', 'work')
    chat = repo.create_conversation('project_work', 'Chat', project_id=project['id'])
    repo._insert_message(chat['id'], 'user', 'Original question')
    return repo, project, chat


def snapshot(source, destination):
    with sqlite3.connect(source) as src, sqlite3.connect(destination) as dst:
        src.backup(dst)


def test_identical_graph_deduplicates_and_repeated_source_is_idempotent(tmp_path):
    from agent.profile_merge import merge_snapshots
    target = tmp_path / 'target.db'
    source = tmp_path / 'source.db'
    repo, project, chat = seed(target)
    snapshot(target, source)
    report = merge_snapshots(target, source, source_label='OCR Preview')
    assert not report['id_map']
    assert len(repo.list_projects()) == 1
    assert merge_snapshots(target, source, source_label='OCR Preview')['migration_id'] == report['migration_id']


def test_conflicting_graph_preserves_both_parent_project_and_history(tmp_path):
    from agent.profile_merge import merge_snapshots
    target = tmp_path / 'target.db'
    source = tmp_path / 'source.db'
    repo, project, chat = seed(target)
    snapshot(target, source)
    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE messages SET content='Changed source question'")
    report = merge_snapshots(target, source, source_label='OCR Preview')
    projects = repo.list_projects()
    assert len(projects) == 2
    assert any('OCR Preview' in item['name'] for item in projects)
    copied_chat = repo.get_conversation(report['id_map'][chat['id']])
    assert copied_chat['project_id'] == report['id_map'][project['id']]
    assert copied_chat['messages'][0]['content'] == 'Changed source question'
    assert repo.get_conversation(chat['id'])['messages'][0]['content'] == 'Original question'
    with sqlite3.connect(target) as connection:
        assert not connection.execute('PRAGMA foreign_key_check').fetchall()
    CoreRepository(source).update_global_instructions('Changed preference only.', enabled=True)
    repeated = merge_snapshots(target, source, source_label='OCR Preview')
    assert repeated['id_map'] == report['id_map']
    assert len(repo.list_projects()) == 2


def test_reference_rewrite_changes_managed_links_and_json_keys_without_touching_quotes():
    from agent.profile_merge import rewrite_references
    mapping = {'rag-old': 'rag-new', 'attachment-old': 'attachment-new'}
    assert rewrite_references('[source](rag-evidence:rag-old)', mapping) == '[source](rag-evidence:rag-new)'
    assert rewrite_references('The document says rag-old.', mapping) == 'The document says rag-old.'
    value = {'attachment_roles': {'attachment-old': 'template'}, 'evidence': [{'id': 'rag-old', 'text': 'quoted rag-old'}]}
    assert rewrite_references(value, mapping) == {'attachment_roles': {'attachment-new': 'template'}, 'evidence': [{'id': 'rag-new', 'text': 'quoted rag-old'}]}


def test_conflicting_run_gets_independent_idempotency_and_cancel_rolls_back(tmp_path):
    from agent.profile_merge import merge_snapshots
    target, source = tmp_path / 'target.db', tmp_path / 'source.db'
    repo, project, chat = seed(target)
    repo.start_model_run(content='Run question', idempotency_key='same-request', provider_profile_id='p', model_id='m', conversation_id=chat['id'])
    snapshot(target, source)
    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE messages SET content='Source question' WHERE role='user'")
    report = merge_snapshots(target, source, source_label='OCR Preview')
    with sqlite3.connect(target) as connection:
        assert connection.execute('SELECT count(DISTINCT idempotency_key) FROM model_runs').fetchone()[0] == 2
        assert not connection.execute('PRAGMA foreign_key_check').fetchall()
    def cancelled():
        raise InterruptedError('cancelled')
    import pytest
    with pytest.raises(InterruptedError):
        merge_snapshots(target, source, source_label='Another source', check_cancel=cancelled)
    with sqlite3.connect(target) as connection:
        assert connection.execute('SELECT count(*) FROM profile_migration_sources').fetchone()[0] == 1


def test_global_change_is_a_new_source_and_primary_default_model_is_preserved(tmp_path):
    from agent.profile_merge import merge_snapshots
    target, source = tmp_path / 'target.db', tmp_path / 'source.db'
    repo, project, chat = seed(target)
    record = dict(id='a'*64, provider_profile_id='managed:a', name='Preferred', architecture='qwen3',
                  quantization='Q4_K_M', context_length=8192, max_context_length=32768, size_bytes=3,
                  digest='a'*64, template_digest='b'*64, adapter_digest='c'*64, threads=2,
                  status='imported', is_default=1, files_json='[]', created_at='2026-11-11')
    columns = ','.join(record)
    placeholders = ','.join('?' for _ in record)
    with repo._connect() as connection:
        connection.execute(f'INSERT INTO local_models ({columns}) VALUES ({placeholders})', tuple(record.values()))
    snapshot(target, source)
    first = merge_snapshots(target, source, source_label='OCR Preview')
    source_repo = CoreRepository(source)
    source_repo.update_global_instructions('Use the source preference.', enabled=True)
    with source_repo._connect() as connection:
        connection.execute('UPDATE local_models SET is_default=0')
        other = {**record, 'id': 'd'*64, 'provider_profile_id': 'managed:d', 'digest': 'd'*64}
        connection.execute(f'INSERT INTO local_models ({columns}) VALUES ({placeholders})', tuple(other.values()))
    second = merge_snapshots(target, source, source_label='OCR Preview')
    assert first['source_digest'] != second['source_digest']
    with repo._connect() as connection:
        assert connection.execute('SELECT id FROM local_models WHERE is_default=1').fetchone()[0] == record['id']
        assert connection.execute('SELECT count(*) FROM local_models').fetchone()[0] == 2


def test_attachment_original_and_model_suffix_paths_follow_the_same_identity():
    from agent.profile_merge import rewrite_storage_key, rewrite_references
    old, new = 'ab123456-1234-1234-1234-123456789012', 'cd123456-1234-1234-1234-123456789012'
    mapping = {old: new}
    assert rewrite_storage_key(f'message-attachments/{old}-model.png', mapping) == f'message-attachments/{new}-model.png'
    assert rewrite_references({'storage_key': f'message-attachments/{old}.docx', 'name': old}, mapping) == {
        'storage_key': f'message-attachments/{new}.docx', 'name': old}


def test_rewritten_tool_and_artifact_metadata_keep_valid_hashes_and_noop_bytes():
    from agent.profile_merge import _remap_row
    from agent.tool_contracts import canonical_arguments
    import hashlib
    import json
    arguments, digest = canonical_arguments({'attachment_id': 'old', 'text': '原文'})
    summary = json.dumps({'artifact_id': 'old'}, ensure_ascii=False)
    values = {'arguments_json': arguments, 'arguments_hash': digest, 'result_summary': summary,
              'result_hash': hashlib.sha256(summary.encode()).hexdigest()}
    assert _remap_row('tool_operations', values, {}) == values
    rewritten = _remap_row('tool_operations', values, {'old': 'new'})
    assert (rewritten['arguments_json'], rewritten['arguments_hash']) == canonical_arguments({'attachment_id': 'new', 'text': '原文'})
    assert json.loads(rewritten['result_summary'])['artifact_id'] == 'new'
    assert rewritten['result_hash'] == hashlib.sha256(rewritten['result_summary'].encode()).hexdigest()
    artifact = _remap_row('execution_artifacts', {'content_json': summary, 'content_hash': values['result_hash'], 'byte_size': len(summary)}, {'old': 'new'})
    assert artifact['content_hash'] == hashlib.sha256(artifact['content_json'].encode()).hexdigest()
    assert artifact['byte_size'] == len(artifact['content_json'].encode())


def test_remapped_plan_preserves_valid_attachment_selection_binding():
    import json
    from agent.execution.domain import stable_hash
    from agent.context_engine.attachment_references import validate_saved_selection
    from agent.profile_merge import _remap_row
    intent = {'effective_requirements': {'request': '填写模板'},
              'effective_attachment_ids': ['old'],
              'attachment_selection': [{'current_attachment_id': 'old', 'source_attachment_id': 'old',
                                        'source_start': 0, 'source_end': 20}]}
    requirements_hash = stable_hash({'requirements_hash': stable_hash(intent['effective_requirements']),
                                     'attachment_selection': intent['attachment_selection']})
    values = {'resolved_intent_json': json.dumps(intent), 'resolution_hash': stable_hash(intent),
              'requirements_hash': requirements_hash}
    assert _remap_row('execution_plans', values, {}) == values
    rewritten = _remap_row('execution_plans', values, {'old': 'new'})
    selection = validate_saved_selection(rewritten, json.loads(rewritten['resolved_intent_json']))
    assert selection[0]['current_attachment_id'] == 'new'
    import pytest
    from agent.storage import CoreDataError
    with pytest.raises(CoreDataError, match='integrity validation'):
        _remap_row('execution_plans', {**values, 'requirements_hash': 'tampered'}, {'old': 'new'})
