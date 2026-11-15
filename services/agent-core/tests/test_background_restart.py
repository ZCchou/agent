import asyncio
import json
import pytest

from agent.runtime import ChatRunService
from agent.storage import CoreRepository


@pytest.mark.parametrize('retry_fails', [False, True])
def test_resumed_intent_holds_model_pin_until_its_classifier_finishes(tmp_path, retry_fails):
    async def scenario():
        async def emit(event):
            pass
        core = CoreRepository(tmp_path / 'core.db')
        old = ChatRunService(core, emit)
        entered = asyncio.Event()
        async def pending(request):
            entered.set()
            await asyncio.Event().wait()
        old.intent_sessions.start = pending
        initial = await old.intent_sessions.handle('start', {'scope': 'draft', 'request': {
            'content': 'list files', 'provider_profile_id': 'managed:model', 'idempotency_key': 'pin-restart'}})
        await entered.wait()
        await old.shutdown()
        service = ChatRunService(core, emit)
        state = service.intent_sessions._read(initial['task']['id'])
        pin = 'execution:' + state['execution_task_id']
        admissions = []
        async def ready(provider_id, *, pin_id=None):
            admissions.append(pin_id)
            if pin_id:
                service.local_models._run_pins[pin_id] = 'model'
        service.local_models.await_ready = ready
        service._sync_managed_profiles = lambda: None
        async def complete(request):
            assert service.local_models._run_pins.get(pin) == 'model'
            service.local_models.release_run(pin)
            return {'status': 'preflight_rejected', 'rejection': {}}
        service.intent_sessions.start = complete
        if retry_fails:
            async def reject(*args):
                raise RuntimeError('retry rejected before classification')
            service.intent_sessions.handle = reject
        try:
            await service.resume_background()
            if not retry_fails:
                await service.intent_sessions.wait(state['id'])
            assert admissions == [pin]
            assert pin not in service.local_models._run_pins
        finally:
            await service.shutdown()
    asyncio.run(scenario())


def test_restart_resumes_retained_intent_without_old_preparation_lease(tmp_path):
    async def scenario():
        async def emit(event):
            pass

        core = CoreRepository(tmp_path / 'core.db')
        old = ChatRunService(core, emit)
        entered = asyncio.Event()

        async def wait(request):
            entered.set()
            await asyncio.Event().wait()

        old.intent_sessions.start = wait
        initial = await old.intent_sessions.handle('start', {'scope': 'draft', 'request': {
            'content': 'hello', 'kind': 'standard_chat', 'category': 'casual', 'idempotency_key': 'restart'}})
        await entered.wait()
        await old.shutdown()
        new = ChatRunService(core, emit)
        requests = []

        async def complete(request):
            requests.append(request)
            return {'status': 'preflight_rejected', 'rejection': {}}

        new.intent_sessions.start = complete
        try:
            await new.resume_background()
            await new.intent_sessions.wait(initial['task']['id'])
            assert new.intent_sessions.get(initial['task']['id'])['status'] == 'completed'
            assert len(requests) == 1
            await new.resume_background()
            assert len(requests) == 1
        finally:
            await new.shutdown()

    asyncio.run(scenario())


def test_interrupted_probe_resumes_before_its_waiting_intent(tmp_path):
    async def scenario():
        async def emit(event):
            pass

        core = CoreRepository(tmp_path / 'core.db')
        old = ChatRunService(core, emit)
        entered = asyncio.Event()

        async def pending(request):
            entered.set()
            await asyncio.Event().wait()

        old.intent_sessions.start = pending
        initial = await old.intent_sessions.handle('start', {'scope': 'draft', 'request': {
            'content': 'list files', 'provider_profile_id': 'test', 'idempotency_key': 'shared-probe-restart'}})
        await entered.wait()
        probe_root = old.execution_monitor.start_task(run_id='tool-probe:old')
        old.execution_monitor.set_replay(probe_root['id'], 'tool_probe', {'provider_id': 'test', 'model_id': 'model'})
        old.tool_verification._save('identity', 'probing')
        await old.shutdown()
        service = ChatRunService(core, emit)
        order = []

        def start_probe(provider_id, model_id, *, root_id=None):
            async def complete():
                await asyncio.sleep(0)
                order.append('probe')
                service.tool_verification._save('identity', 'verified', 'native')
                service.execution_monitor.finish_task(root_id)
            service.tool_probe_tasks[provider_id] = asyncio.create_task(complete())
            return {'status': 'probing'}

        async def resume(request):
            async def unexpected():
                raise AssertionError('completed probe should be reused')
            await service.tool_verification.ensure('identity', unexpected)
            order.append('intent')
            return {'status': 'preflight_rejected', 'rejection': {}}

        service.start_tool_probe = start_probe
        service.intent_sessions.start = resume
        try:
            await service.resume_background()
            assert service.intent_sessions.get(initial['task']['id'])['status'] == 'completed'
            assert order == ['probe', 'intent']
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_cancelling_one_resumed_run_does_not_cancel_other_recovery(tmp_path):
    async def scenario():
        async def emit(event):
            pass

        core = CoreRepository(tmp_path / 'core.db')
        service = ChatRunService(core, emit)
        runs = []
        for index in range(2):
            chat = core.create_conversation('standard_chat', str(index), category='casual')
            run = core.start_model_run(content='hello', idempotency_key=str(index), conversation_id=chat['id'],
                                      provider_profile_id='builtin-demo', model_id='demo')
            core.transition_model_run(run['id'], {'queued'}, 'interrupted', 'core_interrupted')
            root = service.execution_monitor.start_task(run_id=run['id'])
            service.execution_monitor.interrupt_active()
            runs.append(run['id'])
        calls = []

        async def resume(run_id):
            calls.append(run_id)
            task = asyncio.create_task(asyncio.sleep(0))
            service.run_tasks[run_id] = task
            if len(calls) == 1:
                task.cancel()

        service.retry = resume
        try:
            await service.resume_background()
            assert calls == runs
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_waiting_restored_task_does_not_block_registration_of_later_runs(tmp_path):
    async def scenario():
        async def emit(event):
            pass
        core = CoreRepository(tmp_path / 'core.db')
        service = ChatRunService(core, emit)
        runs, calls = [], []
        gate = asyncio.Event()
        for index in range(2):
            chat = core.create_conversation('standard_chat', str(index), category='casual')
            run = core.start_model_run(content='resume', idempotency_key=str(index), conversation_id=chat['id'],
                provider_profile_id='builtin-demo', model_id='demo')
            core.transition_model_run(run['id'], {'queued'}, 'interrupted', 'core_interrupted')
            service.execution_monitor.start_task(run_id=run['id'])
            runs.append(run['id'])
        service.execution_monitor.interrupt_active()
        async def resume(run_id):
            calls.append(run_id)
            service.run_tasks[run_id] = asyncio.create_task(gate.wait())
        service.retry = resume
        resuming = asyncio.create_task(service.resume_background())
        try:
            for _ in range(100):
                if len(calls) == 2:
                    break
                await asyncio.sleep(.01)
            assert calls == runs
        finally:
            gate.set()
            await asyncio.wait_for(resuming, 5)
            await service.shutdown()
    asyncio.run(scenario())
