import asyncio
import time

import pytest

from agent.runtime import ChatRunService
from agent.storage import CoreDataError, CoreRepository
from test_local_models_catalog import finish
from test_local_models_runtime import imported
from test_managed_adaptive_recovery import recovering_runtime


def test_preload_receipt_survives_ready_and_links_intent_without_reviving_load_root(tmp_path, monkeypatch):
    async def exercise():
        async def emit(value):
            pass

        runtime, counter = recovering_runtime(tmp_path, monkeypatch, failures=0)
        core = CoreRepository(tmp_path / 'core.db')
        service = ChatRunService(core, emit)
        service.local_models.runtime_root = runtime
        model = await imported(service.local_models, tmp_path / 'model.gguf')

        async def classify(**params):
            return {'files': 'none', 'tools': 'none', 'web': 'off', 'web_evidence': 'optional',
                'reasoning': 'standard', 'task_kinds': ['general'], 'rag_requirement': 'not_needed',
                'explanation_code': 'preparation_receipt'}, None

        service._preflight_intent_advisor = classify
        try:
            loading = await service.handle_local_model('runtime.prepare', {'model_id': model['id']})
            assert (await finish(service.local_models, loading))['status'] == 'completed'
            service.local_models.set_tool_mode(model['provider_profile_id'], 'text_only')
            ready = await service.handle_local_model('runtime.prepare', {'model_id': model['id'],
                'preparation_id': loading['preparation_id']})
            assert ready['state'] == 'ready'
            assert ready['execution_task_id'] == loading['execution_task_id']
            request = {'content': 'hello', 'kind': 'standard_chat', 'category': 'casual',
                'provider_profile_id': model['provider_profile_id'], 'model_id': model['id'],
                'idempotency_key': 'prepared-intent', 'model_preparation_id': ready['preparation_id']}
            first = (await service.intent_sessions.handle('start', {'request': request}))['task']
            assert first['preload_execution_task_id'] == loading['execution_task_id']
            assert first['execution_task_id'] != loading['execution_task_id']
            await service.intent_sessions.wait(first['id'])
            task = service.intent_sessions.get(first['id'])
            assert task['status'] == 'completed', task.get('error')
            run_id = task['result']['run']['id']
            running = service.run_tasks.get(run_id)
            if running:
                await running
            root = service.execution_status({'owner_kind': 'run', 'owner_id': run_id})
            assert root['preload_execution_task_id'] == loading['execution_task_id']
            assert service.execution_monitor.get_task(loading['execution_task_id'])['status'] == 'completed'
            renewed = await service.handle_local_model('runtime.prepare', {'model_id': model['id']})
            duplicate = (await service.intent_sessions.handle('start', {'request': {
                **request, 'model_preparation_id': renewed['preparation_id']}}))['task']
            assert duplicate['id'] == first['id']
            assert duplicate['request']['model_preparation_id'] == ready['preparation_id']
        finally:
            await service.shutdown()

    asyncio.run(exercise())


def test_preload_receipt_rejects_expired_or_mismatched_lease_before_intent_admission(tmp_path):
    async def exercise():
        async def emit(value):
            pass

        core = CoreRepository(tmp_path / 'core.db')
        service = ChatRunService(core, emit)
        service.local_models._preparations['known'] = ('selected-model', time.monotonic() + 30)
        try:
            for key, preparation, model in [('unknown', 'absent', 'selected-model'),
                                             ('mismatch', 'known', 'other-model')]:
                with pytest.raises(CoreDataError):
                    await service.intent_sessions.handle('start', {'request': {'content': 'hello',
                        'idempotency_key': key, 'model_preparation_id': preparation,
                        'provider_profile_id': 'managed:' + model, 'model_id': model}})
            service.local_models._preparations['known'] = ('selected-model', time.monotonic() - 1)
            with pytest.raises(CoreDataError):
                await service.intent_sessions.handle('start', {'request': {'content': 'hello',
                    'idempotency_key': 'expired', 'model_preparation_id': 'known',
                    'provider_profile_id': 'managed:selected-model', 'model_id': 'selected-model'}})
            with core._connect() as connection:
                assert connection.execute('SELECT COUNT(*) FROM intent_tasks').fetchone()[0] == 0
        finally:
            await service.shutdown()

    asyncio.run(exercise())
