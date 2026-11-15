import pytest

from agent.execution.execution_wait import ExecutionMonitor
from agent.execution.task_plans import TaskPlans
from agent.storage import CoreDataError, CoreRepository


def setup_task(tmp_path):
    core = CoreRepository(tmp_path / 'core.db')
    monitor = ExecutionMonitor(core)
    root = monitor.start_task(intent_task_id='request:goal')
    plans = TaskPlans(core)
    plans.create(root['id'], {'content': '审阅附件全部非空内容并保存DOCX报告',
                            'attachments': [{'id': 'source', 'name': '模板.docx'}]})
    return core, monitor, root, plans


def scope(operation='review', extent='full'):
    return {'source_id': 'source', 'operation': operation, 'scope': extent,
            'target_ids': [], 'reason': ''}


def requirements():
    return {'source_scopes': [scope()], 'deliverables': [{'path': None, 'format': 'docx'}],
            'success_criteria': ['审阅全部非空位置并交付DOCX报告']}


@pytest.mark.parametrize('field,value', [('operation', []), ('operation', {}), ('scope', []), ('scope', {})])
def test_malformed_goal_scope_reports_validation_error(tmp_path, field, value):
    _, _, root, plans = setup_task(tmp_path)
    goal = requirements()
    goal['source_scopes'][0][field] = value
    with pytest.raises(CoreDataError) as failure:
        plans.capture_requirements(root['id'], goal, expected_revision=0)
    assert failure.value.code == 'task_plan_invalid'


def test_metadata_declaration_cannot_complete_uncaptured_document_goal(tmp_path):
    _, _, root, plans = setup_task(tmp_path)
    plans.update(root['id'], expected_revision=0, steps=[], success_criteria=['审阅全部内容'],
                 source_scopes=[scope('read', 'metadata')], deliverables=[])
    assert plans.completion(root['id'], require_declared=True)['ready'] is False


def test_goal_scope_and_format_remain_required_when_plan_declares_metadata(tmp_path):
    _, _, root, plans = setup_task(tmp_path)
    assert hasattr(plans, 'capture_requirements'), 'Independent goal requirements are missing'
    plans.capture_requirements(root['id'], requirements(), expected_revision=0)
    plans.update(root['id'], expected_revision=0, steps=[], success_criteria=[],
                 source_scopes=[scope('read', 'metadata')], deliverables=[])
    result = plans.completion(root['id'], require_declared=True)
    assert not result['ready']
    assert {item['code'] for item in result['unmet_requirements']} >= {'source_missing', 'deliverable_missing'}


def test_plan_can_expand_metadata_into_full_review_and_keep_both_operations(tmp_path):
    _, _, root, plans = setup_task(tmp_path)
    plans.update(root['id'], expected_revision=0, steps=[], success_criteria=[],
                 source_scopes=[scope('read', 'metadata')], deliverables=[])
    expanded = plans.update(root['id'], expected_revision=1, steps=[], success_criteria=[],
                            source_scopes=[scope('read'), scope('review')], deliverables=[])
    assert expanded['revision'] == 2
    assert len(expanded['source_scopes']) == 2


def test_required_review_outcomes_survive_execution_plan_reordering(tmp_path):
    _, _, root, plans = setup_task(tmp_path)
    assert hasattr(plans, 'capture_requirements'), 'Independent goal requirements are missing'
    value = requirements()
    value['deliverables'] = []
    plans.capture_requirements(root['id'], value, expected_revision=0)
    plans.register_source(root['id'], 'source', 'review-source', 'hash', [{'id': 'p-0'}, {'id': 'p-1'}])
    plans.record_results(root['id'], 'review-source', 'hash', [{'target_id': 'p-0', 'status': 'reviewed'}])
    plans.update(root['id'], expected_revision=0, steps=[], success_criteria=[],
                 source_scopes=[scope('read', 'metadata')], deliverables=[])
    assert plans.completion(root['id'])['remaining'] == 1
    plans.record_results(root['id'], 'review-source', 'hash', [
        {'target_id': 'p-1', 'status': 'pending', 'gaps': ['缺少设备参数']}])
    result = plans.completion(root['id'])
    assert result['ready'] and result['gaps'] == 1


def test_goal_capture_is_revision_and_attempt_fenced(tmp_path):
    _, monitor, root, plans = setup_task(tmp_path)
    assert hasattr(plans, 'capture_requirements'), 'Independent goal requirements are missing'
    plans.capture_requirements(root['id'], requirements(), expected_revision=0)
    with pytest.raises(CoreDataError):
        plans.capture_requirements(root['id'], requirements(), expected_revision=0)
    monitor.finish_task(root['id'], 'failed')
    ended = monitor.get_task(root['id'])
    monitor.control(root['id'], 'retry', revision=ended['revision'], idempotency_key='retry')
    with pytest.raises(CoreDataError):
        plans.capture_requirements(root['id'], requirements(), expected_revision=1,
                                   fence=(root['revision'], root['attempt']))


def test_goal_proposal_and_plan_update_commit_together(tmp_path):
    _, _, root, plans = setup_task(tmp_path)
    assert hasattr(plans, 'capture_requirements'), 'Independent goal requirements are missing'
    with pytest.raises(CoreDataError):
        plans.update(root['id'], expected_revision=4, steps=[], success_criteria=[],
                     goal_requirements=requirements(), expected_goal_revision=0)
    assert plans.get(root['id'])['goal_requirements'] is None
    updated = plans.update(root['id'], expected_revision=0, steps=[], success_criteria=[],
                           goal_requirements=requirements(), expected_goal_revision=0)
    assert updated['goal_requirements']['revision'] == 1


def test_goal_rejects_unrelated_source_and_silent_scope_reduction(tmp_path):
    _, _, root, plans = setup_task(tmp_path)
    assert hasattr(plans, 'capture_requirements'), 'Independent goal requirements are missing'
    value = requirements()
    value['source_scopes'][0]['source_id'] = 'other'
    with pytest.raises(CoreDataError):
        plans.capture_requirements(root['id'], value, expected_revision=0)
    plans.capture_requirements(root['id'], requirements(), expected_revision=0)
    value = requirements()
    value['source_scopes'] = [scope('read', 'metadata')]
    with pytest.raises(CoreDataError):
        plans.capture_requirements(root['id'], value, expected_revision=1)


def test_captured_goal_is_checked_before_any_execution_plan_declaration(tmp_path):
    _, _, root, plans = setup_task(tmp_path)
    value = requirements()
    value['deliverables'] = []
    plans.capture_requirements(root['id'], value, expected_revision=0)
    assert not plans.completion(root['id'])['ready']


def test_model_updates_preserve_user_confirmed_requirement_floor(tmp_path):
    import asyncio
    from agent.execution.task_input import TaskInput
    async def scenario():
        core, _, root, plans = setup_task(tmp_path)
        plans.capture_requirements(root['id'], requirements(), expected_revision=0)
        inputs = TaskInput(core)
        asking = asyncio.create_task(inputs.ask(root['id'], '确认范围', [
            {'id': 'scope', 'question': '要审阅哪里？', 'options': ['全部非空内容']}]))
        await asyncio.sleep(0)
        pending = inputs.pending(root['id'])
        inputs.respond(root['id'], pending['id'], pending['revision'], {'scope': '全部非空内容，保存为确认报告.docx'})
        await asking
        confirmed = requirements()
        confirmed['deliverables'] = [{'path': '确认报告.docx', 'format': 'docx'}]
        plans.capture_requirements(root['id'], confirmed, expected_revision=1, user_change=True)
        plans.capture_requirements(root['id'], confirmed, expected_revision=2)
        reduced = requirements()
        reduced['source_scopes'] = [scope('read', 'metadata')]
        with pytest.raises(CoreDataError):
            plans.capture_requirements(root['id'], reduced, expected_revision=3, reconciled=True)
        for key, replacement in [('deliverables', [{'path': '别的报告.docx', 'format': 'docx'}]),
                                 ('success_criteria', [])]:
            changed = {**confirmed, key: replacement}
            with pytest.raises(CoreDataError):
                plans.capture_requirements(root['id'], changed, expected_revision=3)
    asyncio.run(scenario())


def test_one_publication_alias_cannot_satisfy_two_docx_deliverables():
    from agent.execution.goal_requirements import GoalRequirements
    outputs = [{'path': None, 'format': 'docx'}] * 2
    relative, absolute = '报告.docx', 'c:/project/报告.docx'
    matched, missing = GoalRequirements.match_outputs(outputs, {relative, absolute},
        {relative: 'docx', absolute: 'docx'}, aliases={relative: absolute})
    assert len(matched) == 1 and len(missing) == 1


def test_docx_requirement_checks_artifact_format_not_only_filename(tmp_path):
    from agent.documents import DocumentWorkflow
    core, monitor, root, plans = setup_task(tmp_path)
    project = core.create_project('Files', 'work')
    chat = core.create_conversation('project_work', 'Files', project_id=project['id'])
    run = core.start_model_run(content='保存DOCX', conversation_id=chat['id'],
        idempotency_key='file', provider_profile_id='local', model_id='model')
    monitor.bind_run(root['id'], run['id'])
    value = requirements()
    value['source_scopes'] = [scope('read', 'metadata')]
    plans.capture_requirements(root['id'], value, expected_revision=0)
    plans.update(root['id'], expected_revision=0, steps=[], success_criteria=[],
                 source_scopes=[scope('read', 'metadata')], deliverables=[])
    docs = DocumentWorkflow(core)
    wrong = docs.create(run['id'], '报告', 'markdown')
    docs.record_publication(run['id'], wrong['id'], {
        'status': 'published', 'path': '报告.docx', 'sha256': wrong['sha256']})
    assert not plans.completion(root['id'])['ready']
    correct = docs.create(run['id'], '报告', 'docx')
    docs.record_publication(run['id'], correct['id'], {
        'status': 'published', 'path': '报告.docx', 'sha256': correct['sha256']})
    assert plans.completion(root['id'])['ready']


@pytest.mark.parametrize('delete_after_export', [False, True])
def test_completion_uses_chronological_export_after_prior_file_changes(tmp_path, delete_after_export):
    from agent.documents import DocumentWorkflow
    from agent.tool_contracts import ToolCall
    from agent.tool_repository import ToolRepository
    core, monitor, root, plans = setup_task(tmp_path)
    project = core.create_project('Files', 'work')
    chat = core.create_conversation('project_work', 'Files', project_id=project['id'])
    run = core.start_model_run(content='保存DOCX', conversation_id=chat['id'],
        idempotency_key='export-order', provider_profile_id='local', model_id='model')
    attempt = core.begin_run_attempt(run['id'])
    monitor.bind_run(root['id'], run['id'])
    value = requirements()
    value['source_scopes'] = [scope('read', 'metadata')]
    value['deliverables'] = [{'path': '报告.docx', 'format': 'docx'}]
    plans.capture_requirements(root['id'], value, expected_revision=0)
    tools = ToolRepository(core.database_path)
    sequence = 0
    def complete(name, arguments):
        nonlocal sequence
        sequence += 1
        operation = tools.create(run['id'], attempt['id'], sequence,
            ToolCall('operation-' + str(sequence), name, arguments))
        tools.transition(operation['id'], {'requested'}, 'completed')
    complete('fs.propose_changes', {'title': '替换报告', 'mutations': [{'kind': 'delete', 'path': '报告.docx'}]})
    docs = DocumentWorkflow(core)
    artifact = docs.create(run['id'], '报告', 'docx')
    docs.record_publication(run['id'], artifact['id'], {
        'status': 'published', 'path': '报告.docx', 'sha256': artifact['sha256']})
    complete('document.export', {'artifact_id': artifact['id'], 'path': '报告.docx'})
    if delete_after_export:
        complete('fs.propose_changes', {'title': '删除报告', 'mutations': [{'kind': 'delete', 'path': '报告.docx'}]})
    assert plans.completion(root['id'])['ready'] is not delete_after_export
