import asyncio
import pytest

from agent.storage import CoreDataError


def test_primary_gpu_load_releases_auxiliary_residency_and_serializes(tmp_path):
    from agent.gpu_resources import GpuResources
    async def scenario():
        calls = []
        async def release():
            calls.append("release")
        gpu = GpuResources(release, probe=lambda: {"free_bytes": 8 * 1024**3})
        entered, finish = asyncio.Event(), asyncio.Event()
        async def primary():
            async with gpu.primary({"device": "cuda"}):
                calls.append("primary")
                entered.set()
                await finish.wait()
        async def auxiliary():
            async with gpu.auxiliary({"device": "cuda", "size_bytes": 1024}, resident=False):
                calls.append("auxiliary")
        task = asyncio.create_task(primary())
        await entered.wait()
        other = asyncio.create_task(auxiliary())
        await asyncio.sleep(0)
        assert calls == ["release", "primary"]
        finish.set()
        await asyncio.gather(task, other)
        assert calls == ["release", "primary", "release", "auxiliary"]
    asyncio.run(scenario())


def test_auxiliary_gpu_low_memory_is_actionable_and_cpu_remains_available():
    from agent.gpu_resources import GpuResources
    async def scenario():
        gpu = GpuResources(lambda: asyncio.sleep(0), probe=lambda: {"free_bytes": 1024**2})
        with pytest.raises(CoreDataError) as error:
            async with gpu.auxiliary({"device": "cuda", "size_bytes": 1024**3}, resident=False):
                pytest.fail("Unsafe CUDA request admitted")
        assert error.value.code == "insufficient_vram"
        async with gpu.auxiliary({"device": "cpu", "size_bytes": 1024**3}, resident=False):
            pass
        async with gpu.auxiliary({"device": "cuda", "size_bytes": 1024**3}, resident=True):
            pass
    asyncio.run(scenario())


def test_explicit_cuda_probe_failure_prevents_unchecked_loading():
    from agent.gpu_resources import GpuResources
    async def scenario():
        gpu = GpuResources(lambda: asyncio.sleep(0), probe=lambda: {"error": "unavailable"})
        with pytest.raises(CoreDataError) as error:
            async with gpu.auxiliary({"device": "cuda", "size_bytes": 1024}, resident=False):
                pass
        assert error.value.code == "gpu_probe_unavailable"
    asyncio.run(scenario())


def test_auxiliary_switch_reclaims_old_cached_model_before_memory_check():
    from agent.gpu_resources import GpuResources
    async def scenario():
        memory = {"free_bytes": 1024}
        async def release():
            memory["free_bytes"] = 8 * 1024**3
        gpu = GpuResources(release, probe=lambda: dict(memory))
        async with gpu.auxiliary({"device": "cuda", "size_bytes": 1024**3}, resident=False):
            assert memory["free_bytes"] == 8 * 1024**3
    asyncio.run(scenario())


def test_primary_memory_preflight_accounts_for_context_and_keeps_paging_distinct():
    from agent.gpu_resources import primary_memory_preflight
    model = {"size_bytes": 5 * 1024**3, "kv_bytes_per_token": 128 * 1024}
    result = primary_memory_preflight(model, 8192,
        {"available_physical_bytes": 2 * 1024**3, "available_commit_bytes": 8 * 1024**3})
    assert result["kv_cache_bytes"] == 1024**3
    assert result["physical_memory_pressure"] is True
    with pytest.raises(CoreDataError) as error:
        primary_memory_preflight(model, 8192,
            {"available_physical_bytes": 2 * 1024**3, "available_commit_bytes": 1024**2})
    assert error.value.code == "insufficient_ram"
