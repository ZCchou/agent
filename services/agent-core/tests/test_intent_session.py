import asyncio

import pytest

from agent.storage import CoreDataError, CoreRepository


def test_intent_task_is_idempotent_and_persists_completion(tmp_path):
    from agent.execution.intent_session import IntentService, current_intent

    async def scenario():
        core = CoreRepository(tmp_path / 'agent.db')
        calls = []

        async def start(params):
            session = current_intent.get()
            calls.append(await session.classify(lambda mode: asyncio.sleep(0, result={'mode': mode})))
            return {'status': 'accepted', 'run': {'id': 'run'}, 'content': params['content']}

        service = IntentService(core, start)
        request = {'content': '读取附件', 'idempotency_key': 'same'}
        first = await service.handle('start', {'request': request, 'scope': 'draft'})
        second = await service.handle('start', {'request': request, 'scope': 'draft'})
        assert first['task']['id'] == second['task']['id']
        await service.wait(first['task']['id'])
        task = service.get(first['task']['id'])
        assert task['status'] == 'completed'
        assert task['attempts'] == 1 and task['timeout_seconds'] == 90
        assert task['result']['content'] == '读取附件'
        assert len(calls) == 1
        with pytest.raises(CoreDataError, match='request'):
            await service.handle('start', {'request': {**request, 'content': 'changed'}, 'scope': 'draft'})
        await service.close()
        restored = IntentService(core, start)
        assert restored.get(task['id'])['result'] == task['result']
        await restored.close()

    asyncio.run(scenario())


def test_two_timeout_calls_wait_for_manual_third_attempt(tmp_path):
    from agent.execution.intent_session import IntentService, current_intent

    async def scenario():
        count = 0

        async def classify(mode):
            nonlocal count
            count += 1
            if count < 3:
                raise TimeoutError('transport timeout')
            return {'task_type': 'attachment_read'}

        async def start(params):
            return {'status': 'accepted', 'intent': await current_intent.get().classify(classify)}

        service = IntentService(CoreRepository(tmp_path / 'agent.db'), start)
        task = (await service.handle('start', {'request': {'content': '提取', 'idempotency_key': 'retry'}}))['task']
        await service.wait(task['id'])
        task = service.get(task['id'])
        assert task['status'] == 'waiting_user'
        assert task['attempts'] == 2 and task['can_retry']
        assert task['error']['code'] == 'routing_intent_timeout'
        retried = await service.handle('retry', {'task_id': task['id'], 'revision': 1})
        assert retried['task']['status'] == 'queued'
        await service.wait(task['id'])
        task = service.get(task['id'])
        assert task['status'] == 'completed' and task['attempts'] == 3
        with pytest.raises(CoreDataError):
            await service.handle('retry', {'task_id': task['id'], 'revision': 1})
        await service.close()

    asyncio.run(scenario())


def test_clarification_revision_and_cancel_isolate_stale_results(tmp_path):
    from agent.execution.intent_session import IntentService, IntentClarification

    async def scenario():
        entered = asyncio.Event()

        async def start(params):
            if not params.get('_intent_answers'):
                raise IntentClarification('选择要读取的附件', [{'id': 'file', 'question': '使用哪份附件？',
                    'options': ['第一份', '第二份']}])
            entered.set()
            await asyncio.Future()

        service = IntentService(CoreRepository(tmp_path / 'agent.db'), start)
        task = (await service.handle('start', {'request': {'content': '分析文件', 'idempotency_key': 'q'}}))['task']
        await service.wait(task['id'])
        task = service.get(task['id'])
        assert task['clarification']['questions'][0]['question'] == '使用哪份附件？'
        answered = await service.handle('respond', {'task_id': task['id'], 'revision': 1,
            'answers': {'file': '第二份'}})
        assert answered['task']['revision'] == 2
        await entered.wait()
        with pytest.raises(CoreDataError):
            await service.handle('cancel', {'task_id': task['id'], 'revision': 1})
        await service.handle('cancel', {'task_id': task['id'], 'revision': 2})
        assert service.get(task['id'])['status'] == 'cancelled'
        await service.close()

    asyncio.run(scenario())


def test_format_retry_consumes_budget_and_extend_reuses_same_call(tmp_path):
    from agent.execution.intent_session import IntentService, current_intent

    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()
        modes = []

        async def classify(mode):
            modes.append(mode)
            if len(modes) == 1:
                raise ValueError('malformed JSON')
            entered.set()
            await release.wait()
            return {'ok': True}

        async def start(params):
            return {'result': await current_intent.get().classify(classify)}

        service = IntentService(CoreRepository(tmp_path / 'agent.db'), start)
        task = (await service.handle('start', {'request': {'content': '读取', 'idempotency_key': 'extend'}}))['task']
        await entered.wait()
        before = service.get(task['id'])
        assert before['attempts'] == 2 and before['can_extend']
        extended = (await service.handle('extend', {'task_id': task['id'], 'revision': 1}))['task']
        assert extended['remaining_seconds'] > before['remaining_seconds'] + 80
        assert extended['attempts'] == 2
        release.set()
        await service.wait(task['id'])
        assert modes == ['json_object', None]
        await service.close()

    asyncio.run(scenario())


def test_restart_interrupts_live_task_and_preserves_input(tmp_path):
    from agent.execution.intent_session import IntentService

    async def scenario():
        core = CoreRepository(tmp_path / 'agent.db')

        async def start(params):
            await asyncio.Future()

        service = IntentService(core, start)
        task = (await service.handle('start', {'request': {'content': '原消息', 'idempotency_key': 'restart'},
            'scope': 'work'}))['task']
        await asyncio.sleep(0)
        await service.close()
        restored = IntentService(core, start)
        state = restored.get(task['id'])
        assert state['status'] == 'interrupted' and state['can_retry']
        assert state['request']['content'] == '原消息'
        assert (await restored.handle('list', {'scope': 'work'}))['tasks'][0]['id'] == task['id']
        await restored.close()

    asyncio.run(scenario())


@pytest.mark.parametrize('code', ['connection_reset', 'stream_stalled', 'request_timeout', 'network_unavailable'])
def test_actual_transport_errors_auto_retry_then_allow_third_call(tmp_path, code):
    from agent.execution.intent_session import IntentService, current_intent
    from agent.providers import ProviderError
    async def scenario():
        calls = []
        async def classify(mode):
            calls.append(mode)
            if len(calls) < 3:
                raise ProviderError(code, 'Synthetic transport failure')
            return {'ok': True}
        async def start(params):
            return await current_intent.get().classify(classify)
        service = IntentService(CoreRepository(tmp_path / 'agent.db'), start)
        task = (await service.handle('start', {'request': {'content': '读取', 'idempotency_key': code}}))['task']
        await service.wait(task['id'])
        state = service.get(task['id'])
        assert state['attempts'] == 2 and state['can_retry']
        await service.handle('retry', {'task_id': state['id'], 'revision': 1})
        await service.wait(state['id'])
        assert service.get(state['id'])['status'] == 'completed'
        assert len(calls) == 3
        await service.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('code', ['resource_exhausted', 'model_not_found'])
def test_resource_error_allows_manual_retry_after_repair(tmp_path, code):
    from agent.execution.intent_session import IntentService, current_intent
    from agent.providers import ProviderError
    async def scenario():
        repaired = False
        async def classify(mode):
            if not repaired:
                raise ProviderError(code, 'Synthetic repairable model error')
            return {'ok': True}
        async def start(params):
            return await current_intent.get().classify(classify)
        service = IntentService(CoreRepository(tmp_path / 'agent.db'), start)
        state = (await service.handle('start', {'request': {'content': '读取', 'idempotency_key': 'repair'}}))['task']
        await service.wait(state['id'])
        assert service.get(state['id'])['attempts'] == 1
        assert service.get(state['id'])['can_retry']
        repaired = True
        await service.handle('retry', {'task_id': state['id'], 'revision': 1})
        await service.wait(state['id'])
        assert service.get(state['id'])['status'] == 'completed'
        await service.close()
    asyncio.run(scenario())


def test_abrupt_restart_charges_persisted_active_attempt_budget(tmp_path):
    import json
    import time
    from agent.execution.intent_session import IntentService
    async def scenario():
        core = CoreRepository(tmp_path / 'agent.db')
        async def start(params):
            await asyncio.Future()
        service = IntentService(core, start)
        state = (await service.handle('start', {'request': {'content': '读取', 'idempotency_key': 'crash'}}))['task']
        await service.close()
        with core._connect() as connection:
            state.update(status='identifying', attempts=1, active_seconds=5,
                attempt_started_at=time.time() - 170, attempt_wait_seconds=180)
            connection.execute('UPDATE intent_tasks SET state_json=? WHERE id=?', (json.dumps(state), state['id']))
        restored = IntentService(core, start)
        state = restored.get(state['id'])
        assert state['status'] == 'interrupted'
        assert 175 <= state['active_seconds'] <= 176
        assert 94 <= state['budget_remaining_seconds'] <= 95
        assert state.get('attempt_started_at') is None
        await restored.close()
    asyncio.run(scenario())
