"""Journaled per-file profile activation while the application Core is stopped."""
import json
import hashlib
import os
from pathlib import Path
import uuid

from agent.local_models.paths import checked_path, file_hash
from agent.profile_files import copy_managed_file
from agent.rag.models import _write_json
from agent.storage import CoreDataError


_CONFIGURATION = {'agent.db', 'settings.json', 'skills.json', 'work-tool-grants.json',
                  'rag/rag-models.json', 'rag/ocr-storage.json', 'file-recovery/undo-catalog-v1.bin'}
_BLOB_ROOTS = {'message-attachments', 'document-artifacts', 'skills', 'file-recovery'}
_APPLICATION = 'io.github.zcchou.agent.airworthiness'


def _json_hash(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError, UnicodeError):
        return None
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf8')).hexdigest()


def _managed_key(key):
    path = Path(key)
    return not path.is_absolute() and '..' not in path.parts and (
        key in _CONFIGURATION or path.parts[0] in _BLOB_ROOTS or key.startswith('rag/originals/'))


def _replace(source, destination, digest, size):
    temporary = destination.with_name('.activation-' + uuid.uuid4().hex + '.tmp')
    try:
        copy_managed_file(source, temporary, digest, size)
        checked_path(destination)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def rollback_activation(workspace):
    workspace = checked_path(Path(workspace).resolve())
    journal = workspace / 'activation.json'
    state = json.loads(journal.read_text(encoding='utf8'))
    if state.get('application_id') != _APPLICATION:
        raise CoreDataError('migration_recovery_invalid', '迁移恢复记录不属于当前应用。')
    target = checked_path(Path(state['target_root']))
    # Check the entire recovery set before changing any published file.
    for entry in state['files']:
        key = entry['key']
        if not _managed_key(key):
            raise CoreDataError('migration_recovery_invalid', '迁移恢复路径无效。')
        destination = checked_path(target / key)
        if destination.exists():
            current = file_hash(destination)
            equivalent_json = entry.get('new_semantic_hash') and _json_hash(destination) == entry['new_semantic_hash']
            if current not in {entry.get('old_hash'), entry['new_hash']} and not equivalent_json:
                raise CoreDataError('migration_recovery_conflict', '迁移后的文件已有更改，已保留现场，请从备份恢复。')
        if entry.get('old_hash'):
            backup = checked_path(workspace / 'activation-backup' / key, file=True)
            if file_hash(backup) != entry['old_hash'] or backup.stat().st_size != entry['old_size']:
                raise CoreDataError('migration_recovery_invalid', '迁移备份未通过完整性校验。')
    for entry in reversed(state['files']):
        key = entry['key']
        if not _managed_key(key):
            raise CoreDataError('migration_recovery_invalid', '迁移恢复路径无效。')
        destination = checked_path(target / key)
        if destination.exists():
            current = file_hash(destination)
            if current == entry.get('old_hash'):
                continue
        if entry.get('old_hash'):
            backup = workspace / 'activation-backup' / key
            _replace(backup, destination, entry['old_hash'], entry['old_size'])
        else:
            destination.unlink(missing_ok=True)
    state['status'] = 'rolled_back'
    _write_json(journal, state)


def activate_candidate(candidate, target, workspace, *, health_check, check_cancel=lambda: None):
    candidate = checked_path(Path(candidate).resolve())
    target = checked_path(Path(target).resolve())
    workspace = checked_path(Path(workspace).resolve())
    if candidate == target or target.is_relative_to(candidate):
        raise CoreDataError('migration_path_invalid', '迁移候选与应用目录必须独立。')
    workspace.mkdir(parents=True, exist_ok=True)
    journal = workspace / 'activation.json'
    if journal.exists():
        raise CoreDataError('migration_recovery_required', '已有激活记录，请先完成或回退该次迁移。')
    check_cancel()
    if any((target / ('agent.db' + suffix)).exists() for suffix in ('-wal', '-shm', '-journal')):
        raise CoreDataError('migration_application_busy', '请先关闭目标应用，等待数据库写入结束后再迁移。')
    state = {'application_id': _APPLICATION, 'target_root': str(target), 'status': 'preparing', 'files': []}
    for source in sorted(candidate.rglob('*')):
        check_cancel()
        checked_path(source)
        if not source.is_file():
            continue
        key = source.relative_to(candidate).as_posix()
        if not _managed_key(key):
            continue
        destination = checked_path(target / key)
        digest, size = file_hash(source, check_cancel), source.stat().st_size
        old_hash = file_hash(destination, check_cancel) if destination.is_file() else None
        if destination.exists() and not destination.is_file():
            raise CoreDataError('migration_file_conflict', '迁移目标已有不同内容，原文件已保留。')
        if old_hash == digest:
            continue
        if old_hash and key not in _CONFIGURATION:
            raise CoreDataError('migration_file_conflict', '迁移目标已有不同内容，原文件已保留。')
        entry = {'key': key, 'new_hash': digest, 'new_size': size, 'old_hash': old_hash,
                 'old_size': destination.stat().st_size if old_hash else None,
                 'new_semantic_hash': _json_hash(source) if key in _CONFIGURATION and key.endswith('.json') else None}
        if old_hash:
            copy_managed_file(destination, workspace / 'activation-backup' / key, old_hash, entry['old_size'], check_cancel=check_cancel)
        state['files'].append(entry)
    check_cancel()
    state['status'] = 'publishing'
    _write_json(journal, state)
    try:
        # Cancellation is deferred across activation and the health handshake.
        # The complete prewritten journal also covers interruption between files.
        for entry in state['files']:
            destination = target / entry['key']
            if entry['old_hash']:
                if not destination.is_file() or file_hash(destination) != entry['old_hash']:
                    raise CoreDataError('migration_application_busy', '目标应用配置在迁移期间发生更改。')
            elif destination.exists():
                raise CoreDataError('migration_application_busy', '目标应用在迁移期间创建了同名文件。')
            _replace(candidate / entry['key'], destination, entry['new_hash'], entry['new_size'])
        state['status'] = 'verifying'
        _write_json(journal, state)
        health_check()
        state['status'] = 'completed'
        _write_json(journal, state)
        return state
    except BaseException:
        rollback_activation(workspace)
        raise
