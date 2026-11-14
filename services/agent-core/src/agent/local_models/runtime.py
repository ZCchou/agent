"""One authenticated, contained llama.cpp server with explicit session leases."""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import socket
import subprocess
import threading
import time
import traceback
import uuid
from dataclasses import replace
from pathlib import Path

import httpx

from agent.infrastructure.processes import launch_contained, restrict_file
from agent.providers import ProviderProfile
from agent.storage import CoreDataError
from agent.execution.execution_wait import current_activity, current_task_id
from agent.execution.activity import watch_process
from .catalog import canonical, cancellation
from .manifest import RuntimeManifest
from .paths import checked_path


def allocate_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        if os.name == "nt":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def isolated_environment(executable):
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() not in {"PATH", "PYTHONPATH", "PYTHONHOME", "LD_LIBRARY_PATH",
                                          "LD_PRELOAD", "GGML_BACKEND_PATH", "LLAMA_ARG_API_KEY"}
                   and not key.upper().startswith(("LLAMA_ARG_", "GGML_"))}
    system_root = Path(os.environ.get("SystemRoot", "C:/Windows"))
    environment["PATH"] = os.pathsep.join((str(executable.parent), str(system_root / "System32"), str(system_root)))
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


class _Output:
    def __init__(self, stream):
        self.data = bytearray()
        self.lock = threading.Lock()
        self._pending = b""
        self._gpu_layers = 0
        self._backends = set()
        self._context_length = None
        self.byte_count = 0
        self.thread = threading.Thread(target=self._read, args=(stream,), daemon=True)
        self.thread.start()

    def _read(self, stream):
        try:
            while block := os.read(stream.fileno(), 4096):
                with self.lock:
                    self._capture_facts(block)
                    self.byte_count += len(block)
                    self.data.extend(block)
                    del self.data[:-65536]
        except (OSError, ValueError):
            pass
        finally:
            stream.close()

    def text(self):
        with self.lock:
            return self.data.decode("utf-8", errors="replace")

    def _capture_facts(self, block):
        lines = (self._pending + block).split(b"\n")
        self._pending = lines.pop()[-65536:]
        for raw in lines:
            line = raw.decode("utf-8", errors="replace")
            self._backends.update(re.findall(r"\b(cuda|vulkan)\d*\b", line.lower()))
            offload = re.search(r"offloaded\s+(\d{1,5})/\d+\s+layers\s+to\s+GPU", line, re.IGNORECASE)
            if offload:
                self._gpu_layers = int(offload[1])
            context = re.search(r"\bn_ctx\s*=\s*(\d{1,9})\b", line)
            if context:
                self._context_length = int(context[1])

    def facts(self):
        with self.lock:
            return {"gpu_layers": self._gpu_layers, "backends": sorted(self._backends),
                    "context_length": self._context_length}


def process_failure(output, returncode=None):
    text = output.lower()
    if "address already in use" in text or "failed to bind" in text or "10048" in text or "10013" in text:
        return CoreDataError("port_conflict", "The local runtime port was already occupied.")
    if "out of memory" in text or "not enough memory" in text or "failed to allocate" in text:
        return CoreDataError("insufficient_vram", "The model does not fit the selected backend memory.")
    if "driver" in text or "cuda error" in text or "vk_error" in text or "no vulkan" in text:
        return CoreDataError("driver_error", "The selected GPU driver could not initialize the runtime.")
    if "gguf" in text or "error loading model" in text or "failed to load model" in text:
        return CoreDataError("malformed_model", "The inference engine rejected the model weights.")
    if returncode is not None and (returncode & 0xFFFFFFFF) in {0xC0000135, 0xC000007B, 0xC0000142}:
        return CoreDataError("runtime_dependency", "A required native runtime library could not be loaded.")
    return CoreDataError("runtime_crashed", "The local inference process exited unexpectedly.")


class ManagedRuntime:
    def __init__(self, catalog, *, runtime_root=None, load_timeout=300, idle_timeout=1800, permit, pinned=lambda: False):
        self.catalog = catalog
        self.pinned = pinned
        self.manifest = RuntimeManifest(runtime_root)
        self.load_timeout = load_timeout
        self.idle_timeout = idle_timeout
        self.permit = permit
        self.profile = None
        self.process = None
        self.job = None
        self.session_id = None
        self.model_id = None
        self.backend = None
        self.engine = None
        self.gpu_layers_used = 0
        self.actual_context_length = None
        self.error = None
        self.preflight = None
        self.fallbacks = []
        self._state = "unloaded"
        self._output = None
        self._credential = None
        self._watch = None
        self._idle = None
        self._last_used = time.monotonic()

    @property
    def alive(self):
        return self.process is not None and self.process.poll() is None and self._state == "loaded"

    def matches(self, provider_id):
        return self.alive and self.profile is not None and self.profile.id == provider_id

    def touch(self):
        self._last_used = time.monotonic()

    def status(self):
        return {"state": self._state, "model_id": self.model_id, "backend": self.backend,
                "session_id": self.session_id, "gpu_layers_used": self.gpu_layers_used,
                "context_length": self.actual_context_length, "error": self.error,
                "preflight": self.preflight,
                "fallbacks": list(self.fallbacks)}

    def capabilities(self, *, verify=True):
        try:
            engines = self.manifest.engines() if verify else self.manifest.inventory()
        except CoreDataError as exc:
            return {"devices": [{"id": "cpu", "name": "CPU", "available": True}],
                    "runtimes": [], "error": {"code": exc.code, "message": exc.message}}
        devices = [{"id": "cpu", "name": "CPU", "available": True}]
        runtimes = []
        for backend, engine in engines.items():
            record = {"backend": backend, "version": engine.version, "source_commit": engine.source_commit,
                      "build": engine.build, "available": verify, "integrity": "verified" if verify else "unchecked"}
            if verify and backend != "cpu":
                process = job = None
                try:
                    process, job = launch_contained([str(engine.executable), "--list-devices"],
                        cwd=engine.executable.parent, env=isolated_environment(engine.executable),
                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                    output, _ = process.communicate(timeout=10)
                    matches = re.findall(r"^\s*((?:CUDA|Vulkan)\d+):\s*(.{1,256})$", output[:65536].decode("utf-8", errors="replace"), re.MULTILINE | re.IGNORECASE)
                    record["available"] = process.returncode == 0 and bool(matches)
                    for identifier, name in matches:
                        devices.append({"id": identifier, "backend": backend, "name": name, "available": True,
                                        "provenance": "llama.cpp --list-devices"})
                    if not record["available"]:
                        record["error"] = {"code": "device_unavailable", "message": "The engine did not report a usable GPU device."}
                except (OSError, subprocess.TimeoutExpired):
                    record["available"] = False
                    record["error"] = {"code": "device_unavailable", "message": "The engine could not probe GPU devices."}
                finally:
                    if job:
                        job.close()
                    if process:
                        process.wait()
            runtimes.append(record)
        return {"devices": devices, "runtimes": runtimes}

    async def load(self, model_id, event):
        await self.unload()
        self._state, self.model_id = "loading", model_id
        self.error, self.fallbacks = None, []
        self.catalog.set_status(model_id, "loading")
        try:
            record = self.catalog.get(model_id)
            cancel = lambda: cancellation(event)
            model_path, inspection = await asyncio.to_thread(self.catalog.verify, record, cancel=cancel, include_inspection=True)
            from agent.gpu_resources import primary_memory_preflight
            self.preflight = primary_memory_preflight(inspection, record["context_length"])
            engines = await asyncio.to_thread(self.manifest.engines)
            candidates = [backend for backend in ("cuda", "vulkan", "cpu") if backend in engines] if record["device"] == "auto" else [record["device"]]
            if not candidates:
                raise CoreDataError("backend_unavailable", "No managed llama.cpp backend is installed.")
            deadline = None if current_task_id.get() is not None else time.monotonic() + self.load_timeout
            for backend in candidates:
                cancel()
                if backend not in engines:
                    raise CoreDataError("backend_unavailable", "The requested backend is not installed.")
                try:
                    await self._load_engine(engines[backend], record, model_path, deadline, event)
                    self.catalog.set_status(model_id, "loaded")
                    self._state = "loaded"
                    self.touch()
                    self._watch = asyncio.create_task(self._monitor(self.process, self.session_id))
                    self._idle = asyncio.create_task(self._release_idle(self.session_id))
                    return
                except CoreDataError as exc:
                    await self._stop_child()
                    if record["device"] != "auto":
                        raise
                    self.fallbacks.append({"backend": backend, "code": exc.code})
                    if backend == candidates[-1]:
                        raise
        except BaseException as exc:
            await self._stop_child()
            self.profile, self.session_id = None, None
            if isinstance(exc, asyncio.CancelledError) or event.is_set():
                self._state = "unloaded"
                self.catalog.set_status(model_id, "imported")
            else:
                self._state = "failed"
                self.error = {"code": getattr(exc, "code", "runtime_start_failed"),
                              "message": exc.message if isinstance(exc, CoreDataError) else "The local runtime could not start."}
                self.catalog.set_status(model_id, "error", self.error)
            raise

    async def _load_engine(self, engine, record, model_path, deadline, event):
        for attempt in range(3):
            try:
                operation = "port allocation"
                port = allocate_port()
                token = secrets.token_urlsafe(48)
                operation = "credential preparation"
                credentials = checked_path(self.catalog.root / ".credentials")
                credentials.mkdir(exist_ok=True)
                self._credential = credentials / ("session-" + uuid.uuid4().hex + ".key")
                descriptor = os.open(self._credential, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    restrict_file(self._credential)
                    with os.fdopen(descriptor, "w", encoding="ascii", closefd=False) as stream:
                        stream.write(token + "\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                finally:
                    os.close(descriptor)
                auto_layers = engine.backend != "cpu" and record["gpu_layers"] == -1
                layers = "auto" if auto_layers else str(record["gpu_layers"] if engine.backend != "cpu" else 0)
                command = [str(engine.executable), "--host", "127.0.0.1", "--port", str(port),
                    "--model", str(model_path), "--alias", record["id"], "--ctx-size", str(record["context_length"]),
                    "--threads", str(record["threads"]), "--n-gpu-layers", layers,
                    "--api-key-file", str(self._credential), "--jinja", "--parallel", "1", "--no-webui",
                    "--log-verbosity", "4", "--log-colors", "off",
                    "--fit", "on" if auto_layers else "off"]
                if auto_layers:
                    command.extend(["--fit-ctx", str(record["context_length"])])
                if record["architecture"] == "qwen3":
                    command.extend(["--reasoning", "off"])
                operation = "process creation"
                self.process, self.job = launch_contained(command, cwd=engine.executable.parent,
                    env=isolated_environment(engine.executable), stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, bufsize=0)
                self._output = _Output(self.process.stderr)
                base_url = f"http://127.0.0.1:{port}"
                operation = "authenticated readiness check"
                props = await self._readiness(base_url, token, record["id"], deadline, event)
                facts = self._output.facts()
                self.gpu_layers_used = facts["gpu_layers"]
                if engine.backend != "cpu" and (not self.gpu_layers_used or engine.backend not in facts["backends"]):
                    raise CoreDataError("backend_not_used", "The selected GPU backend did not offload model layers.")
                actual_context = props.get("default_generation_settings", {}).get("n_ctx")
                self.actual_context_length = facts["context_length"] or actual_context or record["context_length"]
                if self.actual_context_length < record["context_length"]:
                    raise CoreDataError("context_not_available", "The runtime could not preserve the requested context length.")
                self.engine, self.backend = engine, engine.backend
                self.session_id = uuid.uuid4().hex
                capabilities = {"input_cost_per_million": "0", "output_cost_per_million": "0",
                                "context_length": self.actual_context_length, "text": True}
                self.profile = ProviderProfile(id=record["provider_profile_id"], name=record["name"],
                    kind="openai_compatible", base_url=base_url + "/v1", api_key=token,
                    local_strict=True, allowed_model_ids=(record["id"],), automatic_model_ids=(record["id"],),
                    model_capabilities={record["id"]: capabilities}, model_tool_modes={record["id"]: self._tool_mode(record)})
                # llama.cpp consumes the key file at startup. The resident secret
                # remains only in memory after authenticated readiness succeeds.
                operation = "credential cleanup"
                self._credential.unlink(missing_ok=True)
                self._credential = None
                return
            except CoreDataError as exc:
                await self._stop_child()
                if exc.code != "port_conflict" or attempt == 2:
                    raise
            except OSError as exc:
                await self._stop_child()
                os_code = getattr(exc, "winerror", None) or exc.errno
                origin = traceback.extract_tb(exc.__traceback__)[-1]
                message = (f"Local runtime {operation} failed "
                           f"(OS error {os_code} at {Path(origin.filename).name}:{origin.lineno}).")
                raise CoreDataError("runtime_dependency", message) from exc

    async def _readiness(self, base_url, token, model_id, deadline, event):
        activity = current_activity.get()
        sampler = asyncio.create_task(watch_process(self.process, activity, job=self.job)) if activity is not None else None
        reported_bytes = 0
        try:
            async with httpx.AsyncClient(timeout=1, trust_env=False, headers={"Authorization": "Bearer " + token}) as client:
                while deadline is None or time.monotonic() < deadline:
                    cancellation(event)
                    if self.process.poll() is not None:
                        await asyncio.to_thread(self._output.thread.join, 0.2)
                        raise process_failure(self._output.text(), self.process.returncode)
                    if activity is not None and self._output.byte_count != reported_bytes:
                        reported_bytes = self._output.byte_count
                        activity.resource(log_bytes=reported_bytes)
                    try:
                        health = await client.get(base_url + "/health")
                        if activity is not None:
                            activity.heartbeat(health_status=health.status_code)
                        if health.status_code == 200:
                            models = await client.get(base_url + "/v1/models")
                            if models.status_code in (401, 403):
                                raise CoreDataError("runtime_authentication", "The local runtime did not accept its session credential.")
                            models.raise_for_status()
                            if [item.get("id") for item in models.json().get("data", [])] != [model_id]:
                                raise CoreDataError("runtime_identity", "The local runtime returned an unexpected model identity.")
                            props = await client.get(base_url + "/props")
                            props.raise_for_status()
                            return props.json()
                    except (httpx.HTTPError, ValueError, KeyError, TypeError):
                        pass
                    await asyncio.sleep(0.1)
        finally:
            if sampler is not None:
                sampler.cancel()
                await asyncio.gather(sampler, return_exceptions=True)
        raise CoreDataError("runtime_load_timeout", "The model did not become ready within the load timeout.")

    async def heartbeat(self, activity):
        profile = self.profile
        if profile is None:
            return
        try:
            async with httpx.AsyncClient(timeout=1, trust_env=False,
                headers={'Authorization': 'Bearer ' + profile.api_key}) as client:
                response = await client.get(profile.base_url.removesuffix('/v1') + '/health')
                activity.heartbeat(health_status=response.status_code)
        except httpx.HTTPError:
            # A failed probe provides no CPU/GPU activity measurement.
            pass

    async def _stop_child(self):
        self.profile = None
        process, job = self.process, self.job
        self.process, self.job = None, None
        if job:
            job.close()
        if process:
            if process.poll() is None:
                process.kill()
            await asyncio.to_thread(process.wait)
        if self._output:
            await asyncio.to_thread(self._output.thread.join, 1)
            self._output = None
        if self._credential:
            self._credential.unlink(missing_ok=True)
            self._credential = None

    async def _monitor(self, process, session):
        while process.poll() is None:
            await asyncio.sleep(0.1)
        if session == self.session_id and self._state == "loaded":
            failure = process_failure(self._output.text() if self._output else "", process.returncode)
            self.error = {"code": failure.code, "message": failure.message}
            self._state = "failed"
            self.catalog.set_status(self.model_id, "error", self.error)
            await self._stop_child()
            self.session_id = None

    async def _release_idle(self, session):
        while session == self.session_id and self._state == "loaded":
            await asyncio.sleep(1 if self.idle_timeout == 0 else min(1, max(0.02, self.idle_timeout / 4)))
            if session != self.session_id or not self.alive:
                return
            if self.idle_timeout == 0:
                continue
            if not self.permit.locked() and not self.pinned() and time.monotonic() - self._last_used >= self.idle_timeout:
                async with self.permit:
                    if session == self.session_id and not self.pinned() and time.monotonic() - self._last_used >= self.idle_timeout:
                        await self.unload()
                        return

    async def unload(self):
        current = asyncio.current_task()
        for task in (self._watch, self._idle):
            if task and task is not current:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self._watch = self._idle = None
        await self._stop_child()
        if self.model_id:
            self.catalog.set_status(self.model_id, "imported")
        self._state, self.model_id, self.session_id, self.backend = "unloaded", None, None, None
        self.engine, self.error = None, None
        self.preflight = None
        self.gpu_layers_used, self.actual_context_length = 0, None

    def _tool_key(self, record):
        return "tool-mode:" + record["digest"] + ":" + record["template_digest"] + ":" + self.engine.source_commit + ":" + self.engine.version

    def _tool_mode(self, record):
        with self.catalog.repository._connect() as connection:
            row = connection.execute("SELECT value FROM local_model_settings WHERE key=?", (self._tool_key(record),)).fetchone()
        return row[0] if row else "text_only"

    def set_tool_mode(self, provider_id, mode, verified=True):
        if not self.matches(provider_id):
            raise CoreDataError("model_not_loaded", "Load this model before probing tool support.")
        if mode not in {"text_only", "native", "structured"} or not verified:
            raise CoreDataError("invalid_tool_mode", "Only a verified supported tool mode can be stored.")
        record = self.catalog.get(self.model_id)
        with self.catalog.repository._connect() as connection:
            connection.execute("INSERT INTO local_model_settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (self._tool_key(record), mode))
        self.profile = replace(self.profile, model_tool_modes={self.model_id: mode})

    async def close(self):
        await self.unload()
