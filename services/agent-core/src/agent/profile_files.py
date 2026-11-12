"""Verified, resumable file copies for profile migration."""
from __future__ import annotations

import os
import uuid

from agent.local_models.catalog import copy_file
from agent.local_models.paths import checked_path, file_hash
from agent.storage import CoreDataError


def copy_managed_file(source, destination, expected_hash, expected_size, *, check_cancel=lambda: None, progress=None):
    destination = checked_path(destination)
    check_cancel()
    if destination.exists():
        if destination.is_file() and destination.stat().st_size == expected_size and file_hash(destination, check_cancel) == expected_hash:
            if progress:
                progress(expected_size, expected_size)
            return False
        raise CoreDataError('migration_file_conflict', '迁移目标已有不同内容，原文件已保留。')
    source = checked_path(source, file=True)
    if source.stat().st_size != expected_size:
        raise CoreDataError('migration_file_changed', '源文件大小与登记信息不一致。')
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name('.migration-' + uuid.uuid4().hex + '.tmp')
    def tick():
        check_cancel()
        if progress and temporary.exists():
            progress(min(temporary.stat().st_size, expected_size), expected_size)
    try:
        digest = copy_file(source, temporary, tick)
        if digest != expected_hash or temporary.stat().st_size != expected_size:
            raise CoreDataError('migration_file_changed', '迁移文件未通过哈希校验。')
        check_cancel()
        checked_path(destination.parent)
        if os.name == 'nt':
            # Windows rename refuses an existing destination atomically.
            os.rename(temporary, destination)
        else:
            os.link(temporary, destination)
            temporary.unlink()
        return True
    finally:
        temporary.unlink(missing_ok=True)
