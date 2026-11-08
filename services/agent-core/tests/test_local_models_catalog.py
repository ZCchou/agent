import asyncio
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agent.storage import CoreDataError, CoreRepository
from test_local_models_gguf import gguf_file


def service_at(tmp_path, **kwargs):
    from agent.local_models import LocalModelService
    from agent.migrations.v25_local_models import migrate
    repository = CoreRepository(tmp_path / "data" / "core.sqlite")
    with repository._connect() as connection:
        migrate(connection)
    return LocalModelService(repository, **kwargs)


async def finish(service, response):
    job_id = response["job"]["id"]
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        job = next(item for item in (await service.handle("job.list", {}))["jobs"] if item["id"] == job_id)
        if job["status"] not in {"queued", "running"}:
            return job
        await asyncio.sleep(0.01)
    raise AssertionError("job did not finish")


def test_import_owned_copy_deduplicates_and_reopens_cold(tmp_path):
    async def exercise():
        service = service_at(tmp_path)
        source = gguf_file(tmp_path / "千问.gguf")
        queued = await service.handle("model.import", {"path": str(source)})
        assert queued["job"]["status"] == "queued"
        assert (await finish(service, queued))["status"] == "completed"
        models = (await service.handle("model.list", {}))["models"]
        assert len(models) == 1
        model = models[0]
        assert model["provider_profile_id"] == "managed:" + model["id"]
        assert model["context_length"] == 8192 and model["device"] == "cpu"
        assert model["status"] == "imported" and model["digest"] and model["template_digest"]
        assert service.provider_profiles() == {}
        with pytest.raises(CoreDataError) as error:
            service.get_profile(model["provider_profile_id"])
        assert error.value.code == "model_not_loaded"
        assert (await finish(service, await service.handle("model.import", {"path": str(source)})))["status"] == "completed"
        assert len((await service.handle("model.list", {}))["models"]) == 1
        source.unlink()
        await service.close()
        reopened = service_at(tmp_path)
        assert (await reopened.handle("model.list", {}))["models"][0]["digest"] == model["digest"]
        await reopened.close()
    asyncio.run(exercise())


def test_configuration_default_and_root_migration_are_durable(tmp_path):
    async def exercise():
        service = service_at(tmp_path)
        await finish(service, await service.handle("model.import", {"path": str(gguf_file(tmp_path / "model.gguf"))}))
        model = (await service.handle("model.list", {}))["models"][0]
        configured = await service.handle("model.configure", {"model_id": model["id"], "context_length": 4096,
            "device": "auto", "gpu_layers": 12, "threads": 2, "source": "user confirmed", "license": "Apache-2.0", "is_default": True})
        assert configured["model"]["is_default"]
        destination = tmp_path / "新目录"
        assert (await finish(service, await service.handle("storage.root", {"path": str(destination)})))["status"] == "completed"
        listing = await service.handle("model.list", {})
        assert listing["root"] == str(destination)
        assert listing["models"][0]["context_length"] == 4096
        assert listing["models"][0]["license"] == "Apache-2.0"
        await service.close()
        reopened = service_at(tmp_path)
        assert (await reopened.handle("model.list", {}))["root"] == str(destination)
        await reopened.handle("model.remove", {"model_id": model["id"]})
        assert (await reopened.handle("model.list", {}))["models"] == []
        await reopened.close()
    asyncio.run(exercise())


def test_cancel_before_publication_leaves_no_catalog_entry(tmp_path, monkeypatch):
    import agent.local_models.catalog as catalog
    original = catalog.copy_file
    copying, release = threading.Event(), threading.Event()

    def paused(*args, **kwargs):
        copying.set()
        release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(catalog, "copy_file", paused)

    async def exercise():
        service = service_at(tmp_path)
        queued = await service.handle("model.import", {"path": str(gguf_file(tmp_path / "model.gguf"))})
        await asyncio.to_thread(copying.wait, 5)
        cancelled = await service.handle("job.cancel", {"job_id": queued["job"]["id"]})
        release.set()
        job = await finish(service, queued)
        assert job["status"] == "cancelled"
        assert (await service.handle("model.list", {}))["models"] == []
        assert not list(service.root.glob(".staging/*"))
        await service.close()
    asyncio.run(exercise())


def test_restart_interrupts_jobs_and_cleans_only_owned_staging(tmp_path):
    service = service_at(tmp_path)
    staging = service.root / ".staging"
    staging.mkdir(exist_ok=True)
    owned = staging / ("job-" + "a" * 32)
    owned.mkdir()
    (owned / ".managed-staging").write_text("1")
    unrelated = staging / "keep"
    unrelated.mkdir()
    with service.repository._connect() as connection:
        connection.execute("INSERT INTO local_model_jobs(id,kind,status,progress,created_at,updated_at) VALUES ('pending','model.import','running',0,'x','x')")
    asyncio.run(service.close())
    reopened = service_at(tmp_path)
    jobs = asyncio.run(reopened.handle("job.list", {}))["jobs"]
    assert jobs[0]["status"] == "interrupted"
    assert unrelated.exists() and not owned.exists()
    asyncio.run(reopened.close())


def test_immediate_cancel_releases_mutation_guard(tmp_path):
    async def exercise():
        service = service_at(tmp_path)
        source = gguf_file(tmp_path / "model.gguf")
        queued = await service.handle("model.import", {"path": str(source)})
        await service.handle("job.cancel", {"job_id": queued["job"]["id"]})
        assert (await finish(service, queued))["status"] == "cancelled"
        assert (await finish(service, await service.handle("model.import", {"path": str(source)})))["status"] == "completed"
        await service.close()
    asyncio.run(exercise())


def test_cancellation_at_import_publication_removes_uncommitted_object(tmp_path, monkeypatch):
    import agent.local_models.catalog as catalog
    original = catalog.os.replace
    published, release = threading.Event(), threading.Event()

    def paused(source, destination):
        original(source, destination)
        if destination.parent.name == "objects":
            published.set()
            release.wait(5)

    monkeypatch.setattr(catalog.os, "replace", paused)

    async def exercise():
        service = service_at(tmp_path)
        queued = await service.handle("model.import", {"path": str(gguf_file(tmp_path / "model.gguf"))})
        await asyncio.to_thread(published.wait, 5)
        await service.handle("job.cancel", {"job_id": queued["job"]["id"]})
        release.set()
        assert (await finish(service, queued))["status"] == "cancelled"
        assert (await service.handle("model.list", {}))["models"] == []
        assert not list((service.root / "objects").iterdir())
        await service.close()
    asyncio.run(exercise())


@pytest.mark.parametrize("close_after_cancel", [False, True])
def test_repeated_cancel_keeps_publication_guard_until_worker_exits(tmp_path, monkeypatch, close_after_cancel):
    import agent.local_models.catalog as catalog
    original = catalog.os.replace
    published, release = threading.Event(), threading.Event()

    def paused(source, destination):
        original(source, destination)
        if destination.parent.name == "objects" and not published.is_set():
            published.set()
            release.wait(10)

    monkeypatch.setattr(catalog.os, "replace", paused)

    async def exercise():
        service = service_at(tmp_path)
        source = gguf_file(tmp_path / "model.gguf")
        queued = await service.handle("model.import", {"path": str(source)})
        closing = None
        try:
            assert await asyncio.to_thread(published.wait, 5)
            await service.handle("job.cancel", {"job_id": queued["job"]["id"]})
            await asyncio.sleep(0)
            if close_after_cancel:
                closing = asyncio.create_task(service.close())
            else:
                await service.handle("job.cancel", {"job_id": queued["job"]["id"]})
            await asyncio.sleep(0.05)
            assert service._operation == "model.import"
            assert queued["job"]["id"] in service._jobs
            if closing:
                assert not closing.done()
            else:
                with pytest.raises(CoreDataError) as error:
                    await service.handle("model.import", {"path": str(source)})
                assert error.value.code == "model_busy"
        finally:
            release.set()
            if closing:
                await closing
            else:
                await finish(service, queued)
        job = next(job for job in service.catalog.jobs() if job["id"] == queued["job"]["id"])
        assert job["status"] == "cancelled"
        if close_after_cancel:
            service = service_at(tmp_path)
        assert (await finish(service, await service.handle("model.import", {"path": str(source)})))["status"] == "completed"
        model = service.catalog.models()[0]
        assert service.catalog.verify(model).is_file()
        await service.close()
    asyncio.run(exercise())


def test_restart_removes_owned_ephemeral_credentials(tmp_path):
    service = service_at(tmp_path)
    directory = service.root / ".credentials"
    directory.mkdir()
    stale = directory / ("session-" + "a" * 32 + ".key")
    stale.write_text("stale session secret")
    unrelated = directory / "keep.txt"
    unrelated.write_text("keep")
    asyncio.run(service.close())
    reopened = service_at(tmp_path)
    assert not stale.exists() and unrelated.exists()
    asyncio.run(reopened.close())


def test_source_replacement_before_copy_persists_actual_staged_metadata(tmp_path, monkeypatch):
    import agent.local_models.catalog as catalog
    from agent.local_models.gguf import inspect_gguf
    original = catalog.copy_file

    def replacement(source, destination, cancel):
        gguf_file(source, architecture="qwen2", template="replacement template", metadata_extra={
            "general.name": "Replacement", "general.file_type": 14, "qwen2.context_length": 4096})
        return original(source, destination, cancel)

    monkeypatch.setattr(catalog, "copy_file", replacement)

    async def exercise():
        service = service_at(tmp_path)
        source = gguf_file(tmp_path / "model.gguf")
        job = await finish(service, await service.handle("model.import", {"path": str(source)}))
        assert job["status"] == "completed", job
        model = service.catalog.models()[0]
        actual = inspect_gguf(service.catalog.verify(model))
        assert model["architecture"] == actual["architecture"] == "qwen2"
        assert model["name"] == actual["name"] == "Replacement"
        assert model["quantization"] == actual["quantization"] == "Q4_K_S"
        assert model["max_context_length"] == model["context_length"] == actual["context_length"] == 4096
        assert model["template_digest"] == actual["template_digest"]
        await service.close()
    asyncio.run(exercise())


def test_restart_cleans_journaled_root_migration_staging(tmp_path):
    service = service_at(tmp_path)
    target = tmp_path / "new-root"
    staging = target / ".staging" / ("job-" + "a" * 32)
    staging.mkdir(parents=True)
    (staging / ".managed-staging").write_text("1")
    (staging / "partial.gguf").write_bytes(b"partial")
    unrelated = target / "keep.txt"
    unrelated.write_text("keep")
    with service.repository._connect() as connection:
        connection.execute("INSERT INTO local_model_settings(key,value) VALUES ('pending_root',?)", (json.dumps({
            "target": str(target), "previous_root": str(service.root), "job_id": "a" * 32, "model_ids": []}),))
    asyncio.run(service.close())
    reopened = service_at(tmp_path)
    assert not staging.exists() and unrelated.exists()
    assert reopened.root != target
    asyncio.run(reopened.close())


@pytest.mark.parametrize("crash_point", ["rename", "marker"])
def test_restart_reconciles_import_publication_after_abrupt_exit(tmp_path, crash_point):
    script = r'''
import os, sys, threading
from pathlib import Path
sys.path[:0] = sys.argv[3:]
from test_local_models_catalog import service_at
from test_local_models_gguf import gguf_file
import agent.local_models.catalog as catalog
service = service_at(Path(sys.argv[1]))
source = gguf_file(Path(sys.argv[1]) / 'model.gguf')
unrelated = service.root / 'objects' / 'keep'
unrelated.mkdir(parents=True)
(unrelated / 'keep.txt').write_text('preserve unrelated object')
replace, unlink = catalog.os.replace, Path.unlink
def crash_replace(source, destination):
    replace(source, destination)
    if sys.argv[2] == 'rename' and destination.parent.name == 'objects':
        os._exit(77)
def crash_unlink(path, *args, **kwargs):
    unlink(path, *args, **kwargs)
    if sys.argv[2] == 'marker' and path.name == '.managed-staging' and path.parent.parent.name == 'objects':
        os._exit(77)
catalog.os.replace, Path.unlink = crash_replace, crash_unlink
service.catalog.import_model(source, 'a' * 32, threading.Event())
raise SystemExit('fault point not reached')
'''
    tests = Path(__file__).resolve().parent
    src = tests.parent / "src"
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path), crash_point, str(src), str(tests)],
                            capture_output=True, timeout=30)
    assert result.returncode == 77, result.stderr.decode(errors="replace")
    reopened = service_at(tmp_path)
    assert reopened.catalog.models() == []
    objects = reopened.root / "objects"
    assert {path.name for path in objects.iterdir()} == {"keep"}
    assert (objects / "keep" / "keep.txt").read_text() == "preserve unrelated object"
    assert not list(reopened.root.glob(".staging/job-*"))
    with reopened.repository._connect() as connection:
        assert connection.execute("SELECT value FROM local_model_settings WHERE key='pending_import'").fetchone() is None
    asyncio.run(reopened.close())


def test_root_rollback_retains_marker_until_published_objects_are_removed(tmp_path, monkeypatch):
    import agent.local_models.catalog as catalog

    async def exercise():
        service = service_at(tmp_path)
        await finish(service, await service.handle("model.import", {"path": str(gguf_file(tmp_path / "model.gguf"))}))
        model = service.catalog.models()[0]
        target, event = tmp_path / "new-root", threading.Event()
        original_replace, original_remove = catalog.os.replace, catalog.remove_owned_tree

        def cancel_after_publish(source, destination):
            original_replace(source, destination)
            if destination == target / "objects":
                event.set()

        def interrupt_rollback(path):
            if path == target / "objects":
                raise OSError("simulated rollback interruption")
            return original_remove(path)

        with monkeypatch.context() as patch:
            patch.setattr(catalog.os, "replace", cancel_after_publish)
            patch.setattr(catalog, "remove_owned_tree", interrupt_rollback)
            with pytest.raises(OSError, match="simulated rollback interruption"):
                await asyncio.to_thread(service.catalog.migrate_root, target, "b" * 32, event)
        await service.close()
        reopened = service_at(tmp_path)
        assert reopened.catalog.verify(model).is_file()
        assert not (target / "objects" / model["id"]).exists()
        assert not list(target.glob(".staging/job-*"))
        await reopened.close()
    asyncio.run(exercise())


def test_retry_recovers_failed_import_rollback_before_replacing_journal(tmp_path, monkeypatch):
    import agent.local_models.catalog as catalog
    service = service_at(tmp_path)
    source = gguf_file(tmp_path / "first.gguf")
    other = gguf_file(tmp_path / "second.gguf", template="second model")
    event = threading.Event()
    original_replace = catalog.os.replace

    def cancel_after_publish(source, destination):
        original_replace(source, destination)
        if destination.parent.name == "objects":
            event.set()

    def interrupted(record, **kwargs):
        raise OSError("interrupted import rollback")

    with monkeypatch.context() as patch:
        patch.setattr(catalog.os, "replace", cancel_after_publish)
        patch.setattr(service.catalog, "_remove_published_object", interrupted)
        with pytest.raises(OSError, match="interrupted import rollback"):
            service.catalog.import_model(source, "c" * 32, event)
    service.catalog.import_model(other, "d" * 32, threading.Event())
    assert {path.name for path in (service.root / "objects").iterdir()} == {model["id"] for model in service.catalog.models()}
    asyncio.run(service.close())


@pytest.mark.parametrize("params", [{"device": "remote"}, {"context_length": True}, {"context_length": 99999999},
    {"gpu_layers": -2}, {"threads": 0}, {"source": []}, {"is_default": "yes"}])
def test_invalid_configuration_is_atomic(tmp_path, params):
    async def exercise():
        service = service_at(tmp_path)
        await finish(service, await service.handle("model.import", {"path": str(gguf_file(tmp_path / "model.gguf"))}))
        before = (await service.handle("model.list", {}))["models"][0]
        with pytest.raises(CoreDataError):
            await service.handle("model.configure", {"model_id": before["id"], **params})
        assert (await service.handle("model.list", {}))["models"][0] == before
        await service.close()
    asyncio.run(exercise())
