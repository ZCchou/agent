import pytest

from agent.storage import CoreDataError, CoreRepository


@pytest.mark.parametrize('text,expected', [
    ('你好', False), ('你好！', False), ('谢谢', False), ('解释一下 Python 的列表', False),
    ('你好，依据资料库填写振动试验目的', True), ('DO-160G 第8章的要求是什么', True),
    ('这个设备是否符合适航要求', True), ('请依据项目资料填写模板', True),
    ('失效要求', True),
    ('温度试验的合格判据是什么', True),
    ('适航智能体如何启用RAG', False),
    ('如何在这个软件中导入DO-160G PDF', False),
    ('如何配置振动试验的量级和判据', True),
])
def test_automatic_evidence_decision(text, expected):
    from agent.rag.routing import decide_evidence
    result = decide_evidence(text, mode='auto')
    assert result['required'] is expected
    assert result['reason']


def test_forced_mode_and_required_skill_override_greeting():
    from agent.rag.routing import decide_evidence
    assert decide_evidence('你好', mode='required')['required']
    assert decide_evidence('你好', mode='auto', skill_required=True)['required']


def test_short_continuation_retains_previous_evidence_task():
    from agent.rag.routing import decide_evidence
    assert decide_evidence('继续', mode='auto', previous_evidence=True)['required']
    assert decide_evidence('你好', mode='auto', previous_evidence=True)['required'] is False
    assert decide_evidence('把刚才的回答保存为Word', mode='auto', previous_evidence=True)['reuse_evidence']
    assert decide_evidence('它能通过吗', mode='auto', previous_evidence=True)['required']
    assert decide_evidence('继续', pending_evidence=True)['required']


def test_mode_persists_and_run_captures_it_for_idempotency(tmp_path):
    core = CoreRepository(tmp_path / 'core.db')
    conversation = core.create_conversation('standard_chat', 'Test', category='casual')
    assert conversation['rag_mode'] == 'auto'
    core.update_conversation_rag(conversation['id'], 'required')
    run = core.start_model_run(content='你好', idempotency_key='same', provider_profile_id='p', model_id='m',
                               conversation_id=conversation['id'])
    assert run['rag_requested_mode'] == 'required'
    core.update_conversation_rag(conversation['id'], 'auto')
    assert core.get_model_run(run['id'])['rag_requested_mode'] == 'required'
    with pytest.raises(CoreDataError) as error:
        core.start_model_run(content='你好', idempotency_key='same', provider_profile_id='p', model_id='m',
                             conversation_id=conversation['id'], rag_mode='auto')
    assert error.value.code == 'idempotency_conflict'
    reopened = CoreRepository(core.database_path)
    assert reopened.get_conversation(conversation['id'])['rag_mode'] == 'auto'


def test_mode_inheritance_and_linked_retry_keep_captured_decision(tmp_path):
    core = CoreRepository(tmp_path / 'core.db')
    project = core.create_project('Project', 'work')
    chat = core.create_conversation('standard_chat', 'Chat', category='casual')
    core.update_conversation_rag(chat['id'], 'required')
    work = core.derive_work(chat['id'], project['id'])
    assert work['rag_mode'] == 'required'
    assert core.copy_work(work['id'], project['id'])['rag_mode'] == 'required'
    run = core.start_model_run(content='条款', idempotency_key='run', provider_profile_id='p', model_id='m', conversation_id=work['id'])
    core.record_rag_decision(run['id'], {'requested_mode': 'required', 'required': True, 'reason': 'user_required'})
    core.transition_model_run(run['id'], {'queued'}, 'failed')
    core.update_conversation_rag(work['id'], 'auto')
    retried = core.create_expanded_retry(run['id'])
    assert retried['rag_requested_mode'] == 'required'
    assert retried['rag_decision']['reason'] == 'user_required'


def test_real_schema28_upgrade_defaults_to_auto_and_preserves_history(tmp_path):
    core = CoreRepository(tmp_path / 'core.db')
    chat = core.create_conversation('standard_chat', 'Existing', category='casual')
    core._insert_message(chat['id'], 'user', 'Existing message')
    with core._connect() as connection:
        connection.execute('DROP TABLE document_artifacts')
        connection.execute('ALTER TABLE message_attachments DROP COLUMN purpose')
        connection.execute('ALTER TABLE conversations DROP COLUMN rag_mode')
        connection.execute('ALTER TABLE model_runs DROP COLUMN rag_requested_mode')
        connection.execute('ALTER TABLE model_runs DROP COLUMN rag_decision_json')
        connection.execute('UPDATE schema_info SET version=28')
    upgraded = CoreRepository(core.database_path)
    restored = upgraded.get_conversation(chat['id'])
    assert restored['rag_mode'] == 'auto'
    assert restored['messages'][0]['content'] == 'Existing message'
    with upgraded._connect() as connection:
        assert connection.execute('SELECT version FROM schema_info').fetchone()[0] == 30
        assert not connection.execute('PRAGMA foreign_key_check').fetchall()


def test_attachment_purposes_are_scoped_to_selected_files():
    from agent.rag.routing import resolve_attachment_roles
    selected = [{'id': 'template', 'name': '试验模板.docx'}, {'id': 'facts', 'name': '设备资料.docx'}]
    assert resolve_attachment_roles(selected) == {'template': 'template', 'facts': 'reference'}
    assert resolve_attachment_roles(selected, {'template': 'reference'})['template'] == 'reference'
    for invalid in [{'other': 'template'}, {'facts': []}, []]:
        with pytest.raises(CoreDataError):
            resolve_attachment_roles(selected, invalid)


@pytest.mark.parametrize('purpose', ['template', 'reference'])
def test_attachment_roles_remap_to_cloned_ids_and_survive_work_copy(tmp_path, purpose):
    core = CoreRepository(tmp_path / 'core.db')
    project = core.create_project('Project', 'work')
    work = core.create_conversation('project_work', 'Work', project_id=project['id'])
    attachment = {'id': 'source', 'kind': 'text', 'name': '模板.docx', 'media_type': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'byte_size': 1, 'sha256': 'a'*64, 'storage_key': 'message-attachments/source.docx', 'width': None, 'height': None, 'purpose': purpose}
    first = core.start_model_run(content='First', idempotency_key='first', provider_profile_id='p', model_id='m', conversation_id=work['id'], attachments=[attachment],
        rag_decision={'required': False, 'attachment_roles': {'source': purpose}})
    core.transition_model_run(first['id'], {'queued'}, 'failed')
    second = core.start_model_run(content='Reference', idempotency_key='second', provider_profile_id='p', model_id='m', conversation_id=work['id'], attachments=[attachment],
        rag_decision={'required': False, 'attachment_roles': {'source': purpose}})
    stored = core.get_run_conversation(second['id'])['messages'][0]['attachments'][0]
    assert stored['id'] != 'source'
    assert second['rag_decision']['attachment_roles'] == {stored['id']: purpose}
    copied = core.copy_work(work['id'], project['id'])
    assert copied['messages'][-1]['attachments'][0]['purpose'] == purpose
