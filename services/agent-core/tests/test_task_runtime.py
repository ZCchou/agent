import asyncio

import pytest

from agent.execution.execution_wait import current_task_id
from agent.runtime import ChatRunService
from agent.storage import CoreDataError, CoreRepository


def test_request_goal_is_saved_before_model_preparation_and_survives_failure(tmp_path):
    async def scenario():
        async def sink(event):
            pass
        core = CoreRepository(tmp_path / 'core.db')
        service = ChatRunService(core, sink)
        async def fail_after_registration(params):
            plan = service.task_plans.get(current_task_id.get())
            assert plan['goal'] == '检查附件中的问题'
            raise CoreDataError('model_not_loaded', 'model unavailable')
        service._start_monitored = fail_after_registration
        try:
            with pytest.raises(CoreDataError, match='model unavailable'):
                await service.start({'content': '检查附件中的问题', 'idempotency_key': 'request-1'})
            with core._connect() as connection:
                identity = connection.execute('SELECT task_id FROM task_plan_revisions').fetchone()[0]
            result = await service.handle_execution('plan', {'task_id': identity})
            assert result['plan']['goal'] == '检查附件中的问题'
            assert result['coverage']['remaining'] == 0
        finally:
            await service.shutdown()
    asyncio.run(scenario())


def test_verified_local_agent_combines_understanding_with_first_action(tmp_path, monkeypatch):
    from test_tool_continuation_retry import configuration, fixture, install_http
    requests = install_http(monkeypatch, [({'content': '这是普通解释。'}, 'stop')])
    async def scenario():
        async def sink(event):
            pass
        config = configuration()
        config['base_url'] = 'http://127.0.0.1:19999/v1'
        core, _, work, service = await fixture(tmp_path, sink, config)
        service.offline = True
        async def separate_classifier(**kwargs):
            raise AssertionError('Verified local agent should decide its first action in the agent loop')
        service._preflight_intent_advisor = separate_classifier
        try:
            response = await service.start({'conversation_id': work['id'], 'content': '解释普通文本的处理方法',
                'idempotency_key': 'combined-first-turn'})
            await service.tasks[response['run']['id']]
            assert core.get_model_run(response['run']['id'])['status'] == 'completed'
            assert len(requests) == 1
        finally:
            await service.shutdown()
    asyncio.run(scenario())


def test_eight_k_local_agent_can_discover_catalog_and_answer_in_same_run(tmp_path, monkeypatch):
    import json
    from test_tool_continuation_retry import configuration, fixture, install_http
    requests = install_http(monkeypatch, [
        ({'tool_calls': [{'index': 0, 'id': 'discover', 'type': 'function', 'function': {
            'name': 'tools_discover', 'arguments': json.dumps({})}}]}, 'tool_calls'),
        ({'content': '可用本地文件、文档、检索和任务工具。'}, 'stop'),
    ])
    async def scenario():
        async def sink(event):
            pass
        config = configuration()
        config['base_url'] = 'http://127.0.0.1:19999/v1'
        config['model_capabilities']['model']['context_length'] = 8192
        core, _, work, service = await fixture(tmp_path, sink, config)
        try:
            started = await service.start({'conversation_id': work['id'], 'content': '你有哪些实际可用工具？',
                'idempotency_key': 'small-catalog'})
            await service.tasks[started['run']['id']]
            run = core.get_model_run(started['run']['id'])
            assert run['status'] == 'completed', (run['error_code'], run['error_message'])
            assert len(requests) == 2
            assert 'document.export' in requests[-1][1]['messages'][-1]['content']
        finally:
            await service.shutdown()
    asyncio.run(scenario())


def test_model_discovers_document_capability_and_replans_in_same_run(tmp_path, monkeypatch):
    import json
    from test_tool_continuation_retry import fixture, install_http, start

    def call(name, arguments, identity):
        return {'tool_calls': [{'index': 0, 'id': identity, 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(arguments)}}]}

    requests = install_http(monkeypatch, [
        (call('tools_discover', {'names': ['document.inspect_template']}, 'discover'), 'tool_calls'),
        (call('task_update_plan', {'expected_revision': 0,
            'steps': [{'id': 'inspect', 'description': 'Inspect the supplied source', 'depends_on': []}],
            'success_criteria': ['Report verified findings']}, 'plan'), 'tool_calls'),
        ({'content': 'Available document inspection has been discovered.'}, 'stop'),
    ])

    async def scenario():
        async def sink(event):
            pass
        core, _, work, service = await fixture(tmp_path, sink)
        try:
            run_id = await start(service, work)
            run = core.get_model_run(run_id)
            assert run['status'] == 'completed', (run['error_code'], run['error_message'], requests)
            operations = service.tool_loop.repository.list_for_run(run_id)
            assert all(item['status'] == 'completed' for item in operations), requests[1][1]['messages']
            names = {item['function']['name'] for item in requests[1][1]['tools']}
            assert 'document_inspect_template' in names
            root = service.execution_status({'owner_kind': 'run', 'owner_id': run_id})
            assert root['plan']['revision'] == 1
            assert root['plan']['steps'][0]['id'] == 'inspect'
            assert [item['tool_name'] for item in operations] == ['tools.discover', 'task.update_plan']
            assert all(item['status'] == 'completed' for item in operations)
        finally:
            await service.shutdown()
    asyncio.run(scenario())


def test_native_task_asks_then_continues_same_run_with_explicit_answer(tmp_path, monkeypatch):
    import json
    from test_tool_continuation_retry import fixture, install_http
    question = {'summary': '需要设备型号', 'questions': [
        {'id': 'equipment', 'question': '设备型号是什么？', 'options': ['设备 A', '设备 B']}]}
    requests = install_http(monkeypatch, [
        ({'tool_calls': [{'index': 0, 'id': 'clarify', 'type': 'function', 'function': {
            'name': 'task_request_input', 'arguments': json.dumps(question)}}]}, 'tool_calls'),
        ({'content': '已按设备 C 继续原任务。'}, 'stop'),
    ])
    async def scenario():
        async def sink(event):
            pass
        core, _, work, service = await fixture(tmp_path, sink)
        try:
            response = await service.start({'conversation_id': work['id'], 'content': '请安排设备试验',
                'idempotency_key': 'runtime-question'})
            run_id = response['run']['id']
            for _ in range(200):
                snapshot = service.execution_status({'owner_kind': 'run', 'owner_id': run_id})
                if snapshot.get('pending_question'):
                    break
                if service.tasks[run_id].done():
                    pytest.fail(str(core.get_model_run(run_id)))
                await asyncio.sleep(0.01)
            pending = snapshot['pending_question']
            assert pending['clarification']['summary'] == '需要设备型号'
            assert len(requests) == 1
            await service.handle_execution('respond', {'task_id': snapshot['id'],
                'question_id': pending['id'], 'question_revision': pending['revision'], 'answers': {'equipment': '设备 C'}})
            await service.tasks[run_id]
            assert core.get_model_run(run_id)['status'] == 'completed'
            assert len(requests) == 2
            assert '设备 C' in requests[1][1]['messages'][-1]['content']
        finally:
            await service.shutdown()
    asyncio.run(scenario())
