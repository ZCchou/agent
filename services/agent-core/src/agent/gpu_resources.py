"""Serialize GPU loading and check available NVIDIA memory before auxiliary loads."""
import asyncio
import csv
import ctypes
import os
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

from agent.storage import CoreDataError


def system_memory():
    if os.name != "nt":
        return {}
    class MemoryStatus(ctypes.Structure):
        _fields_ = [("length", ctypes.c_uint32), ("load", ctypes.c_uint32)] + [
            (name, ctypes.c_uint64) for name in ("total_physical", "available_physical", "total_commit",
                "available_commit", "total_virtual", "available_virtual", "available_extended")]
    status = MemoryStatus()
    status.length = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return {}
    return {"available_physical_bytes": status.available_physical,
            "available_commit_bytes": status.available_commit}


def primary_memory_preflight(inspection, context_length, memory=None):
    memory = system_memory() if memory is None else memory
    per_token = inspection.get("kv_bytes_per_token")
    kv = per_token * context_length if per_token is not None else None
    workspace = 1024**3
    commit_required = kv + workspace if kv is not None else None
    if commit_required is not None and memory.get("available_commit_bytes", commit_required) < commit_required:
        raise CoreDataError("insufficient_ram", "主模型 KV 缓存和工作区所需提交内存不足。请减小上下文长度或释放内存。")
    resident = inspection["size_bytes"] + kv + workspace if kv is not None else None
    return {"weights_bytes": inspection["size_bytes"], "kv_cache_bytes": kv,
            "workspace_estimate_bytes": workspace, "resident_estimate_bytes": resident,
            "physical_memory_pressure": resident is not None and memory.get("available_physical_bytes", resident) < resident,
            **memory}


def nvidia_memory():
    executable = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/nvidia-smi.exe"
    try:
        result = subprocess.run([str(executable), "--query-gpu=name,driver_version,memory.total,memory.free",
            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5,
            creationflags=0x08000000 if os.name == "nt" else 0, check=True)
        # Both managed runtimes currently select the first physical CUDA device.
        row = next(csv.reader(result.stdout.splitlines()))
        return {"name": row[0].strip(), "driver": row[1].strip(),
                "total_bytes": int(row[2].strip()) * 1024**2, "free_bytes": int(row[3].strip()) * 1024**2}
    except (OSError, subprocess.SubprocessError, ValueError, IndexError, StopIteration):
        return {"error": "NVIDIA device memory could not be queried."}


class GpuResources:
    def __init__(self, release_auxiliary, *, probe=nvidia_memory):
        self._lock = asyncio.Lock()
        self._release_auxiliary = release_auxiliary
        self._probe = probe

    @asynccontextmanager
    async def primary(self, model):
        if model["device"] == "cpu":
            yield
            return
        async with self._lock:
            # Release cached auxiliary weights before llama.cpp fits its layers/KV.
            await self._release_auxiliary()
            yield

    @asynccontextmanager
    async def auxiliary(self, model, *, resident):
        if model["device"] == "cpu":
            yield
            return
        async with self._lock:
            loaded = resident() if callable(resident) else resident
            if not loaded:
                await self._release_auxiliary()
                memory = await asyncio.to_thread(self._probe)
                if "free_bytes" not in memory:
                    raise CoreDataError("gpu_probe_unavailable", "无法核验 CUDA 可用显存。请检查驱动或选择 CPU。")
                # Includes potential FP32 expansion plus workspace. Actual backend
                # allocation remains authoritative and its errors stay visible.
                required = 2 * model["size_bytes"] + 512 * 1024**2
                if memory["free_bytes"] < required:
                    raise CoreDataError("insufficient_vram", "辅助模型可用显存不足。请卸载主模型、减少 GPU 层数，或将辅助模型设为 CPU。")
            yield
