"""Model-visible planning and live local capability discovery."""
from agent.document_contracts import definition
from agent.execution.task_plans import _steps, validate_requirements
from agent.execution.goal_requirements import validate_goal_requirements
from agent.storage import CoreDataError


TASK_TOOLS = ('tools.discover', 'task.update_plan', 'task.request_input')


def refresh_discovery_context(messages):
    """Replace completed capability directories while retaining other tool pairs."""
    superseded = {message['tool_call_id'] for message in messages
                  if message.get('role') == 'tool' and message.get('name') == 'tools.discover'}
    retained = []
    for message in messages:
        if message.get('role') == 'tool' and message.get('tool_call_id') in superseded:
            continue
        if message.get('role') == 'assistant' and message.get('tool_calls'):
            remaining = [call for call in message['tool_calls'] if call.get('id') not in superseded]
            message = {**message, 'tool_calls': remaining}
            if not remaining:
                message.pop('tool_calls')
                if not message.get('content'):
                    continue
        retained.append(message)
    messages[:] = retained


TASK_DEFINITIONS = [
    definition(TASK_TOOLS[0], 'Discover the currently authorized local tools. Omit names to list capabilities; select names to enable their schemas for the next turn.', {
        'names': {'type': 'array', 'maxItems': 32, 'items': {'type': 'string'}}}, []),
    definition(TASK_TOOLS[1], 'Update the current task plan after reading actual results. Preserve the user goal and scope. Read revision from task context or the previous result.', {
        'expected_revision': {'type': 'integer', 'minimum': 0},
        'steps': {'type': 'array', 'maxItems': 100, 'items': {
            'type': 'object', 'additionalProperties': False,
            'properties': {'id': {'type': 'string', 'maxLength': 128},
                'description': {'type': 'string', 'maxLength': 2000},
                'depends_on': {'type': 'array', 'items': {'type': 'string'}}},
            'required': ['id', 'description', 'depends_on']}},
        'source_scopes': {'type': 'array', 'maxItems': 32, 'items': {
            'type': 'object', 'additionalProperties': False, 'properties': {
                'source_id': {'type': 'string'}, 'operation': {'type': 'string', 'enum': ['read', 'review']},
                'scope': {'type': 'string', 'enum': ['full', 'selected', 'metadata', 'excluded']},
                'target_ids': {'type': 'array', 'items': {'type': 'string'}}, 'reason': {'type': 'string'}},
            'required': ['source_id', 'operation', 'scope', 'target_ids', 'reason']}},
        'deliverables': {'type': 'array', 'maxItems': 100, 'items': {'type': 'object',
            'properties': {'path': {'type': 'string'}}, 'required': ['path'], 'additionalProperties': False}},
        'success_criteria': {'type': 'array', 'maxItems': 100,
            'items': {'type': 'string', 'maxLength': 2000}}},
        ['expected_revision', 'steps', 'success_criteria']),
    definition(TASK_TOOLS[2], 'Ask for essential missing user information after inspecting available sources. The task waits for an explicit answer and then continues. Preserve choices the user already stated.', {
        'summary': {'type': 'string', 'maxLength': 2000},
        'questions': {'type': 'array', 'minItems': 1, 'maxItems': 3, 'items': {
            'type': 'object', 'additionalProperties': False,
            'properties': {'id': {'type': 'string'}, 'question': {'type': 'string'},
                'options': {'type': 'array', 'maxItems': 4, 'items': {'type': 'string'}}},
            'required': ['id', 'question', 'options']}}}, ['summary', 'questions']),
]

_plan_properties = TASK_DEFINITIONS[1]['function']['parameters']['properties']
_plan_properties['expected_goal_revision'] = {'type': 'integer', 'minimum': 0}
_plan_properties['goal_requirements'] = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'source_scopes': _plan_properties['source_scopes'],
        'deliverables': {'type': 'array', 'maxItems': 100, 'items': {
            'type': 'object', 'additionalProperties': False,
            'properties': {'path': {'type': ['string', 'null']}, 'format': {'type': ['string', 'null']}},
            'required': ['path', 'format']}},
        'success_criteria': _plan_properties['success_criteria'],
    }, 'required': ['source_scopes', 'deliverables', 'success_criteria'],
}


def validate_task_call(name, arguments):
    keys = ({'names'} if name == TASK_TOOLS[0] else {'summary', 'questions'} if name == TASK_TOOLS[2]
            else {'expected_revision', 'steps', 'success_criteria', 'source_scopes', 'deliverables',
                  'goal_requirements', 'expected_goal_revision'})
    if set(arguments) - keys:
        raise CoreDataError('invalid_tool_call', '任务工具包含未知参数。')
    if name == TASK_TOOLS[0]:
        names = arguments.get('names', [])
        if not isinstance(names, list) or len(names) > 32 or any(not isinstance(item, str) or not item for item in names):
            raise CoreDataError('invalid_tool_call', '工具名称清单无效。')
    elif name == TASK_TOOLS[2]:
        from agent.execution.intent_session import IntentClarification
        if set(arguments) != keys:
            raise CoreDataError('invalid_tool_call', '澄清参数不完整。')
        IntentClarification(arguments['summary'], arguments['questions'])
    else:
        if not {'expected_revision', 'steps', 'success_criteria'} <= arguments.keys() or type(arguments['expected_revision']) is not int or arguments['expected_revision'] < 0:
            raise CoreDataError('invalid_tool_call', '任务计划参数或修订无效。')
        _steps(arguments['steps'])
        criteria = arguments['success_criteria']
        if not isinstance(criteria, list) or len(criteria) > 100 or any(
                not isinstance(item, str) or not 1 <= len(item) <= 2000 for item in criteria):
            raise CoreDataError('invalid_tool_call', '任务完成条件无效。')
        validate_requirements(arguments.get('source_scopes', []), arguments.get('deliverables', []))
        if ('goal_requirements' in arguments) != ('expected_goal_revision' in arguments):
            raise CoreDataError('invalid_tool_call', '任务要求与要求修订号需要同时提交。')
        if 'goal_requirements' in arguments:
            if type(arguments['expected_goal_revision']) is not int or arguments['expected_goal_revision'] < 0:
                raise CoreDataError('invalid_tool_call', '任务要求修订号无效。')
            validate_goal_requirements(arguments['goal_requirements'])


def discover_tools(arguments, authorized):
    from agent.tool_contracts import ToolContractRegistry
    validate_task_call(TASK_TOOLS[0], arguments)
    names = list(dict.fromkeys(arguments.get('names', [])))
    if set(names) - set(authorized):
        raise CoreDataError('tool_not_allowed', '所选工具当前不可用，请查看本轮能力目录。')
    definitions = ToolContractRegistry.definitions(names or authorized)
    result = {'tools': [{'name': item['function']['name'], 'description': item['function']['description']}
                        for item in definitions], 'enabled': names}
    if names:
        result['available_names'] = sorted(authorized)
    return result
