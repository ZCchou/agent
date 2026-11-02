import asyncio
import json

import pytest

from agent.protocol import handle_message, PROTOCOL_VERSION
from agent.storage import CoreRepository, CoreDataError
from agent.tool_contracts import ToolCall, validate_tool_call
from test_skills import package
from test_tool_continuation_retry import fixture, install_http, start


def call(identity, name, arguments):
    return {'tool_calls': [{'index': 0, 'id': identity, 'type': 'function',
                           'function': {'name': name, 'arguments': json.dumps(arguments)}}]}


def test_skill_management_protocol(tmp_path):
    repository = CoreRepository(tmp_path / 'data' / 'test.db')
    def request(kind, **params):
        result = handle_message({'v': PROTOCOL_VERSION, 'id': 'test', 'type': kind, 'params': params}, repository)
        assert 'error' not in result, result
        return result['result']
    item = request('skill.import', path=str(package(tmp_path)))['skill']
    assert request('skill.list')['skills'][0]['id'] == item['id']
    assert 'worked example' in request('skill.inspect', name='lesson')['skill']['content']
    request('skill.set_enabled', skill_id=item['id'], enabled=False)
    assert request('skill.list')['skills'][0]['enabled'] is False
    request('skill.remove', skill_id=item['id'])
    assert not request('skill.list')['skills']


def test_skill_tools_contracts():
    for name, args in [('skills.load', {'name': 'lesson'}),
                       ('skills.read_resource', {'name': 'lesson', 'path': 'references/example.md'})]:
        assert validate_tool_call(ToolCall('c', name, args)).name == name
        with pytest.raises(CoreDataError):
            validate_tool_call(ToolCall('c', name, {**args, 'execute': True}))


def test_model_loads_instruction_and_resource_without_host_and_finishes(tmp_path, monkeypatch):
    requests = install_http(monkeypatch, [
        (call('load', 'skills_load', {'name': 'lesson'}), 'tool_calls'),
        (call('read', 'skills_read_resource', {'name': 'lesson', 'path': 'references/example.md'}), 'tool_calls'),
        ({'content': 'Used the lesson and example.'}, 'stop'),
    ])
    async def scenario():
        async def sink(event):
            assert event['event_type'] != 'run.tool_requested', 'Core skill reads must not ask Host to execute'
        repository, _, work, service = await fixture(tmp_path, sink)
        service.skills.add(str(package(tmp_path)))
        run_id = await start(service, work)
        run = repository.get_model_run(run_id)
        assert run['status'] == 'completed', run.get('error_message')
        assert 'Used the lesson' in run['assistant_message']['content']
        assert [v['tool_name'] for v in service.list_tool_operations(run_id)] == ['skills.load', 'skills.read_resource']
        assert all(v['status'] == 'completed' for v in service.list_tool_operations(run_id))
        final = json.dumps(requests[-1][1]['messages'])
        assert 'worked example' in final and 'abcdef' in final
    asyncio.run(scenario())


def test_missing_skill_returns_feedback_then_normal_answer(tmp_path, monkeypatch):
    requests = install_http(monkeypatch, [
        (call('missing', 'skills_load', {'name': 'missing'}), 'tool_calls'),
        ({'content': 'The skill is unavailable; here is a general explanation.'}, 'stop'),
    ])
    async def scenario():
        async def sink(_event):
            pass
        repository, _, work, service = await fixture(tmp_path, sink)
        service.skills.add(str(package(tmp_path)))
        run_id = await start(service, work)
        run = repository.get_model_run(run_id)
        assert run['status'] == 'completed', run.get('error_message')
        assert 'unavailable' in run['assistant_message']['content']
        assert any(m.get('role') == 'tool' and 'ERROR skill_unavailable' in m['content']
                   for m in requests[-1][1]['messages'])
    asyncio.run(scenario())


@pytest.mark.parametrize('standard', [False, True])
def test_explicit_skill_reaches_text_only_route(tmp_path, monkeypatch, standard):
    from test_tool_continuation_retry import configuration
    requests = install_http(monkeypatch, [({'content': 'Here is a worked example.'}, 'stop')])
    async def scenario():
        async def sink(_event):
            pass
        config = configuration()
        config['model_tool_modes']['model'] = 'text_only'
        repository, _, work, service = await fixture(tmp_path, sink, config)
        if standard:
            work = repository.create_conversation('standard_chat', 'Lesson', category='casual')
            repository.update_conversation_routing(work['id'], 'fixed', 'balanced', 'synthetic', 'model')
        service.skills.add(str(package(tmp_path, policy=False)))
        response = await service.start(dict(conversation_id=work['id'], content='$lesson explain neurons',
            idempotency_key='manual', selection=dict(mode='fixed', provider_profile_id='synthetic', model_id='model')))
        await service.tasks[response['run']['id']]
        run = repository.get_model_run(response['run']['id'])
        assert run['status'] == 'completed', run.get('error_message')
        assert not requests[0][1].get('tools')
        assert 'Explain using a small worked example.' in json.dumps(requests[0][1]['messages'])
    asyncio.run(scenario())


def test_revocation_between_model_and_tool_returns_normal_feedback(tmp_path, monkeypatch):
    requests = install_http(monkeypatch, [
        (call('load', 'skills_load', {'name': 'lesson'}), 'tool_calls'),
        ({'content': 'The selected skill was disabled. I can explain directly.'}, 'stop'),
    ])
    async def scenario():
        async def sink(event):
            if event['event_type'] == 'run.invocation_completed':
                service.skills.set_enabled(item['id'], False)
        repository, _, work, service = await fixture(tmp_path, sink)
        item = service.skills.add(str(package(tmp_path)))
        run_id = await start(service, work)
        assert repository.get_model_run(run_id)['status'] == 'completed'
        assert 'disabled' in json.dumps(requests[-1][1]['messages'])
        assert service.list_tool_operations(run_id)[0]['status'] == 'failed'
    asyncio.run(scenario())


def test_skill_instructions_are_in_packed_context_and_token_accounting(tmp_path):
    from agent.runtime import ChatRunService
    from test_tool_repository import started_run
    repository, _, run, _ = started_run(tmp_path)
    async def sink(_event):
        pass
    service = ChatRunService(repository, sink)
    repository_content = '$lesson explain neurons'
    with repository._connect() as connection:
        connection.execute('UPDATE messages SET content=? WHERE id=?', (repository_content, run['user_message_id']))
    service.skills.add(str(package(tmp_path)))
    loaded = service.context.assemble(run, context_window=16000, max_output_tokens=1024)
    assert 'worked example' in json.dumps(loaded.messages)
    service.skills.set_enabled(service.skills.list()[0]['id'], False)
    disabled = service.context.assemble(run, context_window=16000, max_output_tokens=1024)
    assert loaded.estimated_tokens > disabled.estimated_tokens


def test_agent_can_create_a_skill_without_any_imported_packages(tmp_path, monkeypatch):
    requests = install_http(monkeypatch, [
        (call('save', 'skills_save', {'name': 'created', 'description': 'Explain lessons', 'content': 'Always start with an example.'}), 'tool_calls'),
        ({'content': 'The reusable lesson skill is saved.'}, 'stop'),
    ])
    async def scenario():
        async def sink(_event):
            pass
        repository, _, work, service = await fixture(tmp_path, sink)
        run_id = await start(service, work)
        assert repository.get_model_run(run_id)['status'] == 'completed'
        assert service.skills.load('created')['content'] == 'Always start with an example.'
        assert service.list_tool_operations(run_id)[0]['tool_name'] == 'skills.save'
        assert 'saved' in json.dumps(requests[-1][1]['messages']) or 'created' in json.dumps(requests[-1][1]['messages'])
    asyncio.run(scenario())


@pytest.mark.parametrize('stale', [False, True])
def test_agent_skill_update_is_versioned_and_conflicts_are_feedback(tmp_path, monkeypatch, stale):
    from agent.skills import SkillRegistry
    r = SkillRegistry(tmp_path / 'skills.json')
    item = r.save_skill('lesson', 'Teach concepts', 'Original instructions.')
    revision = '0' * 64 if stale else item['revision']
    requests = install_http(monkeypatch, [
        (call('update', 'skills_save', {'name': 'lesson', 'description': 'Teach concepts', 'content': 'Updated instructions.', 'expected_revision': revision}), 'tool_calls'),
        ({'content': 'Done.'}, 'stop'),
    ])
    async def scenario():
        async def sink(_event):
            pass
        repository, _, work, service = await fixture(tmp_path, sink)
        run_id = await start(service, work)
        assert repository.get_model_run(run_id)['status'] == 'completed'
        assert service.skills.load('lesson')['content'] == ('Original instructions.' if stale else 'Updated instructions.')
        assert service.list_tool_operations(run_id)[0]['status'] == ('failed' if stale else 'completed')
        if stale:
            assert 'skill_conflict' in json.dumps(requests[-1][1]['messages'])
    asyncio.run(scenario())


def test_local_skill_operations_consume_shared_budgets(tmp_path):
    from agent.tool_loop import ToolLoopService
    from test_tool_repository import started_run
    async def scenario():
        _, tools, run, attempt = started_run(tmp_path)
        async def sink(_event):
            pytest.fail('Internal skills must not be dispatched to Host')
        loop = ToolLoopService(tools, sink)
        loop.configure_limits({'reads': 1, 'mutations': 1})
        async def request(sequence, name, args):
            return await loop.request(run_id=run['id'], attempt_id=attempt['id'], sequence=sequence,
                call=ToolCall(str(sequence), name, args), local_executor=lambda: '{}')
        await request(1, 'skills.load', {'name': 'lesson'})
        with pytest.raises(CoreDataError) as read_error:
            await request(2, 'skills.load', {'name': 'lesson'})
        assert read_error.value.code == 'tool_read_limit_exceeded'
        await request(3, 'skills.save', {'name': 'lesson', 'description': 'Teach', 'content': 'Example'})
        with pytest.raises(CoreDataError) as write_error:
            await request(4, 'skills.save', {'name': 'other', 'description': 'Teach', 'content': 'Example'})
        assert write_error.value.code == 'tool_mutation_limit_exceeded'
        assert len(tools.list_for_run(run['id'])) == 2
    asyncio.run(scenario())
