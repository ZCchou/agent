import importlib.util

import pytest

from agent.execution.execution_wait import ExecutionMonitor
from agent.storage import CoreDataError, CoreRepository


def service(tmp_path, attachments=None):
    assert importlib.util.find_spec('agent.execution.task_plans'), 'Business task persistence is missing'
    from agent.execution.task_plans import TaskPlans
    core = CoreRepository(tmp_path / 'core.db')
    monitor = ExecutionMonitor(core)
    root = monitor.start_task(intent_task_id='request:original')
    plans = TaskPlans(core)
    plans.create(root['id'], {'content': '审阅附件并保存报告', 'attachments': [{'id': 'source'}] if attachments is None else attachments})
    return core, monitor, root, plans


def test_goal_survives_restart_and_duplicate_submission(tmp_path):
    core, _, root, plans = service(tmp_path)
    before = plans.get(root['id'])
    assert before['goal'] == '审阅附件并保存报告'
    plans.create(root['id'], {'content': '审阅附件并保存报告', 'attachments': [{'id': 'source'}]})
    from agent.execution.task_plans import TaskPlans
    assert TaskPlans(core).get(root['id']) == before


def test_plan_updates_are_revision_checked_and_keep_original_goal(tmp_path):
    _, _, root, plans = service(tmp_path)
    result = plans.update(root['id'], expected_revision=0,
        steps=[{'id': 'read', 'description': '读取附件', 'depends_on': []},
               {'id': 'review', 'description': '核对依据', 'depends_on': ['read']}],
        success_criteria=['每项都有审阅结果'])
    assert result['revision'] == 1
    assert result['goal'] == '审阅附件并保存报告'
    with pytest.raises(CoreDataError, match='修订'):
        plans.update(root['id'], expected_revision=0, steps=[], success_criteria=[])


def test_plan_rejects_cyclic_dependencies(tmp_path):
    _, _, root, plans = service(tmp_path)
    with pytest.raises(CoreDataError):
        plans.update(root['id'], expected_revision=0, steps=[
            {'id': 'a', 'description': 'a', 'depends_on': ['b']},
            {'id': 'b', 'description': 'b', 'depends_on': ['a']}], success_criteria=[])
    assert plans.get(root['id'])['revision'] == 0


def test_coverage_requires_result_for_every_registered_item(tmp_path):
    _, _, root, plans = service(tmp_path)
    plans.register_items(root['id'], 'document', 'hash-one', [
        {'id': 'p-0', 'text': '第一项'}, {'id': 'p-1', 'text': '第二项'}])
    plans.record_results(root['id'], 'document', 'hash-one', [
        {'target_id': 'p-0', 'status': 'reviewed', 'summary': '已核对'}])
    assert plans.completion(root['id'])['ready'] is False
    plans.record_results(root['id'], 'document', 'hash-one', [
        {'target_id': 'p-1', 'status': 'pending', 'gaps': ['缺少试验条件']}])
    result = plans.completion(root['id'])
    assert result == {'ready': True, 'total': 2, 'completed': 1, 'gaps': 1, 'remaining': 0}


def test_source_change_resets_only_affected_results_and_rejects_stale_writer(tmp_path):
    _, _, root, plans = service(tmp_path)
    plans.register_items(root['id'], 'document', 'old', [{'id': 'p-0', 'text': '旧内容'}])
    plans.record_results(root['id'], 'document', 'old', [{'target_id': 'p-0', 'status': 'reviewed'}])
    plans.register_items(root['id'], 'document', 'new', [{'id': 'p-0', 'text': '新内容'}])
    assert plans.completion(root['id'])['remaining'] == 1
    with pytest.raises(CoreDataError):
        plans.record_results(root['id'], 'document', 'old', [{'target_id': 'p-0', 'status': 'reviewed'}])


def test_cancelled_task_cannot_be_replanned(tmp_path):
    _, monitor, root, plans = service(tmp_path)
    monitor.control(root['id'], 'cancel', revision=root['revision'], idempotency_key='cancel')
    with pytest.raises(CoreDataError):
        plans.update(root['id'], expected_revision=0, steps=[], success_criteria=[])


def test_late_work_results_are_rejected_after_execution_revision_changes(tmp_path):
    _, monitor, root, plans = service(tmp_path)
    plans.register_items(root['id'], 'document', 'hash', [{'id': 'p-0'}])
    monitor.finish_task(root['id'], 'failed')
    ended = monitor.get_task(root['id'])
    monitor.control(root['id'], 'retry', revision=ended['revision'], idempotency_key='retry')
    with pytest.raises(CoreDataError):
        plans.record_results(root['id'], 'document', 'hash', [{'target_id': 'p-0', 'status': 'reviewed'}],
                             fence=(root['revision'], root['attempt']))
    assert plans.completion(root['id'])['remaining'] == 1


def test_old_plan_writer_cannot_update_after_execution_retry(tmp_path):
    _, monitor, root, plans = service(tmp_path)
    monitor.finish_task(root['id'], 'failed')
    ended = monitor.get_task(root['id'])
    monitor.control(root['id'], 'retry', revision=ended['revision'], idempotency_key='retry')
    with pytest.raises(CoreDataError):
        plans.update(root['id'], expected_revision=0, steps=[], success_criteria=[],
                     fence=(root['revision'], root['attempt']))
    assert plans.get(root['id'])['revision'] == 0


def test_technical_review_failure_stays_incomplete(tmp_path):
    _, _, root, plans = service(tmp_path)
    plans.register_items(root['id'], 'source', 'hash', [{'id': 'p-0'}])
    plans.record_results(root['id'], 'source', 'hash', [{
        'target_id': 'p-0', 'status': 'pending', 'error_code': 'document_review_invalid',
        'gaps': ['模型输出未通过校验']}])
    result = plans.completion(root['id'])
    assert result['ready'] is False
    assert result['remaining'] == 1 and result['gaps'] == 0


def test_required_source_scope_is_independent_of_review_selection(tmp_path):
    _, _, root, plans = service(tmp_path)
    plans.update(root['id'], expected_revision=0, steps=[], success_criteria=['全部位置'],
        source_scopes=[{'source_id': 'source', 'operation': 'review', 'scope': 'full', 'target_ids': [], 'reason': ''}],
        deliverables=[])
    assert plans.completion(root['id'])['ready'] is False
    plans.register_source(root['id'], 'source', 'artifact', 'hash', [
        {'id': 'p-0', 'text': '第一项'}, {'id': 'p-1', 'text': '第二项'}])
    plans.record_results(root['id'], 'artifact', 'hash', [{'target_id': 'p-0', 'status': 'reviewed'}])
    assert plans.completion(root['id'])['remaining'] == 1
    with pytest.raises(CoreDataError):
        plans.update(root['id'], expected_revision=1, steps=[], success_criteria=[],
            source_scopes=[], deliverables=[])


def test_declared_file_output_requires_actual_publication(tmp_path):
    core, monitor, root, plans = service(tmp_path, attachments=[])
    project = core.create_project('Files', 'work')
    conversation = core.create_conversation('project_work', 'Files', project_id=project['id'])
    run = core.start_model_run(content='保存报告', conversation_id=conversation['id'],
        idempotency_key='publication', provider_profile_id='provider', model_id='model')
    monitor.bind_run(root['id'], run['id'])
    plans.update(root['id'], expected_revision=0, steps=[], success_criteria=['报告已保存'],
                 source_scopes=[], deliverables=[{'path': '报告.md'}])
    assert plans.completion(root['id'])['remaining'] == 1
    from agent.documents import DocumentWorkflow
    documents = DocumentWorkflow(core)
    artifact = documents.create(run['id'], '真实报告内容', 'markdown')
    assert plans.completion(root['id'])['ready'] is False
    documents.record_publication(run['id'], artifact['id'], {
        'status': 'published', 'path': '报告.md', 'sha256': artifact['sha256']})
    assert plans.completion(root['id'])['ready'] is True


def test_project_file_read_requires_all_bytes_and_resets_changed_identity(tmp_path):
    _, _, root, plans = service(tmp_path, attachments=[])
    plans.record_file_read(root['id'], 'source.txt', 'hash', 12, 6, 12)
    plans.update(root['id'], expected_revision=0, steps=[], success_criteria=['全文读取'],
        source_scopes=[{'source_id': 'source.txt', 'operation': 'read', 'scope': 'full', 'target_ids': [], 'reason': ''}],
        deliverables=[])
    assert plans.completion(root['id'])['ready'] is False
    plans.record_file_read(root['id'], 'source.txt', 'hash', 12, 0, 6)
    assert plans.completion(root['id'])['ready'] is True
    plans.record_file_read(root['id'], 'source.txt', 'changed', 12, 0, 6)
    assert plans.completion(root['id'])['ready'] is False
