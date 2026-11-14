"""Validated local model registry and isolated inference process ownership."""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import importlib.util
import json
import math
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import nullcontext
from pathlib import Path

from agent.storage import CoreDataError
from agent.gpu_resources import system_memory
from agent.rag.model_profiles import PROFILES, validate_onnx_package
from agent.rag.worker_client import WorkerClient
from agent.execution.execution_wait import current_activity, current_task_id, current_monitor, Diagnostic

_UNSAFE = {".bin", ".pt", ".pth", ".pkl", ".pickle", ".py", ".pyc", ".pyo", ".exe", ".dll", ".so", ".bat", ".cmd", ".ps1", ".sh"}
_DTYPES = {"BOOL": 1, "U8": 1, "I8": 1, "F8_E4M3": 1, "F8_E5M2": 1, "I16": 2, "U16": 2, "F16": 2, "BF16": 2, "I32": 4, "U32": 4, "F32": 4, "I64": 8, "U64": 8, "F64": 8}


def _json(path):
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError(f"JSON metadata exceeds size limit: {path.name}")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _linked(path):
    return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)


def _raise_walk_error(error):
    raise error


def _model_file_error(error, root, operation):
    if isinstance(error, PermissionError):
        path = str(error.filename or root)
        return CoreDataError('rag_model_access_denied',
            f'辅助模型目录或文件 {path} 访问受限。请在资源管理器中确认当前 Windows 账户的读取权限，然后重新检测模型。',
            {'path': path, 'winerror': getattr(error, 'winerror', None), 'operation': operation})
    return CoreDataError('rag_model_error', f'{operation}: {error}')


def _files(root):
    if _linked(root):
        raise ValueError("Model directory cannot be a symbolic link or junction")
    result = []
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=_raise_walk_error):
        for name in dirs + files:
            path = Path(directory) / name
            if _linked(path):
                raise ValueError(f"Linked model path is forbidden: {path.relative_to(root)}")
        for name in files:
            path = Path(directory) / name
            if name == ".rag-import.json":
                continue
            if path.suffix.lower() in _UNSAFE:
                raise ValueError(f"Unsafe executable or pickle model file: {path.relative_to(root)}")
            result.append(path)
    return sorted(result)


def _safe_relative(root, name):
    relative = Path(name)
    if not name or relative.is_absolute() or ".." in relative.parts or ":" in name or "\\" in name:
        raise ValueError(f"Invalid model package path: {name}")
    target = root / relative
    if not target.resolve().is_relative_to(root.resolve()) or not target.is_file() or _linked(target):
        raise ValueError(f"Missing or linked model file: {name}")
    return target


def _no_remote_code(value):
    if isinstance(value, dict):
        if value.get("auto_map") or value.get("custom_pipelines"):
            raise ValueError("Custom model/tokenizer code is unsupported")
        for item in value.values():
            _no_remote_code(item)
    elif isinstance(value, list):
        for item in value:
            _no_remote_code(item)


def _validate_safetensors(path, *, include_header=False):
    total = path.stat().st_size
    with path.open("rb") as stream:
        raw = stream.read(8)
        if len(raw) != 8:
            raise ValueError(f"Truncated safetensors file: {path.name}")
        length = struct.unpack("<Q", raw)[0]
        if length < 2 or length > min(total - 8, 100 * 1024 * 1024):
            raise ValueError(f"Invalid safetensors header: {path.name}")
        header = json.loads(stream.read(length))
    if not isinstance(header, dict):
        raise ValueError("Invalid safetensors tensor index")
    ranges = []
    for name, tensor in header.items():
        if name == "__metadata__":
            continue
        shape = tensor["shape"]
        offsets = tensor["data_offsets"]
        if not isinstance(shape, list) or any(type(size) is not int or size < 0 for size in shape):
            raise ValueError("Invalid safetensors tensor shape")
        if len(offsets) != 2 or any(type(offset) is not int for offset in offsets):
            raise ValueError("Invalid safetensors offsets")
        start, end = offsets
        size = math.prod(shape) * _DTYPES[tensor["dtype"]]
        if start < 0 or end - start != size or end > total - length - 8:
            raise ValueError(f"Invalid tensor byte range: {name}")
        ranges.append((start, end))
    if not ranges:
        raise ValueError("Safetensors file contains no tensors")
    cursor = 0
    for start, end in sorted(ranges):
        if start != cursor:
            raise ValueError("Safetensors tensor data has gaps or overlaps")
        cursor = end
    if cursor != total - length - 8:
        raise ValueError("Safetensors file has trailing unindexed data")
    return (header, length) if include_header else {name for name in header if name != "__metadata__"}




class ModelStore:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._state_path = self.data_dir / "rag-models.json"
        self._lock = threading.RLock()
        self._mutation_lock = threading.RLock()
        self._runtime_lock = threading.Lock()
        self._runtime_verified = None
        self._workers: dict[str, WorkerClient] = {}
        self._closed = False
        self._device_locks = {"cpu": asyncio.Lock(), "cuda": asyncio.Lock()}
        self._verified_files = {}
        self._loaded_models = {}
        self._resident_workers = OrderedDict()
        self.idle_timeout = 1800
        self._worker_last_used = {}
        self._idle_task = None
        self._retired_workers = set()
        self._model_peaks = {}
        self._rerank_partial = OrderedDict()
        self.gpu_resources = None
        self.execution_monitor = None
        self._state = _json(self._state_path) if self._state_path.exists() else {"schema_version": 1, "root": str(self.data_dir / "models"), "models": []}
        if self._state.get("schema_version") != 1:
            raise CoreDataError("rag_model_error", "Unsupported local model registry version")
        self.root = Path(self._state["root"])
        self.root.mkdir(parents=True, exist_ok=True)

    def _save(self):
        _write_json(self._state_path, self._state)

    def list_models(self):
        with self._lock:
            return [dict(record) for record in self._state["models"]]

    def runtime_status(self):
        try:
            if getattr(sys, "frozen", False):
                base = Path(sys.executable).resolve().parent / "rag-runtime"
                metadata = _json(base / "runtime.json")
                if metadata.get("schema_version") != 1 or metadata.get("protocol_version") != 2 or _linked(base):
                    raise ValueError("Invalid inference runtime manifest")
                executable = _safe_relative(base, metadata["executable"])
                if not isinstance(metadata.get("files"), dict) or metadata["executable"] not in metadata["files"]:
                    raise ValueError("Inference runtime executable is unlisted")
                command = [str(executable)]
            else:
                command = [sys.executable, "-m", "agent.rag.worker"]
            libraries = {name: importlib.util.find_spec(name) is not None for name in ("torch", "transformers", "safetensors", "onnx", "onnxruntime")}
            return {"available": True, "command": command[0], "version": 2, "libraries": libraries,
                    "cuda_requires_probe": True, "source_runtime": not getattr(sys, "frozen", False),
                    "integrity_check": "on_load"}
        except (CoreDataError, OSError, ValueError, KeyError, TypeError) as exc:
            return {"available": False, "version": 2, "error": str(exc)}

    def inspect(self, path, profile=None):
        root = Path(path).absolute()
        result = {"path": str(root), "valid": False, "profile": profile, "formats": [], "files": [], "errors": []}
        try:
            if not root.is_dir():
                raise ValueError("Select a complete local model directory")
            files = _files(root)
            result["files"] = [{"path": str(item.relative_to(root)).replace("\\", "/"), "size_bytes": item.stat().st_size} for item in files]
            config = _json(root / "config.json")
            tokenizer = _json(root / "tokenizer_config.json")
            _no_remote_code(config)
            _no_remote_code(tokenizer)
            if not any((root / name).is_file() for name in ("tokenizer.json", "sentencepiece.bpe.model", "tokenizer.model")):
                raise ValueError("Complete tokenizer assets are required")
            metadata = _json(root / "rag-model.json") if (root / "rag-model.json").exists() else None
            inferred = metadata.get("profile") if metadata else None
            if inferred is None:
                architectures = config.get("architectures", [])
                matches = [key for key, spec in PROFILES.items() if spec.model_type == config.get("model_type") and any(name in spec.architectures for name in architectures)]
                if len(matches) > 1 and profile is None:
                    raise ValueError("Ambiguous model architecture requires an explicit model profile")
                inferred = matches[0] if len(matches) == 1 else None
            profile = profile or inferred
            if profile not in PROFILES:
                raise ValueError("Select one of the four supported model profiles")
            spec = PROFILES[profile]
            if config.get("model_type") != spec.model_type or config.get("hidden_size") != 1024:
                raise ValueError("Model config does not match the selected profile")
            if config.get("architectures") and not any(name in spec.architectures for name in config["architectures"]):
                raise ValueError("Model architecture does not match the selected profile")
            backend = "onnx" if metadata else "transformers"
            result.update(profile=profile, inferred_profile=inferred, backend=backend, role=spec.role, dimension=spec.dimension, formats=["onnx"] if metadata else ["safetensors"])
            if metadata:
                try:
                    validate_onnx_package(root, metadata, profile)
                except Exception as exc:
                    raise ValueError(f"Invalid ONNX package: {exc}") from exc
            else:
                weights = [item for item in files if item.suffix == ".safetensors"]
                if not weights:
                    raise ValueError("Complete safetensors model weights are required")
                tensors = {}
                for item in weights:
                    tensors[item.name] = _validate_safetensors(item)
                index_path = root / "model.safetensors.index.json"
                if index_path.exists():
                    index = _json(index_path).get("weight_map", {})
                    if not index:
                        raise ValueError("Empty safetensors shard index")
                    for tensor, filename in index.items():
                        _safe_relative(root, filename)
                        if tensor not in tensors.get(filename, set()):
                            raise ValueError(f"Missing tensor/shard: {tensor} in {filename}")
                elif not (root / "model.safetensors").is_file():
                    raise ValueError("Sharded weights require model.safetensors.index.json")
            result["size_bytes"] = sum(item["size_bytes"] for item in result["files"])
            result["valid"] = True
        except (OSError, ValueError, KeyError, TypeError, IndexError, OverflowError, ImportError) as exc:
            result["errors"].append(str(exc))
        return result

    async def import_model(self, path, profile, device="cpu"):
        self._device(device)
        return await asyncio.to_thread(self._import, Path(path), profile, device)

    def _import(self, source, profile, device):
        with self._mutation_lock:
            inspection = self.inspect(source, profile)
            if not inspection["valid"]:
                raise CoreDataError("rag_model_error", "; ".join(inspection["errors"]))
            if source.resolve().is_relative_to(self.root.resolve()):
                raise CoreDataError("rag_model_error", "Select a model source outside the managed model root")
            staging = Path(tempfile.mkdtemp(prefix=".import-", dir=self.root))
            try:
                hashes = {}
                for item in inspection["files"]:
                    relative = item["path"]
                    origin = _safe_relative(source, relative)
                    before = _hash(origin)
                    target = staging / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(origin, target)
                    if _hash(target) != before or _hash(origin) != before:
                        raise CoreDataError("rag_model_error", "Model source changed while copying")
                    hashes[relative] = before
                copied = self.inspect(staging, profile)
                if not copied["valid"]:
                    raise CoreDataError("rag_model_error", "Copied model validation failed: " + "; ".join(copied["errors"]))
                digest = hashlib.sha256(json.dumps({"profile": profile, "files": hashes}, sort_keys=True).encode()).hexdigest()
                model_id = digest[:24]
                with self._lock:
                    existing = next((record for record in self._state["models"] if record["id"] == model_id), None)
                    if existing:
                        return dict(existing)
                    _write_json(staging / ".rag-import.json", {"schema_version": 1, "digest": digest, "profile": profile, "files": hashes})
                    target = self.root / model_id
                    if target.exists():
                        raise CoreDataError("rag_model_error", "Unregistered model target already exists")
                    os.replace(staging, target)
                    record = {"id": model_id, "name": source.name, "role": copied["role"], "backend": copied["backend"], "profile": profile, "device": device, "status": "imported", "digest": digest, "dimension": copied["dimension"], "size_bytes": copied["size_bytes"]}
                    self._state["models"].append(record)
                    try:
                        self._save()
                    except Exception:
                        self._state["models"].pop()
                        shutil.rmtree(target)
                        raise
                    return dict(record)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)

    @staticmethod
    def _device(device):
        if device not in {"cpu", "cuda"}:
            raise CoreDataError("rag_model_error", "Select cpu or cuda explicitly")

    def _record(self, model_id):
        for record in self._state["models"]:
            if record["id"] == model_id:
                return record
        raise CoreDataError("rag_model_error", "Local model does not exist")

    def set_device(self, model_id, device):
        self._device(device)
        with self._lock:
            record = self._record(model_id)
            previous = dict(record)
            record.update(device=device, status="imported")
            try:
                self._save()
            except Exception:
                record.update(previous)
                raise
            self._invalidate_cached(model_id)
            return dict(record)

    def set_root(self, path):
        target = Path(path).absolute()
        with self._mutation_lock:
            if target.resolve() == self.root.resolve():
                return {"root": str(self.root)}
            self._invalidate_cached()
            if target.resolve().is_relative_to(self.root.resolve()) or self.root.resolve().is_relative_to(target.resolve()):
                raise CoreDataError("rag_model_error", "Model roots must not contain each other")
            target.mkdir(parents=True, exist_ok=True)
            if _linked(target):
                raise CoreDataError("rag_model_error", "Model root cannot be a symbolic link or junction")
            copied = []
            try:
                for record in self.list_models():
                    self._verify(record)
                    destination = target / record["id"]
                    if destination.exists():
                        raise CoreDataError("rag_model_error", "Destination already contains a model with this id")
                    staging = Path(tempfile.mkdtemp(prefix=".move-", dir=target))
                    try:
                        shutil.copytree(self.root / record["id"], staging, dirs_exist_ok=True)
                        self._verify(record, root=staging, direct=True)
                        os.replace(staging, destination)
                    finally:
                        if staging.exists():
                            shutil.rmtree(staging)
                    copied.append(destination)
                with self._lock:
                    previous = self._state["root"]
                    self._state["root"] = str(target)
                    try:
                        self._save()
                    except Exception:
                        self._state["root"] = previous
                        raise
                    self.root = target
            except Exception:
                for destination in copied:
                    shutil.rmtree(destination)
                raise
            return {"root": str(self.root)}

    def remove(self, model_id):
        with self._mutation_lock:
            self._invalidate_cached(model_id)
            with self._lock:
                record = self._record(model_id)
                target = self.root / record["id"]
                if target.parent.resolve() != self.root.resolve() or _linked(target):
                    raise CoreDataError("rag_model_error", "Invalid managed model path")
                tombstone = self.root / (".remove-" + record["id"] + "-" + uuid.uuid4().hex)
                os.replace(target, tombstone)
                previous = self._state["models"]
                self._state["models"] = [item for item in previous if item["id"] != model_id]
                try:
                    self._save()
                except Exception:
                    self._state["models"] = previous
                    os.replace(tombstone, target)
                    raise
            try:
                shutil.rmtree(tombstone)
            except OSError:
                return {"removed": model_id, "cleanup_pending": str(tombstone)}
            return {"removed": model_id}

    def _verify(self, record, root=None, direct=False):
        root = (root if direct else (root or self.root) / record["id"])
        try:
            files = _files(root)
            manifest = _json(root / ".rag-import.json")
            hashes = {str(item.relative_to(root)).replace("\\", "/"): _hash(item) for item in files}
            digest = hashlib.sha256(json.dumps({"profile": record["profile"], "files": hashes}, sort_keys=True).encode()).hexdigest()
            if manifest.get("files") != hashes or digest != record["digest"] or manifest.get("digest") != digest:
                raise ValueError("Model integrity hash changed")
        except (OSError, ValueError, KeyError) as exc:
            raise _model_file_error(exc, root, 'Model integrity verification failed') from exc
        return root

    def _command(self, force=True):
        with self._runtime_lock:
            return self._runtime_command(force)

    def _runtime_command(self, force):
        if not getattr(sys, "frozen", False):
            return [sys.executable, "-m", "agent.rag.worker"]
        base = Path(sys.executable).resolve().parent / "rag-runtime"
        manifest_path = base / "runtime.json"
        try:
            metadata = _json(manifest_path)
            if metadata.get("schema_version") != 1 or metadata.get("protocol_version") != 2:
                raise ValueError("Unsupported inference runtime manifest version")
            executable = _safe_relative(base, metadata["executable"])
            hashes = metadata["files"]
            actual = set()
            manifest_stat = manifest_path.stat()
            stamp = [("runtime.json", manifest_stat.st_ino, manifest_stat.st_size, manifest_stat.st_mtime_ns, manifest_stat.st_ctime_ns)]
            if _linked(base):
                raise ValueError("Linked inference runtime directory")
            for directory, dirs, files in os.walk(base, followlinks=False):
                for name in dirs + files:
                    item = Path(directory) / name
                    if _linked(item):
                        raise ValueError("Linked inference runtime file")
                for name in files:
                    item = Path(directory) / name
                    if item != manifest_path:
                        relative = item.relative_to(base).as_posix()
                        actual.add(relative)
                        stat = item.stat()
                        stamp.append((relative, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
            if actual != set(hashes):
                raise ValueError("Inference runtime contains unlisted or missing files")
            if not hashes or metadata["executable"] not in hashes:
                raise ValueError("Inference runtime executable is unlisted")
            identity = (str(base), tuple(sorted(stamp)))
            if force or self._runtime_verified != identity:
                for name, expected in hashes.items():
                    if _hash(_safe_relative(base, name)) != expected:
                        raise ValueError(f"Inference runtime file hash mismatch: {name}")
                self._runtime_verified = identity
            return [str(executable)]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise CoreDataError("rag_model_error", f"Trusted packaged inference runtime unavailable: {exc}") from exc

    def _stat_identity(self, root):
        try:
            files = _files(root) + [root / ".rag-import.json"]
            result = []
            for path in files:
                stat = path.stat()
                result.append((path.relative_to(root).as_posix(), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
            return tuple(result)
        except (OSError, ValueError) as exc:
            raise _model_file_error(exc, root, 'Model integrity stat check failed') from exc

    def _verified_path(self, record, root, force):
        path = root / record["id"]
        key = (str(root), record["id"], record["digest"])
        before = self._stat_identity(path)
        with self._lock:
            cached = self._verified_files.get(key)
        if force or cached != before:
            self._verify(record, root=root)
            after = self._stat_identity(path)
            if before != after:
                raise CoreDataError("rag_model_error", "Model changed during integrity verification")
            with self._lock:
                self._verified_files[key] = after
        return path

    def set_idle_timeout(self, seconds):
        if type(seconds) is not int or seconds != 0 and not 60 <= seconds <= 86400:
            raise CoreDataError('invalid_settings', '辅助模型驻留时间须为 1 至 1440 分钟，或选择手动卸载。')
        self.idle_timeout = seconds
        self._worker_last_used = {key: time.monotonic() for key in self._resident_workers}

    def _touch_cached(self, key):
        self._worker_last_used[key] = time.monotonic()
        if self._idle_task is None or self._idle_task.done():
            self._idle_task = asyncio.create_task(self._watch_idle())

    async def _watch_idle(self):
        try:
            while not self._closed and self._resident_workers:
                await asyncio.sleep(1)
                await self._release_idle_workers()
        finally:
            if self._idle_task is asyncio.current_task():
                self._idle_task = None

    async def _release_idle_workers(self, now=None):
        if not self.idle_timeout:
            return
        now = time.monotonic() if now is None else now
        for device, lock in self._device_locks.items():
            if lock.locked():
                continue
            async with lock:
                for key in list(self._resident_workers):
                    if key[2] == device and now - self._worker_last_used.get(key, now) >= self.idle_timeout:
                        await self._discard_worker(key)

    def _invalidate_cached(self, model_id=None):
        for key in list(self._resident_workers):
            if model_id is None or key[0] == model_id:
                worker = self._resident_workers.pop(key, None)
                self._worker_last_used.pop(key, None)
                if worker is None:
                    continue
                worker.invalidate()
                self._retired_workers.add(worker)
                if self._workers.get(key[2]) is worker:
                    self._workers.pop(key[2], None)
                    self._loaded_models.pop(key[2], None)
        self._verified_files.clear()
        self._rerank_partial.clear()

    async def _discard_worker(self, key):
        worker = self._resident_workers.pop(key, None)
        self._worker_last_used.pop(key, None)
        if worker is None:
            return
        device = key[2]
        if self._workers.get(device) is worker:
            self._workers.pop(device, None)
            self._loaded_models.pop(device, None)
        await worker.close()

    async def invalidate(self, model_id=None):
        """Release native handles before registry mutation or root migration."""
        for device in ('cpu', 'cuda'):
            async with self._device_locks[device]:
                for key in list(self._resident_workers):
                    if key[2] == device and (model_id is None or key[0] == model_id):
                        await self._discard_worker(key)
        self._rerank_partial.clear()
        self._verified_files.clear()

    async def _admit_cpu(self, record):
        required = max(2 * record['size_bytes'] + 512 * 1024**2,
                       self._model_peaks.get((record['id'], record['digest']), 0))
        available = system_memory().get('available_physical_bytes')
        cpu_keys = [key for key in self._resident_workers if key[2] == 'cpu']
        while cpu_keys and (len(cpu_keys) >= 2 or available is None or available - required < 2 * 1024**3):
            key = cpu_keys.pop(0)
            await self._discard_worker(key)
            available = system_memory().get('available_physical_bytes')
        if available is not None and available - required < 2 * 1024**3:
            raise CoreDataError('rag_resource_exhausted', '辅助检索模型可用内存不足。',
                                {'device': 'cpu', 'resource': 'cpu_memory', 'reserve_bytes': 2 * 1024**3})

    async def _request(self, model_id, action, **params):
        monitor = current_monitor.get() or self.execution_monitor
        if current_activity.get() is not None or monitor is None:
            return await self._request_once(model_id, action, **params)
        task_id = current_task_id.get()
        owned = task_id is None
        if owned:
            task_id = monitor.start_task(run_id='rag-model-' + uuid.uuid4().hex)['id']
        async def operation(activity):
            return await self._request_once(model_id, action, **params)
        try:
            result = await monitor.execute_stage(task_id, 'rag_' + action, operation,
                safe_to_retry=True, diagnose=self.diagnose,
                recoverable=lambda error: isinstance(error, CoreDataError) and error.code == 'rag_worker_crash')
        except BaseException as error:
            if owned:
                monitor.finish_task(task_id, 'cancelled' if isinstance(error, asyncio.CancelledError) else 'failed',
                                    error_code=getattr(error, 'code', None))
            raise
        if owned:
            monitor.finish_task(task_id)
        return result

    def diagnose(self, activity):
        worker = getattr(activity, '_rag_worker', None)
        if worker is None:
            return Diagnostic(activity_observed=None)
        return worker.diagnose()

    async def _request_once(self, model_id, action, *, progress=None, partial=None, checkpoint_identity=None, **params):
        if self._closed:
            raise CoreDataError("rag_model_error", "Local model store is closed")
        with self._lock:
            record = dict(self._record(model_id))
            root = self.root
        if action == "embed" and record["role"] != "embedding" or action == "rerank" and record["role"] != "reranker":
            raise CoreDataError("rag_model_error", "Selected local model has the wrong role")
        device = record["device"]
        activity = current_activity.get()
        model_details = {'model_role': record['role'], 'model_name': record['name'], 'device': device}
        if activity:
            activity.queue(True, **model_details)
        key = (record['id'], record['digest'], device)
        def is_resident():
            worker = self._resident_workers.get(key)
            return worker is not None and worker.process is not None and worker.process.returncode is None
        guard = self.gpu_resources.auxiliary(record, resident=is_resident) if self.gpu_resources else nullcontext()
        async with guard, self._device_locks[device]:
            if activity:
                activity.queue(False, **model_details)
                activity.progress(stage='integrity_verification', **model_details)
            if self._closed:
                raise CoreDataError("rag_model_error", "Local model store is closed")
            identity = (str(root), record["id"], record["digest"])
            with self._lock:
                current = self._record(model_id)
                if self.root != root or any(current[field] != record[field] for field in ('digest', 'device', 'profile', 'backend')):
                    raise CoreDataError('rag_model_changed', 'The selected model changed while waiting for resources.')
            worker = self._resident_workers.get(key)
            force = worker is None or not is_resident()
            try:
                path = await asyncio.to_thread(self._verified_path, record, root, force)
                fingerprint = self._stat_identity(path)
                if worker is not None and (force or worker.model_fingerprint != fingerprint or worker.model_root != str(root)):
                    await self._discard_worker(key)
                    worker = None
                if worker is None:
                    if device == 'cpu':
                        await self._admit_cpu(record)
                    else:
                        for old in list(self._resident_workers):
                            if old[2] == 'cuda':
                                await self._discard_worker(old)
                    command = await asyncio.to_thread(self._command)
                    if self._closed:
                        raise CoreDataError('rag_model_error', 'Local model store is closed')
                    with self._lock:
                        current = self._record(model_id)
                        if self.root != root or any(current[field] != record[field] for field in ('digest', 'device', 'profile', 'backend')):
                            raise CoreDataError('rag_model_changed', 'The selected model changed during admission.')
                    worker = WorkerClient(command)
                    worker.model_fingerprint = fingerprint
                    worker.model_root = str(root)
                    self._resident_workers[key] = worker
                self._resident_workers.move_to_end(key)
                self._workers[device] = worker
                self._loaded_models[device] = (worker, identity)
                if activity:
                    activity._rag_worker = worker
                    activity.progress(stage='worker_starting' if force else 'model_ready', **model_details)
                request_params = {"path": str(path), "profile": record["profile"], "backend": record["backend"],
                                  "device": device, "digest": record["digest"], 'protocol_version': 2, **params}
                if activity:
                    request_params['execution_identity'] = {'task_id': activity.task_id,
                        'revision': activity.revision, 'attempt': activity.attempt}
                if action == 'probe':
                    result = await worker.request(action, request_params)
                else:
                    texts = params['texts']
                    batch_size = 2 if record['backend'] == 'onnx' else (4 if action == 'rerank' else 8)
                    checkpoint = partial if partial is not None else {}
                    checkpoint_key = None
                    if activity and checkpoint_identity is not None:
                        checkpoint_key = 'rag_scores_' + hashlib.sha256(
                            json.dumps(checkpoint_identity, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
                        saved = activity.load_checkpoint(checkpoint_key, checkpoint_identity)
                        if saved is not None:
                            if (not isinstance(saved, dict) or any(
                                not key.isdigit() or not 0 <= int(key) < len(texts)
                                or type(value) not in (int, float) or not math.isfinite(value)
                                or not 0 <= value <= 1 for key, value in saved.items())):
                                raise CoreDataError('rag_model_error', 'Stored reranker checkpoint is invalid')
                            checkpoint.update((int(key), value) for key, value in saved.items())
                    pending_indices = [index for index in range(len(texts)) if index not in checkpoint]
                    completed = len(checkpoint)
                    model_progress = model_details
                    if progress:
                        inference_stage = 'reranking' if action == 'rerank' else 'embedding'
                        event = {'stage': inference_stage if is_resident() else 'model_loading',
                                 **model_progress, 'completed': completed, 'total': len(texts)}
                        pending = progress(event)
                        if inspect.isawaitable(pending):
                            await pending
                    async def worker_progress(event):
                        if event.get('kind') == 'heartbeat':
                            if activity:
                                activity.heartbeat(worker_sequence=event.get('sequence'), **model_details)
                                activity.resource(active=event.get('resource_active'),
                                    cpu_seconds=event.get('cpu_seconds'),
                                    peak_memory_bytes=event.get('peak_memory_bytes'), **model_details)
                            return
                        if activity:
                            activity.progress(completed, len(texts), **{key: value for key, value in event.items()
                                if key not in {'completed', 'total'}}, **model_details)
                        if progress:
                            pending = progress({**event, **model_progress, 'completed': completed, 'total': len(texts)})
                            if inspect.isawaitable(pending):
                                await pending
                    if action == 'rerank' and len(pending_indices) > 1:
                        if activity:
                            activity.progress(completed, len(texts), stage='tokenization', **model_details)
                        lengths = await worker.request('rerank_lengths', {
                            **request_params, 'texts': [texts[index] for index in pending_indices],
                            'report_progress': True}, progress=worker_progress)
                        if (not isinstance(lengths, list) or len(lengths) != len(pending_indices)
                                or any(type(length) is not int or length < 0 for length in lengths)):
                            raise CoreDataError('rag_model_error', 'Inference returned invalid token lengths')
                        pending_indices = [index for _, index in sorted(zip(lengths, pending_indices))]
                    while pending_indices:
                        batch_indices = pending_indices[:batch_size]
                        batch = [texts[index] for index in batch_indices]
                        try:
                            batch_result = await worker.request(action, {**request_params, 'texts': batch,
                                                                         'report_progress': True}, progress=worker_progress)
                        except CoreDataError as exc:
                            if exc.code != 'rag_resource_exhausted' or batch_size == 1:
                                raise
                            batch_size = 1
                            await asyncio.to_thread(self._verified_path, record, root, True)
                            continue
                        if not isinstance(batch_result, list) or len(batch_result) != len(batch):
                            raise CoreDataError('rag_model_error', 'Inference returned an invalid batch size')
                        if action == 'rerank' and any(type(value) not in (int, float) or not math.isfinite(value)
                                                      or not 0 <= value <= 1 for value in batch_result):
                            raise CoreDataError('rag_model_error', 'Inference returned invalid reranker scores')
                        checkpoint.update(zip(batch_indices, batch_result))
                        if activity and checkpoint_key is not None:
                            if not activity.checkpoint(checkpoint_key, checkpoint_identity,
                                                       {str(key): value for key, value in checkpoint.items()}):
                                raise CoreDataError('execution_revision_conflict', 'Reranker result belongs to a superseded attempt')
                        del pending_indices[:len(batch_indices)]
                        completed = len(checkpoint)
                        if activity:
                            activity.progress(completed, len(texts), stage='result_validation', **model_details)
                        if progress:
                            pending = progress({'stage': 'reranking' if action == 'rerank' else 'embedding',
                                                'completed': completed, 'total': len(texts)})
                            if inspect.isawaitable(pending):
                                await pending
                    result = [checkpoint[index] for index in range(len(texts))]
                self._model_peaks[(record['id'], record['digest'])] = max(
                    self._model_peaks.get((record['id'], record['digest']), 0),
                    getattr(worker, 'peak_memory_bytes', 0))
                if self._resident_workers.get(key) is not worker:
                    raise CoreDataError('rag_model_changed', 'The selected model changed during inference.')
                self._loaded_models[device] = (worker, identity)
                return result
            except asyncio.CancelledError:
                await self._discard_worker(key)
                raise
            except Exception:
                await self._discard_worker(key)
                raise
            finally:
                if key in self._resident_workers:
                    self._touch_cached(key)

    async def release_cuda(self):
        async with self._device_locks["cuda"]:
            for key in list(self._resident_workers):
                if key[2] == 'cuda':
                    await self._discard_worker(key)

    async def probe(self, model_id):
        try:
            result = await self._request(model_id, "probe")
            status = "ready"
        except CoreDataError as exc:
            result = {"available": False, "error": str(exc)}
            status = "unavailable"
        with self._lock:
            record = self._record(model_id)
            record["status"] = status
            self._save()
        return {"model_id": model_id, **result}

    async def embed(self, model_id, texts, query=False, *, progress=None):
        if not isinstance(texts, list) or any(not isinstance(text, str) for text in texts):
            raise CoreDataError("rag_model_error", "texts must be a list of strings")
        result = await self._request(model_id, "embed", texts=texts, query=bool(query), progress=progress)
        dimension = self._record(model_id)["dimension"]
        if len(result) != len(texts) or any(len(vector) != dimension or any(not math.isfinite(value) for value in vector) for vector in result):
            raise CoreDataError("rag_model_error", "Inference returned invalid embedding dimensions or values")
        return result

    async def rerank(self, model_id, query, texts, *, token_budget=2048, progress=None, cache_key=None,
                     checkpoint_context=None, timeout=None):
        if not isinstance(query, str) or not isinstance(texts, list) or any(not isinstance(text, str) for text in texts):
            raise CoreDataError("rag_model_error", "Reranking requires a query and list of strings")
        partial = {}
        try:
            async with asyncio.timeout(timeout):
                record = dict(self._record(model_id))
                fingerprint = await asyncio.to_thread(self._stat_identity, self.root / model_id)
                checkpoint_identity = {'root': str(self.root), 'model_id': model_id, 'digest': record['digest'],
                    'device': record['device'], 'profile': record['profile'], 'backend': record['backend'],
                    'fingerprint': fingerprint, 'query': query, 'token_budget': token_budget, 'context': cache_key,
                    'sources': checkpoint_context,
                    'candidates': [hashlib.sha256(text.encode('utf-8')).hexdigest() for text in texts]}
                identity = json.dumps(checkpoint_identity, sort_keys=True, ensure_ascii=True)
                partial = self._rerank_partial.setdefault(identity, {})
                self._rerank_partial.move_to_end(identity)
                while len(self._rerank_partial) > 8:
                    self._rerank_partial.popitem(last=False)
                result = await self._request(model_id, 'rerank', query=query, texts=texts,
                                             token_budget=token_budget, progress=progress, partial=partial,
                                             checkpoint_identity=checkpoint_identity)
        except TimeoutError as exc:
            raise CoreDataError('rag_rerank_timeout', '辅助检索重排超过时间预算，可重试继续。',
                                {'completed': len(partial), 'total': len(texts), 'timeout_seconds': timeout,
                                 'model_id': model_id, 'retryable': True}) from exc
        if len(result) != len(texts) or any(not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1 for value in result):
            raise CoreDataError("rag_model_error", "Inference returned invalid reranker scores")
        return result

    async def close(self):
        self._closed = True
        idle, self._idle_task = self._idle_task, None
        if idle is not None and idle is not asyncio.current_task():
            idle.cancel()
            await asyncio.gather(idle, return_exceptions=True)
        self._worker_last_used.clear()
        workers, self._resident_workers = self._resident_workers, OrderedDict()
        self._workers.clear()
        self._loaded_models.clear()
        self._verified_files.clear()
        self._rerank_partial.clear()
        retired, self._retired_workers = self._retired_workers, set()
        await asyncio.gather(*(worker.close() for worker in set(workers.values()) | retired))
