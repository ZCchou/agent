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


@pytest.mark.parametrize("bundle,device,accepted", [("cpu", "cpu", True), ("cpu", "cuda", False),
                                                  ("cuda", "cpu", True), ("cuda", "cuda", True)])
def test_offline_full_edition_requires_cuda_capable_runtime(tmp_path, bundle, device, accepted):
    tool = bundle_tool()
    (tmp_path / "rag-worker.exe").write_bytes(b"runtime")
    manifest = {"schema_version": 1, "protocol_version": 1, "executable": "rag-worker.exe",
                "device_bundle": bundle, "files": {"rag-worker.exe": hashlib.sha256(b"runtime").hexdigest()}}
    (tmp_path / "runtime.json").write_text(json.dumps(manifest), encoding="utf-8")
    if accepted:
        assert tool.verify(tmp_path, required_device=device)["device_bundle"] == bundle
    else:
        with pytest.raises(ValueError, match="CPU and CUDA"):
            tool.verify(tmp_path, required_device=device)


def test_windows_release_defaults_to_the_combined_cpu_cuda_runtime():
    release = (Path(__file__).resolve().parents[3] / "tools/release-windows.ps1").read_text(encoding="utf-8")
    assert '[string]$RagRuntimeDevice = "cuda"' in release
    assert '"--required-device", $RagRuntimeDevice' in release


def test_cpu_only_runtime_cannot_replace_the_installed_full_edition(tmp_path):
    tool = bundle_tool()
    source = tmp_path / "source"
    source.mkdir()
    (source / "rag-worker.exe").write_bytes(b"runtime")
    manifest = {"schema_version": 1, "protocol_version": 1, "executable": "rag-worker.exe",
                "device_bundle": "cpu", "files": {"rag-worker.exe": hashlib.sha256(b"runtime").hexdigest()}}
    (source / "runtime.json").write_text(json.dumps(manifest), encoding="utf-8")
    target = tmp_path / "app/rag-runtime"
    target.mkdir(parents=True)
    (target / "previous.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="CPU and CUDA"):
        tool.stage(source, target, required_device="cuda")
    assert (target / "previous.txt").read_text(encoding="utf-8") == "keep"
    assert not (target / "rag-worker.exe").exists()
