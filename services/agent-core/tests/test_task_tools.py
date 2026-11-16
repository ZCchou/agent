import importlib.util

import pytest

from agent.storage import CoreDataError
from agent.tool_contracts import ToolCall, ToolContractRegistry, validate_tool_call


def test_planning_tools_are_executable_registered_contracts():
    for name, arguments in [
        ('tools.discover', {'names': ['fs.read']}),
        ('task.update_plan', {'expected_revision': 0, 'steps': [
            {'id': 'read', 'description': 'Read source', 'depends_on': []}],
            'success_criteria': ['Saved result can be opened']}),
    ]:
        assert validate_tool_call(ToolCall('call', name, arguments)).name == name
        assert ToolContractRegistry.definitions([name])[0]['function']['name'] == name


def test_discovery_returns_only_authorized_tools_and_supports_later_expansion():
    assert importlib.util.find_spec('agent.execution.task_tools')
    from agent.execution.task_tools import discover_tools
    catalog = discover_tools({}, {'fs.read', 'document.export'})
    assert {item['name'] for item in catalog['tools']} == {'fs.read', 'document.export'}
    assert catalog['enabled'] == []
    selected = discover_tools({'names': ['document.export']}, {'fs.read', 'document.export'})
    assert selected['enabled'] == ['document.export']
    assert [item['name'] for item in selected['tools']] == ['document.export']
    assert selected['available_names'] == ['document.export', 'fs.read']
    with pytest.raises(CoreDataError):
        discover_tools({'names': ['terminal.run']}, {'fs.read'})


def test_plan_tools_reject_unsupported_arguments():
    with pytest.raises(CoreDataError):
        validate_tool_call(ToolCall('call', 'task.update_plan', {
            'expected_revision': 0, 'steps': [], 'success_criteria': [], 'project_id': 'other'}))


def test_discovery_normalizes_native_aliases_before_authorization_and_schema_selection():
    from agent.execution.task_tools import discover_tools
    authorized = {'document.inspect_template', 'document.review_targets'}
    selected = discover_tools({'names': ['document_inspect_template', 'document_review_targets',
                                         'document.inspect_template']}, authorized)
    assert selected['enabled'] == ['document.inspect_template', 'document.review_targets']
    assert {tool['name'] for tool in selected['tools']} == authorized
    with pytest.raises(CoreDataError) as failure:
        discover_tools({'names': ['document_review_targets']}, {'document.inspect_template'})
    assert failure.value.code == 'tool_not_allowed'


def test_selected_document_review_is_a_composable_tool():
    call = validate_tool_call(ToolCall('review', 'document.review_targets', {
        'template_id': 'registered-source', 'target_ids': ['p-0'], 'format': 'markdown'}))
    assert call.arguments['target_ids'] == ['p-0']
    with pytest.raises(CoreDataError):
        validate_tool_call(ToolCall('review', 'document.review_targets', {
            'template_id': 'registered-source', 'target_ids': []}))


def test_goal_proposal_is_a_paired_optional_planning_argument():
    arguments = {'expected_revision': 0, 'steps': [], 'success_criteria': [],
        'expected_goal_revision': 0, 'goal_requirements': {
            'source_scopes': [], 'deliverables': [{'format': 'docx', 'path': None}],
            'success_criteria': ['完成Word文件']}}
    assert validate_tool_call(ToolCall('plan', 'task.update_plan', arguments)).name == 'task.update_plan'
    del arguments['expected_goal_revision']
    with pytest.raises(CoreDataError):
        validate_tool_call(ToolCall('plan', 'task.update_plan', arguments))
