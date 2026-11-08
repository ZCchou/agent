"""Constrained paths used by the managed model store and runtime resources."""
from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from agent.storage import CoreDataError


def checked_path(path: Path | str, *, file: bool = False) -> Path:
    candidate = Path(os.path.abspath(path))
    for part in (candidate, *candidate.parents):
        if not part.exists() and not part.is_symlink():
            continue
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400 \
                or stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            raise CoreDataError("unsafe_path", "Linked or reparse-point paths are forbidden.")
    if file and (not candidate.is_file() or not stat.S_ISREG(candidate.stat().st_mode)):
        raise CoreDataError("invalid_path", "Select a regular model file.")
    return candidate


def relative_file(root: Path, name: str) -> Path:
    if not isinstance(name, str) or not name or "\\" in name or ":" in name:
        raise CoreDataError("runtime_integrity", "Invalid relative resource path.")
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts or "." == name:
        raise CoreDataError("runtime_integrity", "Invalid relative resource path.")
    target = checked_path(root / relative, file=True)
    if not target.is_relative_to(checked_path(root)):
        raise CoreDataError("runtime_integrity", "Resource escaped its managed directory.")
    return target


def file_hash(path: Path, cancel=None) -> str:
    digest = hashlib.sha256()
    with checked_path(path, file=True).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            if cancel is not None:
                cancel()
            digest.update(block)
    return digest.hexdigest()
