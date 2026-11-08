"""Public managed local-model facade used by IPC and unified invocation."""
from __future__ import annotations

import asyncio
import threading
import uuid
from contextlib import asynccontextmanager, nullcontext

from agent.storage import CoreDataError
from .catalog import Catalog, CancelledOperation, now
from .gguf import inspect_gguf


class LocalModelService:
    def __init__(self, core_repository, *, runtime_root=None, idle_timeout=600, load_timeout=300, load_guard=None):
        self.repository = core_repository
        self.catalog = Catalog(core_repository)
        self.runtime_root = runtime_root
        self.idle_timeout = idle_timeout
        self.load_timeout = load_timeout
        self.load_guard = load_guard
        self._runtime = None
        self._jobs = {}
        self._operation = None
        self._permit = asyncio.Lock()
        self._closed = False

    @property
    def root(self):
        return self.catalog.root

    def owns_profile(self, provider_id):
        return isinstance(provider_id, str) and provider_id.startswith("managed:")

    def get_profile(self, provider_id):
        if not self.owns_profile(provider_id):
            raise CoreDataError("model_not_found", "This provider is not a managed local model.")
        self.catalog.get(provider_id.removeprefix("managed:"))
        if self._runtime is None or not self._runtime.matches(provider_id):
            raise CoreDataError("model_not_loaded", "Load the managed model before using it.")
        return self._runtime.profile

    def provider_profiles(self):
        if self._runtime and self._runtime.profile and self._runtime.alive:
            return {self._runtime.profile.id: self._runtime.profile}
        return {}

    def default_provider_id(self):
        profiles = self.provider_profiles()
        return next((model["provider_profile_id"] for model in self.catalog.models()
                     if model["is_default"] and model["provider_profile_id"] in profiles), None)

    def set_tool_mode(self, provider_id, mode, verified=True):
        return self._backend().set_tool_mode(provider_id, mode, verified=verified)

    def runtime_identity(self, provider_id):
        self.get_profile(provider_id)
        runtime = self._runtime
        record = self.catalog.get(runtime.model_id)
        return {"artifact_digest": record["digest"], "template_digest": record["template_digest"],
                "engine_version": runtime.engine.version, "engine_commit": runtime.engine.source_commit,
                "backend": runtime.backend, "session_id": runtime.session_id,
                "context_length": str(runtime.actual_context_length), "threads": str(record["threads"]),
                "gpu_layers": str(runtime.gpu_layers_used)}

    def _backend(self):
        if self._runtime is None:
            from .runtime import ManagedRuntime
            self._runtime = ManagedRuntime(self.catalog, runtime_root=self.runtime_root,
                load_timeout=self.load_timeout, idle_timeout=self.idle_timeout, permit=self._permit)
        return self._runtime

    def _ensure_mutable(self, *, unloaded=False):
        if self._operation or self._permit.locked():
            raise CoreDataError("model_busy", "A managed model operation or invocation is active.")
        if unloaded and self.provider_profiles():
            raise CoreDataError("model_busy", "Unload the resident model before changing storage.")

    async def handle(self, action, params):
        if self._closed:
            raise CoreDataError("runtime_closed", "The managed runtime is closed.")
        if not isinstance(params, dict):
            raise CoreDataError("invalid_params", "Model action parameters must be an object.")
        if action == "model.list":
            return {"models": self.catalog.models(), "root": str(self.root),
                    "storage_migration": self.catalog.storage_migration()}
        if action == "job.list":
            return {"jobs": self.catalog.jobs()}
        if action == "model.inspect":
            return {"model": await asyncio.to_thread(inspect_gguf, self._required(params, "path"))}
        if action == "job.cancel":
            job_id = self._required(params, "job_id")
            self._request_cancel(job_id)
            job = next((job for job in self.catalog.jobs() if job["id"] == job_id), None)
            if job is None:
                raise CoreDataError("job_not_found", "The managed model job was not found.")
            return {"job": job}
        if action == "runtime.capabilities":
            verify = params.get("verify", True)
            if type(verify) is not bool:
                raise CoreDataError("invalid_params", "verify must be a boolean.")
            return await asyncio.to_thread(self._backend().capabilities, verify=verify)
        if action == "runtime.status":
            return self._backend().status()
        if action == "runtime.unload":
            self._ensure_mutable()
            async with self._permit:
                await self._backend().unload()
            return self._backend().status()
        if action == "runtime.load":
            if self._operation:
                raise CoreDataError("model_busy", "A managed model operation is already pending.")
            model_id = self._required(params, "model_id")
            self.catalog.get(model_id)
            return self._schedule(action, model_id=model_id)
        if action == "model.import":
            self._ensure_mutable()
            return self._schedule(action, path=self._required(params, "path"))
        if action == "storage.root":
            self._ensure_mutable(unloaded=True)
            target = self._required(params, "path")
            return {**self._schedule(action, path=target), "storage_migration": {
                "previous_root": str(self.root), "root": target, "retained_previous_root": True}}
        if action in {"model.configure", "model.remove"}:
            if action == "model.configure" and params.keys() <= {"model_id", "is_default", "source", "license"}:
                return {"model": self.catalog.configure(self._required(params, "model_id"), params)}
            self._ensure_mutable()
            model_id = self._required(params, "model_id")
            if self._runtime and self._runtime.matches("managed:" + model_id):
                raise CoreDataError("model_busy", "Unload this model before modifying it.")
            if action == "model.remove":
                self.catalog.remove(model_id)
                return {"removed": True, "model_id": model_id}
            return {"model": self.catalog.configure(model_id, params)}
        raise CoreDataError("unsupported_action", "Unknown managed local-model action.")

    @staticmethod
    def _required(params, key):
        value = params.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > 32768:
            raise CoreDataError("invalid_params", f"{key} must be a non-empty string.")
        return value

    def _schedule(self, kind, *, path=None, model_id=None):
        job_id = uuid.uuid4().hex
        with self.repository._connect() as connection:
            connection.execute("INSERT INTO local_model_jobs(id,kind,model_id,status,progress,created_at,updated_at) VALUES (?,?,?,'queued',0,?,?)",
                               (job_id, kind, model_id, now(), now()))
        self._operation = kind
        event = threading.Event()
        task = asyncio.create_task(self._run_job(job_id, kind, event, path=path, model_id=model_id))
        self._jobs[job_id] = (task, event)
        return {"job": next(job for job in self.catalog.jobs() if job["id"] == job_id)}

    async def _run_job(self, job_id, kind, event, *, path, model_id):
        worker = None
        try:
            self.catalog.update_job(job_id, "running")
            if event.is_set():
                raise CancelledOperation()
            if kind == "runtime.load":
                async with self._permit:
                    guard = self.load_guard(self.catalog.get(model_id)) if self.load_guard else nullcontext()
                    async with guard:
                        await self._backend().load(model_id, event)
            else:
                operation = self.catalog.import_model if kind == "model.import" else self.catalog.migrate_root
                worker = asyncio.create_task(asyncio.to_thread(operation, path, job_id, event))
                await asyncio.shield(worker)
            self.catalog.update_job(job_id, "completed", progress=1)
        except (asyncio.CancelledError, CancelledOperation):
            event.set()
            if worker:
                try:
                    await self._join_worker(worker)
                except CancelledOperation:
                    pass
                except Exception as exc:
                    self._fail_job(job_id, exc)
                    return
                else:
                    self.catalog.update_job(job_id, "completed", progress=1)
                    return
            self.catalog.update_job(job_id, "cancelled")
        except Exception as exc:
            self._fail_job(job_id, exc)
        finally:
            self._operation = None
            self._jobs.pop(job_id, None)

    def _request_cancel(self, job_id):
        active = self._jobs.get(job_id)
        if active is None or active[1].is_set():
            return
        active[1].set()
        # Cancellation is injected at most once. Queued coroutines must enter
        # their own cleanup handler, and running cleanup must finish undisturbed.
        current = next(job for job in self.catalog.jobs() if job["id"] == job_id)
        if current["status"] == "running":
            active[0].cancel()

    @staticmethod
    async def _join_worker(worker):
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                # Even cancellation of the owner must not abandon a disk worker.
                continue
        return worker.result()

    def _fail_job(self, job_id, exc):
        # Never persist subprocess output, endpoint credentials, or tracebacks.
        error = {"code": getattr(exc, "code", "model_operation_failed"),
                 "message": exc.message if isinstance(exc, CoreDataError) else "The local model operation failed."}
        self.catalog.update_job(job_id, "failed", error=error)

    @asynccontextmanager
    async def invocation(self, provider_id):
        profile = self.get_profile(provider_id)
        session = self._runtime.session_id
        async with self._permit:
            current = self.get_profile(provider_id)
            if self._operation or self._runtime.session_id != session \
                    or (current.base_url, current.api_key) != (profile.base_url, profile.api_key):
                raise CoreDataError("model_not_loaded", "The managed model session changed before invocation.")
            self._runtime.touch()
            try:
                yield profile
            finally:
                self._runtime.touch()

    async def close(self):
        self._closed = True
        jobs = list(self._jobs.values())
        for job_id in list(self._jobs):
            self._request_cancel(job_id)
        if jobs:
            for task, _ in jobs:
                await self._join_worker(task)
        async with self._permit:
            if self._runtime:
                await self._runtime.close()
