import asyncio

import pytest

from agent.runtime import ChatRunService
from agent.storage import CoreDataError, CoreRepository
from test_local_models_catalog import finish
from test_local_models_runtime import fake_runtime, imported


@pytest.mark.parametrize('configuration_changed', [False, True])
def test_interrupted_model_load_replays_same_job_and_root_after_service_restart(tmp_path, monkeypatch, configuration_changed):
    import test_local_models_runtime as fixtures
    marker = tmp_path / 'started'
    script = fixtures._SERVER.replace("if mode == 'slow':\n    time.sleep(30)",
        "if mode == 'slow':\n    from pathlib import Path\n    marker = Path(" + repr(str(marker)) + ")\n"
        "    if not marker.exists():\n        marker.write_text('started')\n        time.sleep(30)")
    monkeypatch.setattr(fixtures, '_SERVER', script)

    async def exercise():
        async def emit(value):
            pass

        runtime = fake_runtime(tmp_path / 'runtime', modes={'cpu': 'slow'})
        repository = CoreRepository(tmp_path / 'core.db')
        first = ChatRunService(repository, emit)
        first.local_models.runtime_root = runtime
        model = await imported(first.local_models, tmp_path / 'model.gguf')
        response = await first.handle_local_model('runtime.load', {'model_id': model['id']})
        for _ in range(500):
            if marker.exists():
                break
            await asyncio.sleep(.01)
        assert marker.exists()
        process = first.local_models._runtime.process
        await first.shutdown()
        assert process.poll() is not None
        root_id = response['execution_task_id']
        assert first.execution_monitor.get_task(root_id)['status'] == 'interrupted'

        restored = ChatRunService(CoreRepository(tmp_path / 'core.db'), emit)
        restored.local_models.runtime_root = runtime
        try:
            root = restored.execution_monitor.get_task(root_id)
            assert root['replay_kind'] == 'local_model_load'
            jobs = await restored.handle_local_model('job.list', {})
            saved = next(job for job in jobs['jobs'] if job['id'] == response['job']['id'])
            assert saved['execution_task_id'] == root_id
            if configuration_changed:
                record = restored.local_models.catalog.get(model['id'])
                restored.local_models.catalog.configure(model['id'], {'threads': record['threads'] + 1})
                with pytest.raises(CoreDataError) as error:
                    await restored.handle_execution('retry', {'task_id': root_id, 'revision': root['revision'],
                        'idempotency_key': 'resume-changed'})
                assert error.value.code == 'model_recovery_identity_changed'
                assert restored.execution_monitor.get_task(root_id)['status'] == 'interrupted'
            else:
                result = await asyncio.wait_for(restored.handle_execution('retry', {
                    'task_id': root_id, 'revision': root['revision'], 'idempotency_key': 'resume-load'}), 2)
                assert result['task']['id'] == root_id
                job = await finish(restored.local_models, response)
                assert job['status'] == 'completed', job.get('error')
                assert job['execution_task_id'] == root_id
                completed = restored.execution_monitor.get_task(root_id)
                assert completed['status'] == 'completed'
                assert len(completed['stages']) >= 2
                assert completed['stages'][0]['status'] == 'interrupted'
        finally:
            await restored.shutdown()

    asyncio.run(exercise())


def test_local_job_status_projects_paused_execution_without_changing_job_storage(tmp_path):
    async def exercise():
        async def emit(value):
            pass

        core = CoreRepository(tmp_path / 'core.db')
        service = ChatRunService(core, emit)
        try:
            with core._connect() as connection:
                connection.execute("INSERT INTO local_model_jobs(id,kind,model_id,status,progress,created_at,updated_at) VALUES ('projection','runtime.load','model','running',0,'2026-11-14','2026-11-14')")
            root = service.execution_monitor.start_task(run_id='local_model:projection')
            activity = service.execution_monitor._begin(root['id'], 'main_model_loading')
            service.execution_monitor._pause(activity, 'execution_stalled')
            job = (await service.handle_local_model('job.list', {}))['jobs'][0]
            assert job['status'] == 'waiting_recovery'
            assert job['execution_task_id'] == root['id']
            with core._connect() as connection:
                assert connection.execute("SELECT status FROM local_model_jobs WHERE id='projection'").fetchone()[0] == 'running'
        finally:
            await service.shutdown()

    asyncio.run(exercise())
