"""Verify and atomically stage an offline inference runtime for desktop bundling."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import shutil
import uuid


def build_identity(repository: Path, device: str) -> dict:
    if device not in {"cpu", "cuda"}:
        raise ValueError("Unsupported runtime device")
    paths = ["services/agent-core/src/agent/rag/worker.py",
             "services/agent-core/src/agent/rag/model_profiles.py",
             f"services/agent-core/requirements-rag-{device}.lock"]
    return {relative: hashlib.sha256((Path(repository) / relative).read_bytes().replace(b"\r\n", b"\n").rstrip(b"\n") + b"\n").hexdigest()
            for relative in paths}


def verify_build_identity(manifest: dict, repository: Path) -> None:
    if manifest.get("build_identity") != build_identity(repository, manifest.get("device_bundle")):
        raise ValueError("Inference runtime source or dependency identity differs from this release. Rebuild the runtime.")


def verify(root: Path) -> dict:
    root = Path(root).resolve()
    manifest = json.loads((root / "runtime.json").read_text(encoding="utf-8-sig"))
    if manifest.get("schema_version") != 1 or manifest.get("protocol_version") != 1 \
            or manifest.get("executable") != "rag-worker.exe" or manifest.get("device_bundle") not in {"cpu", "cuda"}:
        raise ValueError("Unsupported inference runtime manifest")
    files = manifest.get("files")
    if not isinstance(files, dict) or "rag-worker.exe" not in files:
        raise ValueError("Inference executable is missing")
    found = set()
    for directory, directories, names in os.walk(root, followlinks=False):
        for name in directories + names:
            item = Path(directory) / name
            if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
                raise ValueError("Linked runtime files are not supported")
            if not item.resolve().is_relative_to(root):
                raise ValueError("Runtime path escapes its root")
        for name in names:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if relative == "runtime.json":
                continue
            found.add(relative)
            expected = files.get(relative)
            if not isinstance(expected, str):
                raise ValueError("Unlisted runtime file")
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != expected:
                raise ValueError(f"Runtime file hash mismatch: {relative}")
    if found != set(files):
        raise ValueError("Runtime files are missing")
    return manifest


def stage(source: Path, target: Path, repository: Path | None = None) -> None:
    source, target = Path(source).resolve(), Path(target).absolute()
    manifest = verify(source)
    if repository is not None:
        verify_build_identity(manifest, repository)
    if target.name != "rag-runtime" or target.is_symlink() or (hasattr(target, "is_junction") and target.is_junction()):
        raise ValueError("Expected a managed rag-runtime directory")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name("rag-runtime-staging-" + uuid.uuid4().hex)
    backup = target.with_name("rag-runtime-backup-" + uuid.uuid4().hex)
    try:
        shutil.copytree(source, temporary)
        verify(temporary)
        if target.exists():
            os.replace(target, backup)
        try:
            os.replace(temporary, target)
        except BaseException:
            if backup.exists():
                os.replace(backup, target)
            raise
    finally:
        for owned in (temporary, backup):
            if owned.exists() and owned.resolve().parent == target.parent.resolve():
                shutil.rmtree(owned)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--repository", type=Path)
    args = parser.parse_args()
    if args.destination:
        stage(args.source, args.destination, args.repository)
    else:
        manifest = verify(args.source)
        if args.repository:
            verify_build_identity(manifest, args.repository)
