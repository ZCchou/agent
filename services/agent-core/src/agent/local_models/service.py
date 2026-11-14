"""Public managed local-model facade used by IPC and unified invocation."""
from __future__ import annotations

import asyncio
import threading
import time
import uuid
from contextlib import asynccontextmanager, nullcontext

from agent.storage import CoreDataError
from .catalog import Catalog, CancelledOperation, now
from .gguf import inspect_gguf


class LocalModelService:
    def __init__(self, core_repository, *, runtime_root=None, idle_timeout=1800, load_timeout=300, load_guard=None):
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
        self._preparations = {}
        self._run_pins = {}
        self._auto_load_job = None
        self.execution_monitor = None
        self.execution_policy = None
        self._runtime_identities = {}
        self._preparation_jobs = {}
        self._model_load_jobs = {}

    @property
    def root(self):
        return self.catalog.root

    def set_idle_timeout(self, seconds):
        if type(seconds) is not int or seconds != 0 and not 60 <= seconds <= 86400:
            raise CoreDataError('invalid_settings', '模型驻留时间须为 1 至 1440 分钟，或选择手动卸载。')
        self.idle_timeout = seconds
        if self._runtime is not None:
            self._runtime.idle_timeout = seconds
            self._runtime.touch()

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
        identity = {"artifact_digest": record["digest"], "template_digest": record["template_digest"],
                "engine_version": runtime.engine.version, "engine_commit": runtime.engine.source_commit,
                "backend": runtime.backend, "session_id": runtime.session_id,
                "context_length": str(runtime.actual_context_length), "threads": str(record["threads"]),
                "gpu_layers": str(runtime.gpu_layers_used)}
        self._runtime_identities[provider_id] = dict(identity)
        return identity

    def _backend(self):
        if self._runtime is None:
            from .runtime import ManagedRuntime
            self._runtime = ManagedRuntime(self.catalog, runtime_root=self.runtime_root,
                load_timeout=self.load_timeout, idle_timeout=self.idle_timeout, permit=self._permit,
                pinned=self.pinned)
        return self._runtime

    def _ensure_mutable(self, *, unloaded=False):
        if self._operation or self._permit.locked() or self.pinned():
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
        if action == "runtime.prepare":
            return self.prepare(self._required(params, "model_id"), params.get("preparation_id"))
        if action == "runtime.release":
            self.release_preparation(self._required(params, "preparation_id"))
            return {"released": True}
        if action == "job.list":
            return {"jobs": self.jobs()}
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
            self.pinned()
            return {**self._backend().status(), "active_run_count": len(self._run_pins),
                    "preparation_count": len(self._preparations), "operation": self._operation}
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
        if action == "model.configure_load":
            self._ensure_mutable()
            model_id = self._required(params, "model_id")
            configuration = self.catalog.validate_configuration(model_id, params)
            return self._schedule("runtime.configure_load", model_id=model_id, configuration=configuration)
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

    def pinned(self, model_id=None):
        now_time = time.monotonic()
        self._preparations = {key: value for key, value in self._preparations.items()
                              if value[1] > now_time}
        self._preparation_jobs = {key: value for key, value in self._preparation_jobs.items()
                                  if key in self._preparations}
        models = [value[0] for value in self._preparations.values()] + list(self._run_pins.values())
        return any(model_id is None or identity == model_id for identity in models)

    def pin_run(self, run_id, provider_id):
        if self.owns_profile(provider_id):
            self.get_profile(provider_id)
            self._run_pins[run_id] = provider_id.removeprefix("managed:")

    def release_run(self, run_id):
        if self._run_pins.pop(run_id, None) is not None and self._runtime is not None:
            self._runtime.touch()

    async def recover(self, provider_id, expected_identity=None):
        """Drain the previous engine and reload the exact captured model/config."""
        model_id = provider_id.removeprefix('managed:')
        record = self.catalog.get(model_id)
        if expected_identity is not None and record['digest'] != expected_identity.get('artifact_digest'):
            raise CoreDataError('model_recovery_identity_changed', '模型权重身份已变化，请重新选择模型后重试。')
        async with self._permit:
            guard = self.load_guard(record) if self.load_guard else nullcontext()
            async with guard:
                await self._backend().load(model_id, threading.Event())
            actual = self.runtime_identity(provider_id)
            expected = {key: value for key, value in (expected_identity or actual).items() if key != 'session_id'}
            current = {key: value for key, value in actual.items() if key != 'session_id'}
            if current != expected:
                await self._backend().unload()
                raise CoreDataError('model_recovery_identity_changed', '模型、设备或推理配置已变化，请核对配置后重试。')
            return self.get_profile(provider_id)

    def prepare(self, model_id, preparation_id=None):
        self.catalog.get(model_id)
        self.pinned()
        if preparation_id is not None and (
            preparation_id not in self._preparations or self._preparations[preparation_id][0] != model_id
        ):
            raise CoreDataError("model_preparation_expired", "模型加载预约已失效，请重新发送。")
        existing_waiter = preparation_id is not None
        preparation_id = preparation_id or uuid.uuid4().hex
        # Polling renews this lease. Abandoned windows cannot pin a model forever.
        self._preparations[preparation_id] = (model_id, time.monotonic() + 30)
        if self._operation == "runtime.configure_load":
            return self._preparation_response(preparation_id, 'waiting')
        if self._runtime and self._runtime.matches("managed:" + model_id):
            self._preparations[preparation_id] = (model_id, time.monotonic() + self.load_timeout + 60)
            self._runtime.touch()
            return self._preparation_response(preparation_id, 'ready')
        if self._operation:
            job = next((job for job in self.catalog.jobs()
                        if job["id"] == self._auto_load_job), None)
            if job and job.get("model_id") == model_id:
                return self._preparation_response(preparation_id, 'loading', job)
            return self._preparation_response(preparation_id, 'waiting')
        job = next((job for job in self.catalog.jobs()
                    if job["id"] == self._auto_load_job and job.get("model_id") == model_id), None)
        if job and job["status"] in {"failed", "cancelled"} and existing_waiter:
            # Existing waiters observe the original failure, rather than looping loads.
            if job["status"] == "cancelled":
                job = {**job, "error": {"code": "model_loading_cancelled", "message": "模型加载已取消，输入和附件已保留。"}}
            return self._preparation_response(preparation_id, 'failed', job)
        result = self._schedule("runtime.load", model_id=model_id)
        self._auto_load_job = result["job"]["id"]
        return self._preparation_response(preparation_id, 'loading', result['job'])

    def _preparation_response(self, preparation_id, state, job=None):
        result = {'preparation_id': preparation_id, 'state': state}
        if job is not None:
            result['job'] = dict(job)
        model_id = self._preparations[preparation_id][0]
        job_id = job['id'] if job is not None else self._preparation_jobs.get(preparation_id)
        if job_id is None and state == 'ready':
            job_id = self._model_load_jobs.get(model_id)
        if job_id is not None:
            self._preparation_jobs[preparation_id] = job_id
            if self.execution_monitor is not None:
                with self.repository._connect() as connection:
                    root = connection.execute('SELECT id FROM execution_wait_tasks WHERE run_id=?',
                        ('local_model:' + job_id,)).fetchone()
                if root is not None:
                    result['execution_task_id'] = root['id']
        return result

    def jobs(self):
        jobs = self.catalog.jobs()
        if self.execution_monitor is None:
            return jobs
        with self.repository._connect() as connection:
            roots = {row['run_id']: row for row in connection.execute(
                "SELECT id,run_id,status FROM execution_wait_tasks WHERE run_id LIKE 'local_model:%'")}
        for job in jobs:
            root = roots.get('local_model:' + job['id'])
            if root is not None:
                job['execution_task_id'] = root['id']
                job['execution_status'] = root['status']
                if root['status'] == 'waiting_recovery':
                    job['status'] = 'waiting_recovery'
        return jobs

    @staticmethod
    def _load_identity(record):
        return {key: record[key] for key in ('digest', 'template_digest', 'context_length', 'device', 'gpu_layers', 'threads')}

    def validate_execution_resume(self, task_id):
        if self.execution_monitor is None:
            raise CoreDataError('execution_replay_unsupported', '该任务没有可恢复的模型加载记录。')
        root = self.execution_monitor.get_task(task_id)
        replay = self.execution_monitor.get_replay(task_id)
        if replay is None or replay['kind'] != 'local_model_load':
            raise CoreDataError('execution_replay_unsupported', '请通过本地模型面板重新提交该操作。')
        request = replay['request']
        if root['run_id'] != 'local_model:' + request['job_id']:
            raise CoreDataError('execution_identity_conflict', '模型加载记录与执行任务不一致。')
        job = next((item for item in self.catalog.jobs() if item['id'] == request['job_id']), None)
        if job is None or job['kind'] != request['kind'] or job['model_id'] != request['model_id']:
            raise CoreDataError('execution_identity_conflict', '模型加载任务的来源身份已变化。')
        current = self._load_identity(self.catalog.get(request['model_id']))
        if current not in (request['source_identity'], request['target_identity']):
            raise CoreDataError('model_recovery_identity_changed', '模型加载配置已变化，请核对配置后重新加载。')
        if self._operation is not None or self._permit.locked() or self._run_pins:
            raise CoreDataError('model_busy', '请等待当前模型请求结束后恢复加载。')
        return request

    def resume_execution(self, task_id):
        request = self.validate_execution_resume(task_id)
        job_id, kind, model_id = request['job_id'], request['kind'], request['model_id']
        self.catalog.update_job(job_id, 'queued')
        self._operation = kind
        self._model_load_jobs[model_id] = job_id
        self._auto_load_job = job_id
        event = threading.Event()
        task = asyncio.create_task(self._run_job(job_id, kind, event, path=None,
            model_id=model_id, configuration=request.get('configuration')))
        self._jobs[job_id] = (task, event)
        return self.execution_monitor.get_task(task_id)

    def cancel_execution(self, task_id):
        root = self.execution_monitor.get_task(task_id)
        if root.get('run_id', '').startswith('local_model:'):
            self._request_cancel(root['run_id'].removeprefix('local_model:'))

    def preparation_receipt(self, preparation_id, *, provider_id, model_id):
        self.pinned()
        if not isinstance(preparation_id, str) or not preparation_id or preparation_id not in self._preparations:
            raise CoreDataError('model_preparation_expired', '模型加载预约已失效，请重新发送。')
        selected = self._preparations[preparation_id][0]
        if model_id != selected or provider_id != 'managed:' + selected:
            raise CoreDataError('model_preparation_mismatch', '模型加载预约与当前请求的模型不一致。')
        self.get_profile(provider_id)
        return self._preparation_response(preparation_id, 'ready')

    def release_preparation(self, preparation_id):
        value = self._preparations.pop(preparation_id, None)
        self._preparation_jobs.pop(preparation_id, None)
        if value and not self.pinned(value[0]) and self._auto_load_job:
            job = next((job for job in self.catalog.jobs() if job["id"] == self._auto_load_job), None)
            if job and job.get("model_id") == value[0]:
                self._request_cancel(self._auto_load_job)

    def _schedule(self, kind, *, path=None, model_id=None, configuration=None):
        job_id = uuid.uuid4().hex
        with self.repository._connect() as connection:
            connection.execute("INSERT INTO local_model_jobs(id,kind,model_id,status,progress,created_at,updated_at) VALUES (?,?,?,'queued',0,?,?)",
                               (job_id, kind, model_id, now(), now()))
        self._operation = kind
        if kind in {'runtime.load', 'runtime.configure_load'}:
            self._model_load_jobs[model_id] = job_id
        root = None
        if self.execution_monitor is not None:
            root = self.execution_monitor.start_task(run_id='local_model:' + job_id,
                policy=self.execution_policy() if callable(self.execution_policy) else self.execution_policy)
            if kind in {'runtime.load', 'runtime.configure_load'}:
                source = self._load_identity(self.catalog.get(model_id))
                target = {key: (configuration or {}).get(key, value) for key, value in source.items()}
                self.execution_monitor.set_replay(root['id'], 'local_model_load', {
                    'job_id': job_id, 'kind': kind, 'model_id': model_id, 'configuration': configuration,
                    'source_identity': source, 'target_identity': target})
        event = threading.Event()
        task = asyncio.create_task(self._run_job(job_id, kind, event, path=path, model_id=model_id, configuration=configuration))
        self._jobs[job_id] = (task, event)
        result = {"job": next(job for job in self.catalog.jobs() if job["id"] == job_id)}
        if root is not None:
            result['execution_task_id'] = root['id']
            result['job']['execution_task_id'] = root['id']
        return result

    async def _run_job(self, job_id, kind, event, *, path, model_id, configuration=None):
        if self.execution_monitor is None:
            return await self._run_job_business(job_id, kind, event, path=path, model_id=model_id, configuration=configuration)
        from agent.execution.activity import associate
        root = self.execution_monitor.start_task(run_id='local_model:' + job_id,
            policy=self.execution_policy() if callable(self.execution_policy) else self.execution_policy)
        with associate(self.execution_monitor, root['id']):
            try:
                await self._run_job_business(job_id, kind, event, path=path, model_id=model_id, configuration=configuration)
            finally:
                job = next(job for job in self.catalog.jobs() if job['id'] == job_id)
                self.execution_monitor.finish_task(root['id'], job['status'] if job['status'] in
                    {'completed', 'cancelled', 'failed'} else 'interrupted',
                    error_code=(job.get('error') or {}).get('code'))

    async def _run_job_business(self, job_id, kind, event, *, path, model_id, configuration=None):
        worker = None
        try:
            self.catalog.update_job(job_id, "running")
            if event.is_set():
                raise CancelledOperation()
            if kind in {"runtime.load", "runtime.configure_load"}:
                while self._run_pins or kind == "runtime.load" and any(
                    self._runtime and self._runtime.matches("managed:" + value[0])
                    for value in self._preparations.values() if value[1] > time.monotonic()
                ):
                    if event.is_set():
                        raise CancelledOperation()
                    await asyncio.sleep(0.1)
                async with self._permit:
                    if kind == "runtime.configure_load":
                        await self._backend().unload()
                        self.catalog.configure(model_id, configuration)
                    guard = self.load_guard(self.catalog.get(model_id)) if self.load_guard else nullcontext()
                    async with guard:
                        from agent.execution.activity import run_stage
                        from agent.execution.execution_wait import Diagnostic
                        await run_stage('main_model_loading', lambda activity: self._backend().load(model_id, event),
                            safe_to_retry=True, diagnose=lambda activity: Diagnostic(process_exited=
                                self._runtime is not None and self._runtime.process is not None
                                and self._runtime.process.poll() is not None),
                            recoverable=lambda error: getattr(error, 'code', None) == 'runtime_crashed')
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
        from agent.execution.execution_wait import current_activity
        activity = current_activity.get()
        if activity is not None:
            activity.queue(waiting=self._permit.locked(), resource='main_model')
        async with self._permit:
            if activity is not None:
                activity.queue(waiting=False, resource='main_model')
            current = self.get_profile(provider_id)
            if self._runtime.session_id != session \
                    or (current.base_url, current.api_key) != (profile.base_url, profile.api_key):
                raise CoreDataError("model_not_loaded", "The managed model session changed before invocation.")
            self._runtime.touch()
            try:
                yield profile
            finally:
                self._runtime.touch()

    @asynccontextmanager
    async def observe_activity(self, provider_id):
        from agent.execution.execution_wait import current_activity
        from agent.execution.activity import watch_process
        activity = current_activity.get()
        runtime = self._runtime
        sampler = None
        if activity is not None and runtime is not None and runtime.matches(provider_id):
            sampler = asyncio.create_task(watch_process(runtime.process, activity, heartbeat=runtime.heartbeat, job=runtime.job))
        try:
            yield
        finally:
            if sampler is not None:
                sampler.cancel()
                await asyncio.gather(sampler, return_exceptions=True)

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
