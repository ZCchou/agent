"""Durable catalog and atomic, cooperative-cancellation file operations."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

from agent.storage import CoreDataError
from .gguf import inspect_gguf
from .paths import checked_path, file_hash, relative_file

_STAGING = re.compile(r"job-[a-f0-9]{32}")
_MODEL_ID = re.compile(r"[a-f0-9]{64}")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def adapter_digest(inspection):
    return hashlib.sha256(canonical({"version": 1, "architecture": inspection["architecture"],
        "template_digest": inspection["template_digest"], "jinja": True,
        "enable_thinking": False}).encode("utf-8")).hexdigest()


def content_digest(files, adapter):
    return hashlib.sha256(canonical({"shards": [{"sha256": item["sha256"], "size_bytes": item["size_bytes"]}
        for item in files], "adapter_digest": adapter}).encode("utf-8")).hexdigest()


class CancelledOperation(Exception):
    pass


def cancellation(event: threading.Event):
    if event.is_set():
        raise CancelledOperation()


def copy_file(source, destination, cancel):
    """Copy bytes into an exclusively created staging file and hash those bytes."""
    digest = hashlib.sha256()
    source = checked_path(source, file=True)
    before = source.stat()
    with source.open("rb") as reader, destination.open("xb") as writer:
        for block in iter(lambda: reader.read(1024 * 1024), b""):
            cancel()
            writer.write(block)
            digest.update(block)
        writer.flush()
        os.fsync(writer.fileno())
    after = source.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise CoreDataError("model_changed", "The source model changed during import.")
    cancel()
    return digest.hexdigest()


def remove_owned_tree(path: Path):
    """Never traverse a link when cleaning up application-owned directories."""
    checked_path(path)
    if not path.exists():
        return
    for directory, dirs, files in os.walk(path, followlinks=False):
        for name in dirs + files:
            checked_path(Path(directory) / name)
    shutil.rmtree(path)


def default_threads():
    physical = None
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            function = kernel.GetLogicalProcessorInformationEx
            function.argtypes = [wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
            function.restype = wintypes.BOOL
            size = wintypes.DWORD()
            function(0, None, ctypes.byref(size))  # RelationProcessorCore
            if 0 < size.value <= 1024 * 1024:
                buffer = ctypes.create_string_buffer(size.value)
                if function(0, buffer, ctypes.byref(size)):
                    cursor, physical = 0, 0
                    while cursor + 8 <= size.value:
                        entry_size = int.from_bytes(buffer.raw[cursor + 4:cursor + 8], "little")
                        if entry_size < 8 or cursor + entry_size > size.value:
                            physical = None
                            break
                        physical += 1
                        cursor += entry_size
        except (OSError, AttributeError):
            physical = None
    return max(1, (physical or os.cpu_count() or 1) - 1)


class Catalog:
    def __init__(self, repository):
        self.repository = repository
        self._context_declarations = {}
        with repository._connect() as connection:
            row = connection.execute("SELECT value FROM local_model_settings WHERE key='root'").fetchone()
            self.root = checked_path(row[0] if row else repository.database_path.parent / "local-models")
            self.root.mkdir(parents=True, exist_ok=True)
            connection.execute("UPDATE local_model_jobs SET status='interrupted',error=?,updated_at=? WHERE status IN ('queued','running')",
                               (canonical({"code": "interrupted", "message": "Application stopped before the job completed."}), now()))
            connection.execute("UPDATE local_models SET status='imported' WHERE status IN ('loaded','loading')")
        self._recover_root()
        self._recover_import()
        self._cleanup_staging()
        credentials = checked_path(self.root / ".credentials")
        if credentials.is_dir():
            for child in credentials.iterdir():
                if re.fullmatch(r"session-[a-f0-9]{32}\.key", child.name):
                    checked_path(child, file=True).unlink()

    def _recover_import(self):
        with self.repository._connect() as connection:
            row = connection.execute("SELECT value FROM local_model_settings WHERE key='pending_import'").fetchone()
        if row is None:
            return
        journal = json.loads(row[0])
        record = journal["model"]
        if checked_path(journal["root"]) != self.root or not re.fullmatch(r"[a-f0-9]{32}", journal.get("job_id", "")):
            raise CoreDataError("invalid_storage_journal", "The import journal escaped its managed storage root.")
        if not _MODEL_ID.fullmatch(record["id"]) or not 1 <= len(record["files"]) <= 1024 \
                or content_digest(record["files"], record["adapter_digest"]) != record["id"]:
            raise CoreDataError("invalid_storage_journal", "The import publication journal has an invalid content identity.")
        with self.repository._connect() as connection:
            committed = connection.execute("SELECT * FROM local_models WHERE id=?", (record["id"],)).fetchone()
        if committed:
            self.verify(self._model(committed))
        else:
            self._remove_published_object(record)
        with self.repository._connect() as connection:
            connection.execute("DELETE FROM local_model_settings WHERE key='pending_import'")

    def _remove_published_object(self, record, *, root=None):
        """Roll back only known bytes, including a previously interrupted rollback."""
        if not _MODEL_ID.fullmatch(record["id"]):
            raise CoreDataError("invalid_storage_journal", "Invalid managed publication identifier.")
        directory = checked_path((root or self.root) / "objects" / record["id"])
        if not directory.exists():
            return
        expected = {item["name"]: item for item in record["files"]}
        for child in directory.iterdir():
            child = checked_path(child, file=True)
            if child.name == ".managed-staging":
                if child.stat().st_size != 1 or child.read_text() != "1":
                    raise CoreDataError("invalid_storage_journal", "Invalid publication ownership marker.")
                continue
            item = expected.get(child.name)
            if item is None or child.stat().st_size != item["size_bytes"] or file_hash(child) != item["sha256"]:
                raise CoreDataError("invalid_storage_journal", "Unexpected or changed files in a publication recovery directory.")
        remove_owned_tree(directory)

    def _recover_root(self):
        with self.repository._connect() as connection:
            row = connection.execute("SELECT value FROM local_model_settings WHERE key='pending_root'").fetchone()
        if row is None:
            return
        journal = json.loads(row[0])
        job_id = journal.get("job_id", "")
        if not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise CoreDataError("invalid_storage_journal", "The root migration journal has an invalid ownership identifier.")
        target = checked_path(journal["target"])
        previous = checked_path(journal["previous_root"])
        if self.root not in {previous, target} or target == previous \
                or target.is_relative_to(previous) or previous.is_relative_to(target):
            raise CoreDataError("invalid_storage_journal", "The root migration journal escaped its managed storage roots.")
        staging = checked_path(target / ".staging" / ("job-" + job_id))
        marker = checked_path(staging / ".managed-staging")
        owned = marker.is_file() and marker.stat().st_size == 1 and marker.read_text() == "1"
        if owned and self.root != target:
            # A crash between directory publication and the SQLite commit leaves
            # complete target objects. Delete only fully verified journaled copies.
            for model_id in journal.get("model_ids", []):
                model = self.get(model_id)
                directory = checked_path(target / "objects" / model_id)
                if directory.exists():
                    self._remove_published_object(model, root=target)
        if owned:
            remove_owned_tree(staging)
        with self.repository._connect() as connection:
            connection.execute("DELETE FROM local_model_settings WHERE key='pending_root'")

    def _cleanup_staging(self):
        staging = checked_path(self.root / ".staging")
        if staging.exists():
            for child in staging.iterdir():
                if _STAGING.fullmatch(child.name) and checked_path(child).is_dir():
                    marker = checked_path(child / ".managed-staging")
                    if marker.is_file() and marker.stat().st_size == 1 and marker.read_text() == "1":
                        remove_owned_tree(child)

    def models(self):
        with self.repository._connect() as connection:
            rows = connection.execute("SELECT * FROM local_models ORDER BY created_at,id").fetchall()
        return [self._model(row) for row in rows]

    def _model(self, row):
        result = dict(row)
        result["is_default"] = bool(result["is_default"])
        result["files"] = json.loads(result.pop("files_json"))
        result["error"] = json.loads(result["error"]) if result["error"] else None
        result['declared_context_length'] = None
        try:
            path = relative_file(self.root / 'objects' / result['id'], result['files'][0]['name'])
            stat = path.stat()
            identity = (str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            cached = self._context_declarations.get(result['id'])
            if cached is None or cached[0] != identity:
                inspection = inspect_gguf(path)
                cached = (identity, inspection['declared_context_length'])
                self._context_declarations[result['id']] = cached
            result['declared_context_length'] = cached[1]
        except (CoreDataError, OSError, IndexError):
            pass
        return result

    def get(self, model_id):
        with self.repository._connect() as connection:
            row = connection.execute("SELECT * FROM local_models WHERE id=?", (model_id,)).fetchone()
        if row is None:
            raise CoreDataError("model_not_found", "The managed model was not found.")
        return self._model(row)

    def _stage(self, root, job_id):
        directory = checked_path(root / ".staging" / ("job-" + job_id))
        directory.mkdir(parents=True)
        (directory / ".managed-staging").write_text("1")
        return directory

    def import_model(self, path, job_id, event):
        cancel = lambda: cancellation(event)
        cancel()
        self._recover_import()
        inspection = inspect_gguf(path, cancel=cancel)
        if shutil.disk_usage(self.root).free < inspection["size_bytes"] + 64 * 1024 * 1024:
            raise CoreDataError("insufficient_disk", "Insufficient space to copy this model safely.")
        staging = self._stage(self.root, job_id)
        try:
            files = []
            for source in map(Path, inspection["files"]):
                digest = copy_file(source, staging / source.name, cancel)
                files.append({"name": source.name, "sha256": digest, "size_bytes": (staging / source.name).stat().st_size})
            copied = inspect_gguf(staging / files[0]["name"], cancel=cancel)
            adapter = adapter_digest(copied)
            digest = content_digest(files, adapter)
            cancel()
            with self.repository._connect() as connection:
                existing = connection.execute("SELECT id FROM local_models WHERE digest=?", (digest,)).fetchone()
                if existing:
                    self.verify(self.get(existing[0]), cancel=cancel)
                    return self.get(existing[0])
                objects = checked_path(self.root / "objects")
                objects.mkdir(exist_ok=True)
                destination = checked_path(objects / digest)
                record = {"id": digest, "provider_profile_id": "managed:" + digest,
                    "name": copied["name"], "role": "generation", "format": "gguf",
                    "architecture": copied["architecture"], "quantization": copied["quantization"],
                    "context_length": min(8192, copied["context_length"]), "max_context_length": copied["context_length"],
                    "size_bytes": copied["size_bytes"], "digest": digest, "template_digest": copied["template_digest"],
                    "adapter_digest": adapter, "device": "auto", "gpu_layers": -1, "threads": default_threads(),
                    "status": "imported", "is_default": 0, "source": None, "license": None, "error": None,
                    "files_json": canonical(files), "created_at": now()}
                if destination.exists():
                    self.verify({**record, "files": files}, root=self.root, cancel=cancel)
                snapshot = {"id": digest, "files": files, "adapter_digest": adapter,
                            "template_digest": copied["template_digest"]}
                journal = {"root": str(self.root), "job_id": job_id, "model": snapshot}
                with self.repository._connect() as journal_connection:
                    journal_connection.execute("INSERT INTO local_model_settings(key,value) VALUES ('pending_import',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (canonical(journal),))
                try:
                    cancel()
                    if not destination.exists():
                        os.replace(staging, destination)
                        (destination / ".managed-staging").unlink()
                    cancel()
                    columns = ",".join(record)
                    connection.execute(f"INSERT INTO local_models ({columns}) VALUES ({','.join('?' for _ in record)})", tuple(record.values()))
                    connection.execute("DELETE FROM local_model_settings WHERE key='pending_import'")
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    self._remove_published_object(snapshot)
                    with self.repository._connect() as journal_connection:
                        journal_connection.execute("DELETE FROM local_model_settings WHERE key='pending_import'")
                    raise
            return self.get(digest)
        finally:
            if staging.exists():
                remove_owned_tree(staging)

    def verify(self, record, *, root=None, cancel=lambda: None, include_inspection=False):
        if not _MODEL_ID.fullmatch(record["id"]):
            raise CoreDataError("model_integrity", "The managed model identifier is invalid.")
        directory = checked_path((root or self.root) / "objects" / record["id"])
        for item in record["files"]:
            path = relative_file(directory, item["name"])
            if path.stat().st_size != item["size_bytes"] or file_hash(path, cancel) != item["sha256"]:
                raise CoreDataError("model_integrity", "A managed model file was modified.")
        first = relative_file(directory, record["files"][0]["name"])
        inspection = inspect_gguf(first, cancel=cancel)
        if inspection["template_digest"] != record["template_digest"] or adapter_digest(inspection) != record["adapter_digest"]:
            raise CoreDataError("model_integrity", "The managed chat template or adapter configuration was modified.")
        return (first, inspection) if include_inspection else first

    def validate_configuration(self, model_id, params):
        record = self.get(model_id)
        values = {key: value for key, value in params.items() if key != "model_id"}
        permitted = {"device", "context_length", "gpu_layers", "threads", "is_default", "source", "license"}
        if values.keys() - permitted:
            raise CoreDataError("invalid_model_configuration", "Unknown model configuration field.")
        if "device" in values and values["device"] not in {"cpu", "cuda", "vulkan", "auto"}:
            raise CoreDataError("invalid_device", "Choose cpu, cuda, vulkan, or auto.")
        for key, low, high in (("context_length", 1, record["max_context_length"]), ("gpu_layers", -1, 10000), ("threads", 1, 1024)):
            if key in values and (type(values[key]) is not int or not low <= values[key] <= high):
                raise CoreDataError("invalid_model_configuration", f"Invalid {key}.")
        if "is_default" in values and type(values["is_default"]) is not bool:
            raise CoreDataError("invalid_model_configuration", "is_default must be a boolean.")
        for key in ("source", "license"):
            if key in values and values[key] is not None and (not isinstance(values[key], str) or len(values[key]) > 4096):
                raise CoreDataError("invalid_model_configuration", f"Invalid {key} metadata.")
        return values

    def configure(self, model_id, params):
        values = self.validate_configuration(model_id, params)
        if values:
            with self.repository._connect() as connection:
                if values.get("is_default"):
                    connection.execute("UPDATE local_models SET is_default=0 WHERE is_default=1")
                connection.execute(f"UPDATE local_models SET {','.join(key + '=?' for key in values)} WHERE id=?", (*values.values(), model_id))
        return self.get(model_id)

    def set_status(self, model_id, status, error=None):
        with self.repository._connect() as connection:
            connection.execute("UPDATE local_models SET status=?,error=? WHERE id=?", (status, canonical(error) if error else None, model_id))

    def remove(self, model_id):
        record = self.get(model_id)
        if not _MODEL_ID.fullmatch(record["id"]):
            raise CoreDataError("model_integrity", "Invalid managed model identifier.")
        directory = checked_path(self.root / "objects" / record["id"])
        # Removing files first is retryable. A crash cannot orphan untracked bytes.
        remove_owned_tree(directory)
        with self.repository._connect() as connection:
            connection.execute("DELETE FROM local_models WHERE id=?", (model_id,))

    def migrate_root(self, target, job_id, event):
        self._recover_root()
        self._recover_import()
        target = checked_path(target)
        if target == self.root:
            return
        if target.is_relative_to(self.root) or self.root.is_relative_to(target):
            raise CoreDataError("invalid_storage_root", "Storage roots cannot contain one another.")
        target.mkdir(parents=True, exist_ok=True)
        if any(target.iterdir()):
            raise CoreDataError("invalid_storage_root", "Choose an empty primary model directory.")
        models, previous = self.models(), self.root
        cancel = lambda: cancellation(event)
        if shutil.disk_usage(target).free < sum(model["size_bytes"] for model in models) + 64 * 1024 * 1024:
            raise CoreDataError("insufficient_disk", "Insufficient space to migrate the model store.")
        journal = {"target": str(target), "previous_root": str(previous), "job_id": job_id,
                   "model_ids": [model["id"] for model in models]}
        with self.repository._connect() as connection:
            connection.execute("INSERT INTO local_model_settings(key,value) VALUES ('pending_root',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (canonical(journal),))
        staging = self._stage(target, job_id)
        staged_objects = staging / "objects"
        staged_objects.mkdir()
        published = False
        try:
            for model in models:
                self.verify(model, cancel=cancel)
                directory = staged_objects / model["id"]
                directory.mkdir()
                for item in model["files"]:
                    digest = copy_file(relative_file(previous / "objects" / model["id"], item["name"]), directory / item["name"], cancel)
                    if digest != item["sha256"]:
                        raise CoreDataError("model_integrity", "A model changed during storage migration.")
            cancel()
            os.replace(staged_objects, target / "objects")
            published = True
            cancel()
            with self.repository._connect() as connection:
                connection.execute("INSERT INTO local_model_settings(key,value) VALUES ('root',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(target),))
                recovery = {"previous_root": str(previous), "root": str(target), "retained_previous_root": True}
                connection.execute("INSERT INTO local_model_settings(key,value) VALUES ('storage_migration',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (canonical(recovery),))
            self.root = target
        finally:
            if published and self.root != target:
                remove_owned_tree(target / "objects")
            remove_owned_tree(staging)
            with self.repository._connect() as connection:
                connection.execute("DELETE FROM local_model_settings WHERE key='pending_root'")
        # Pointer commit is the migration boundary. Old copies are retained for
        # crash recovery and may be removed explicitly after verifying the move.

    def jobs(self):
        with self.repository._connect() as connection:
            rows = connection.execute("SELECT * FROM local_model_jobs ORDER BY created_at DESC,id").fetchall()
        result = [dict(row) for row in rows]
        for job in result:
            job["error"] = json.loads(job["error"]) if job["error"] else None
        return result

    def update_job(self, job_id, status, *, progress=0, error=None):
        with self.repository._connect() as connection:
            connection.execute("UPDATE local_model_jobs SET status=?,progress=?,error=?,updated_at=? WHERE id=?",
                (status, progress, canonical(error) if error else None, now(), job_id))

    def storage_migration(self):
        with self.repository._connect() as connection:
            row = connection.execute("SELECT value FROM local_model_settings WHERE key='storage_migration'").fetchone()
        return json.loads(row[0]) if row else None
