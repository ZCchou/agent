"""Integrity-checked, application-selected llama.cpp executable inventory."""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from agent.storage import CoreDataError
from .paths import checked_path, file_hash, relative_file


@dataclass(frozen=True)
class Engine:
    backend: str
    executable: Path
    version: str
    source_commit: str
    build: object


class RuntimeManifest:
    def __init__(self, root=None):
        fixed = Path(sys.executable).parent / "llm-runtime"
        if getattr(sys, "frozen", False) and root is not None and Path(root).absolute() != fixed.absolute():
            raise CoreDataError("runtime_integrity", "Packaged runtimes must use the fixed application resources.")
        self.root = checked_path(root if root is not None else fixed)

    def _verify(self, *, hash_files=True):
        try:
            manifest_path = checked_path(self.root / "runtime.json", file=True)
            if manifest_path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError("Runtime manifest exceeds its size limit.")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("schema_version") != 1 or manifest.get("protocol_version") != 1:
                raise ValueError("Unsupported managed runtime manifest version.")
            engines = manifest.get("engines")
            if not isinstance(engines, dict) or not engines or engines.keys() - {"cpu", "cuda", "vulkan"}:
                raise ValueError("Invalid managed runtime engine inventory.")
            all_files, commits, result = {}, set(), {}
            shared = manifest.get("files", {})
            if not isinstance(shared, dict):
                raise ValueError("Invalid runtime shared file inventory.")
            all_files.update(shared)
            for backend, record in engines.items():
                files = record.get("files")
                if not isinstance(files, dict) or not files or record.get("executable") not in files:
                    raise ValueError("The engine executable must be listed in its integrity inventory.")
                if not isinstance(record.get("version"), str) or not record["version"] or not record.get("build"):
                    raise ValueError("Missing runtime version or build provenance.")
                commit = record.get("source_commit")
                if not isinstance(commit, str) or not re.fullmatch(r"[a-f0-9]{40}", commit):
                    raise ValueError("Invalid runtime source commit.")
                commits.add(commit)
                for name, digest in files.items():
                    if name in all_files and all_files[name] != digest:
                        raise ValueError("Conflicting runtime file digests.")
                    all_files[name] = digest
                executable = relative_file(self.root, record["executable"])
                if os.name == "nt" and executable.suffix.lower() != ".exe":
                    raise ValueError("The managed runtime must be a native executable.")
                result[backend] = Engine(backend, executable, record["version"], commit, record["build"])
            if len(commits) != 1 or len(all_files) > 10000:
                raise ValueError("Runtime engines must share the same source commit and bounded inventory.")
            licenses = manifest.get("licenses")
            if not isinstance(licenses, list) or not licenses or any(name not in all_files for name in licenses):
                raise ValueError("Runtime license files must be included and integrity checked.")
            for name, digest in all_files.items():
                if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
                    raise ValueError("Invalid runtime file digest.")
                path = relative_file(self.root, name)
                if hash_files and file_hash(path) != digest:
                    raise ValueError("A managed runtime file was modified.")
            observed = set()
            for directory, dirs, files in os.walk(self.root, followlinks=False):
                for name in dirs + files:
                    checked_path(Path(directory) / name)
                for name in files:
                    observed.add((Path(directory) / name).relative_to(self.root).as_posix())
                    if len(observed) > 10001:
                        raise ValueError("Oversized runtime resource inventory.")
            if observed != {*all_files, "runtime.json"}:
                raise ValueError("Unlisted or missing files in the managed runtime directory.")
            return result
        except CoreDataError as exc:
            code = "runtime_missing" if not self.root.exists() else "runtime_integrity"
            raise CoreDataError(code, "Managed llama.cpp resources are missing, linked, or invalid.") from exc
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            raise CoreDataError("runtime_integrity", str(exc)) from exc

    def engines(self):
        return self._verify()

    def inventory(self):
        return self._verify(hash_files=False)

    def engine(self, backend):
        engines = self._verify()
        if backend not in engines:
            raise CoreDataError("backend_unavailable", "The selected llama.cpp backend is not installed.")
        return engines[backend]
