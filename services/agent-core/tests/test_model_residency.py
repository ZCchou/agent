import asyncio
from types import SimpleNamespace

import pytest

from agent.storage import CoreDataError, CoreRepository


def test_main_residence_defaults_to_thirty_minutes_and_changes_live(tmp_path):
    from agent.local_models.service import LocalModelService
    service = LocalModelService(CoreRepository(tmp_path / 'agent.db'))
    assert service.idle_timeout == 1800
    touched = []
    service._runtime = SimpleNamespace(idle_timeout=600, touch=lambda: touched.append(True))
    service.set_idle_timeout(0)
    assert service._runtime.idle_timeout == 0
    service.set_idle_timeout(3600)
    assert service.idle_timeout == 3600 and service._runtime.idle_timeout == 3600
    assert len(touched) == 2
    for value in (-1, True, 0.5, '60', 30, 86401):
        with pytest.raises(CoreDataError):
            service.set_idle_timeout(value)


def test_manual_residency_does_not_release_idle_model(monkeypatch):
    from agent.local_models.runtime import ManagedRuntime
    from agent.local_models import runtime as runtime_module

    async def scenario():
        runtime = ManagedRuntime(SimpleNamespace(), idle_timeout=0, permit=asyncio.Lock())
        runtime.session_id, runtime._state = 'session', 'loaded'
        runtime.process = SimpleNamespace(poll=lambda: None)
        runtime._last_used = -100000
        unloaded = []
        ticks = []

        async def unload():
            unloaded.append(True)
            runtime._state = 'unloaded'

        async def tick(delay):
            assert delay >= 1
            ticks.append(delay)
            if len(ticks) >= 2:
                runtime._state = 'unloaded'

        monkeypatch.setattr(runtime, 'unload', unload)
        monkeypatch.setattr(runtime_module.asyncio, 'sleep', tick)
        await runtime._release_idle('session')
        assert not unloaded

    asyncio.run(scenario())


def test_auxiliary_idle_sweep_respects_device_lock_and_manual_mode(tmp_path):
    from agent.rag.models import ModelStore

    async def scenario():
        store = ModelStore(tmp_path)
        closed = []

        async def close():
            closed.append(True)

        key = ('model', 'digest', 'cpu')
        worker = SimpleNamespace(close=close)
        store._resident_workers[key] = worker
        store._worker_last_used[key] = 0
        assert store.idle_timeout == 1800
        store.set_idle_timeout(60)
        store._worker_last_used[key] = 0
        async with store._device_locks['cpu']:
            await store._release_idle_workers(now=100)
            assert not closed
        store.set_idle_timeout(0)
        store._worker_last_used[key] = 0
        await store._release_idle_workers(now=100)
        assert not closed
        store.set_idle_timeout(60)
        store._worker_last_used[key] = 0
        await store._release_idle_workers(now=100)
        assert closed == [True] and key not in store._resident_workers
        await store.close()

    asyncio.run(scenario())
