import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


def bundle_tool():
    path = Path(__file__).resolve().parents[3] / "tools" / "rag_runtime_bundle.py"
    spec = importlib.util.spec_from_file_location("rag_runtime_bundle", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_staging_verifies_runtime_before_atomic_replacement(tmp_path):
    tool = bundle_tool()
    source = tmp_path / "source"
    source.mkdir()
    (source / "rag-worker.exe").write_bytes(b"runtime")
    manifest = {"schema_version": 1, "protocol_version": 1, "executable": "rag-worker.exe",
        "device_bundle": "cpu", "python": "3.12", "files": {"rag-worker.exe": hashlib.sha256(b"runtime").hexdigest()}}
    (source / "runtime.json").write_text(json.dumps(manifest), encoding="utf-8")
    target = tmp_path / "app" / "rag-runtime"
    tool.stage(source, target)
    assert (target / "rag-worker.exe").read_bytes() == b"runtime"
    (source / "rag-worker.exe").write_bytes(b"tampered")
    with pytest.raises(ValueError):
        tool.stage(source, target)
    assert (target / "rag-worker.exe").read_bytes() == b"runtime"


def test_runtime_release_identity_rejects_stale_worker_and_dependency_lock(tmp_path):
    tool = bundle_tool()
    repository = tmp_path / "repository"
    source = repository / "services/agent-core/src/agent/rag"
    source.mkdir(parents=True)
    for name in ("worker.py", "model_profiles.py"):
        (source / name).write_text("original\n", encoding="utf-8")
    lock = repository / "services/agent-core/requirements-rag-cpu.lock"
    lock.write_text("package==1\n", encoding="utf-8")
    manifest = {"device_bundle": "cpu", "build_identity": tool.build_identity(repository, "cpu")}
    tool.verify_build_identity(manifest, repository)
    for path in (source / "worker.py", lock):
        original = path.read_bytes()
        path.write_bytes(b"changed\n")
        with pytest.raises(ValueError, match="source or dependency"):
            tool.verify_build_identity(manifest, repository)
        path.write_bytes(original)
