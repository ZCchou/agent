from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any

from agent.infrastructure.logging import events, log_event, setup_logging
from agent.protocol import ASYNC_MESSAGE_TYPES, PROTOCOL_VERSION, handle_async_message, handle_message
from agent.runtime import ChatRunService
from agent.storage import CoreDataError, CoreRepository


def _read_stdin_chunk(descriptor: int) -> bytes:
    if os.name != "nt" or stat.S_ISREG(os.fstat(descriptor).st_mode):
        return os.read(descriptor, 65536)
    # A blocked Windows pipe read can stall native-library initialization.
    # Poll available bytes so no descriptor or pipe lock is held while idle.
    import ctypes
    import msvcrt
    from ctypes import wintypes
    peek = ctypes.WinDLL("kernel32", use_last_error=True).PeekNamedPipe
    peek.argtypes = (wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, wintypes.LPVOID,
                     ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID)
    peek.restype = wintypes.BOOL
    handle = msvcrt.get_osfhandle(descriptor)
    available = wintypes.DWORD()
    while True:
        if not peek(handle, None, 0, None, ctypes.byref(available), None):
            error = ctypes.get_last_error()
            if error in {38, 109}:
                return b""
            raise ctypes.WinError(error)
        if available.value:
            return os.read(descriptor, min(65536, available.value))
        time.sleep(.01)


async def _stdin_lines():
    try:
        descriptor = sys.stdin.fileno()
    except (AttributeError, io.UnsupportedOperation):
        for line in sys.stdin:
            yield line
        return
    if os.name == "nt":
        import msvcrt
        msvcrt.setmode(descriptor, os.O_BINARY)
    pending = b""
    while True:
        chunk = await asyncio.to_thread(_read_stdin_chunk, descriptor)
        if not chunk:
            if pending:
                yield pending.decode("utf-8")
            return
        pending += chunk
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            yield line.decode("utf-8")


async def _run(repository: CoreRepository, logger: logging.Logger) -> None:
    output: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    request_tasks: set[asyncio.Task[None]] = set()

    async def emit_event(event: dict[str, Any]) -> None:
        await output.put({"v": PROTOCOL_VERSION, "event": event})

    service = ChatRunService(repository, emit_event, logger=logger, offline=True)
    from agent.rag.bootstrap import install_builtin_skill
    install_builtin_skill(service.skills)

    async def writer() -> None:
        while True:
            item = await output.get()
            if item is None:
                return
            print(json.dumps(item, ensure_ascii=True, separators=(",", ":")), flush=True)

    async def process(message: Any, request_id: str | None, message_type: str | None,
                      started_at: float) -> None:
        if isinstance(message, dict) and message.get("type") in ASYNC_MESSAGE_TYPES:
            response = await handle_async_message(message, service)
        else:
            response = await asyncio.to_thread(handle_message, message, repository)
        await output.put(response)
        response_error = response.get("error") if isinstance(response, dict) else None
        log_event(
            logger, logging.INFO if "error" not in response else logging.WARNING,
            events.IPC_REQUEST_COMPLETED, "IPC request completed",
            request_id=request_id, message_type=message_type,
            duration_ms=round((time.perf_counter() - started_at) * 1000, 3),
            success="error" not in response,
            error_code=(
                response_error.get("code")
                if isinstance(response_error, dict)
                and isinstance(response_error.get("code"), str)
                else None
            ),
        )

    writer_task = asyncio.create_task(writer())
    try:
        async for line in _stdin_lines():
            line = line.strip()
            if not line:
                continue
            started_at = time.perf_counter()
            request_id: str | None = None
            message_type: str | None = None
            try:
                message = json.loads(line)
                if isinstance(message, dict):
                    request_id = message.get("id") if isinstance(message.get("id"), str) else None
                    message_type = message.get("type") if isinstance(message.get("type"), str) else None
                log_event(logger, logging.DEBUG, events.IPC_REQUEST_RECEIVED,
                          "IPC request received", request_id=request_id,
                          message_type=message_type, input_bytes=len(line.encode("utf-8")))
                task = asyncio.create_task(process(message, request_id, message_type, started_at))
                request_tasks.add(task)
                task.add_done_callback(request_tasks.discard)
            except json.JSONDecodeError:
                await output.put({
                    "v": PROTOCOL_VERSION, "id": request_id or "",
                    "type": message_type or "protocol.error",
                    "error": {"code": "invalid_json", "message": "Request is not valid JSON."},
                })
    finally:
        if request_tasks:
            await asyncio.gather(*request_tasks, return_exceptions=True)
        await service.shutdown()
        await output.put(None)
        await writer_task


def main() -> None:
    if sys.argv[1:2] in (["--profile-migration"], ["--profile-health"]):
        from agent.profile_migration_cli import main as profile_worker_main
        raise SystemExit(profile_worker_main(sys.argv[1:]))
    if sys.argv[1:] == ["--document-worker"]:
        from agent.document_parser.worker import main as document_worker_main
        raise SystemExit(document_worker_main())
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="strict")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    runtime = setup_logging(component="core")
    logger = runtime.logger
    pid = os.getpid()
    data_dir = Path(os.environ.get("AGENT_DATA_DIR", Path.cwd()))
    try:
        repository = CoreRepository(data_dir / "agent.db")
    except CoreDataError as error:
        log_event(logger, logging.ERROR, events.CORE_UNHANDLED_EXCEPTION,
                  "Core data initialization failed", error_type=error.code)
        runtime.shutdown()
        raise SystemExit(1) from None
    except Exception as error:
        log_event(logger, logging.ERROR, events.CORE_UNHANDLED_EXCEPTION,
                  "Core data initialization failed", error_type=type(error).__name__)
        runtime.shutdown()
        raise SystemExit(1) from None

    log_event(logger, logging.INFO, events.CORE_STARTED, "Python Agent Core started",
              pid=pid, file_logging=runtime.file_enabled)
    try:
        asyncio.run(_run(repository, logger))
    except Exception:
        logger.exception("Agent Core main loop crashed", extra={
            "event": events.CORE_UNHANDLED_EXCEPTION, "fields": {"pid": pid}
        })
        raise
    finally:
        log_event(logger, logging.INFO, events.CORE_STOPPED, "Python Agent Core stopped", pid=pid)
        runtime.shutdown()


if __name__ == "__main__":
    main()
