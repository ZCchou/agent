"""One serialized JSONL worker per explicitly selected device."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from agent.storage import CoreDataError
from agent.infrastructure.processes import WindowsJob


class WorkerClient:
    def __init__(self, command: list[str], timeout: float = 300, memory_limit: int | None = None):
        self.command = command
        self.timeout = timeout
        self.memory_limit = memory_limit
        self.process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._sequence = 0
        self._closed = False
        self._job = None

    async def _start(self):
        if self._closed:
            raise CoreDataError("rag_model_error", "Inference worker is closed")
        if self.process is not None and self.process.returncode is None:
            return
        env = os.environ.copy()
        for name in list(env):
            if name.upper() in {"PYTHONHOME", "PYTHONPATH", "LD_LIBRARY_PATH", "LD_PRELOAD", "CUDA_VISIBLE_DEVICES", "CUDNN_PATH"} or name.upper().startswith("CUDA_PATH"):
                env.pop(name)
        system_root = Path(os.environ.get("SystemRoot", "C:/Windows"))
        env["PATH"] = os.pathsep.join((str(Path(self.command[0]).resolve().parent),
                                      str(system_root / "System32"), str(system_root)))
        env["PYTHONNOUSERSITE"] = "1"
        env.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_DATASETS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1", TOKENIZERS_PARALLELISM="false", PYTHONIOENCODING="utf-8")
        if not getattr(sys, "frozen", False):
            source = str(Path(__file__).resolve().parents[2])
            env["PYTHONPATH"] = source
        self.process = await asyncio.create_subprocess_exec(
            *self.command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=env, limit=32 * 1024 * 1024,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        if os.name == "nt":
            try:
                self._job = WindowsJob(self.process.pid, self.memory_limit)
            except BaseException:
                await self._stop()
                raise
        if self._closed:
            await self._stop()
            raise CoreDataError("rag_model_error", "Inference worker is closed")

    async def request(self, action: str, params: dict):
        async with self._lock:
            try:
                return await asyncio.wait_for(self._exchange(action, params), timeout=self.timeout)
            except asyncio.TimeoutError as exc:
                await self._stop()
                raise CoreDataError("rag_model_error", "Inference worker timed out and was stopped") from exc
            except asyncio.CancelledError:
                await self._stop()
                raise
            except (OSError, ValueError, EOFError, ConnectionError) as exc:
                await self._stop()
                raise CoreDataError("rag_model_error", f"Inference worker failed: {exc}") from exc

    async def _exchange(self, action, params):
        await self._start()
        process = self.process
        self._sequence += 1
        request_id = self._sequence
        payload = {"id": request_id, "action": action, "params": params}
        process.stdin.write((json.dumps(payload, ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8"))
        await process.stdin.drain()
        line = await process.stdout.readline()
        if not line:
            raise EOFError("Worker exited before returning a response")
        response = json.loads(line)
        if response.get("id") != request_id:
            raise ValueError("Worker response id mismatch")
        if response.get("error"):
            raise CoreDataError("rag_model_error", str(response["error"]))
        return response["result"]

    async def _stop(self):
        process, self.process = self.process, None
        job, self._job = self._job, None
        if job is not None:
            job.close()
        if process is not None:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.wait()

    async def close(self):
        self._closed = True
        # Stopping first also interrupts any in-flight native inference.
        await self._stop()
        async with self._lock:
            pass
