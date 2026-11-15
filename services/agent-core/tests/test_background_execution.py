import asyncio

import pytest

from agent.execution.execution_wait import Diagnostic, ExecutionMonitor
from agent.storage import CoreDataError, CoreRepository
from test_execution_wait import Clock


@pytest.mark.parametrize('diagnostic_error', [False, True])
def test_unknown_activity_keeps_running_until_work_completes(tmp_path, diagnostic_error):
    clock = Clock()
    monitor = ExecutionMonitor(CoreRepository(tmp_path / 'agent.db'), clock=clock, sleep=clock.sleep)
    task = monitor.start_task(run_id='background')
    observed = []

    async def operation(activity):
        while clock.now - task['created_at'] < 650:
            observed.append(monitor.get_task(task['id'])['status'])
            activity.heartbeat()
            await asyncio.sleep(0)
        return 'done'

    def diagnose(activity):
        if diagnostic_error:
            raise OSError('status temporarily unavailable')
        return Diagnostic(activity_observed=None)

    assert asyncio.run(monitor.execute_stage(task['id'], 'load', operation, diagnose=diagnose)) == 'done'
    assert set(observed) == {'running'}


def test_recovery_exhaustion_returns_failure_without_user_wait(tmp_path):
    clock = Clock()
    monitor = ExecutionMonitor(CoreRepository(tmp_path / 'agent.db'), clock=clock, sleep=clock.sleep)
    task = monitor.start_task(run_id='worker')
    calls = []

    async def operation(activity):
        calls.append(activity.attempt)
        raise CoreDataError('rag_worker_crash', 'worker exited')

    async def scenario():
        with pytest.raises(CoreDataError) as error:
            await monitor.execute_stage(task['id'], 'rerank', operation,
                safe_to_retry=True, recoverable=lambda error: error.code == 'rag_worker_crash')
        assert error.value.code == 'rag_worker_crash'

    asyncio.run(scenario())
    assert calls == [1, 2, 3]
    assert monitor.get_task(task['id'])['status'] == 'failed'


def test_diagnostic_export_contains_actual_stage_and_counts(tmp_path):
    from agent.runtime import ChatRunService

    async def scenario():
        async def emit(event):
            pass

        service = ChatRunService(CoreRepository(tmp_path / 'core.db'), emit)
        root = service.execution_monitor.start_task(run_id='diagnostic')

        async def operation(activity):
            activity.progress(18, 40, stage='reranking', device='cuda', model_name='ranker')
            return 'done'

        try:
            await service.execution_monitor.execute_stage(root['id'], 'retrieval', operation)
            report = await service.handle_execution('diagnostics', {'task_id': root['id']})
            assert report['task']['current_stage']['completed'] == 18
            assert report['task']['current_stage']['total'] == 40
            assert report['activity'][-1]['payload']['device'] == 'cuda'
        finally:
            await service.shutdown()

    asyncio.run(scenario())
