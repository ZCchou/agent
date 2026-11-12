"""Complete verified model transfers without removing shared or changed files."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3

from agent.local_models.paths import checked_path, file_hash
from agent.profile_files import copy_managed_file
from agent.profile_migration import MigrationCoordinator
from agent.rag.models import _write_json
from agent.storage import CoreDataError


def legacy_profile_busy(root):
    """Check the source database without changing its SQLite journals."""
    if os.name != 'nt':
        return False
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                   wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(Path(root) / 'agent.db'), 0x80000000, 0, None, 3, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        return True
    kernel.CloseHandle(handle)
    return False


def find_shared_model_roots(other_profiles):
    roots, warnings = set(), []
    for profile in other_profiles:
        profile = checked_path(Path(profile).resolve())
        for name in ('rag/rag-models.json', 'rag/ocr-storage.json'):
            path = profile / name
            if not path.is_file():
                continue
            try:
                checked_path(path, file=True)
                root = json.loads(path.read_text(encoding='utf-8-sig'))['root']
                roots.add(checked_path(Path(root).resolve()))
            except (OSError, ValueError, KeyError, CoreDataError):
                warnings.append({'code': 'shared_storage_check_failed', 'profile': str(profile), 'file': name})
        database = profile / 'agent.db'
        if database.is_file():
            try:
                checked_path(database, file=True)
                with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as connection:
                    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='local_model_settings'").fetchone()
                    record = connection.execute("SELECT value FROM local_model_settings WHERE key='root'").fetchone() if exists else None
                roots.add(checked_path(Path(record[0]).resolve()) if record else profile / 'local-models')
            except (OSError, sqlite3.Error, CoreDataError):
                warnings.append({'code': 'shared_storage_check_failed', 'profile': str(profile), 'file': 'agent.db'})
        roots.add(profile / 'rag/ocr-models')
    return roots, warnings


def finalize_model_transfer(model_sources, workspace, *, health_verified, shared_roots, check_cancel=lambda: None):
    if not health_verified:
        raise ValueError('Target health must be verified before source cleanup.')
    workspace = checked_path(Path(workspace).resolve())
    workspace.mkdir(parents=True, exist_ok=True)
    journal = workspace / 'model-transfer.json'
    state = {'application_id': 'io.github.zcchou.agent.airworthiness', 'status': 'finalizing', 'files': [], 'preserved': []}
    if journal.exists():
        state = json.loads(journal.read_text(encoding='utf8'))
        if state.get('application_id') != 'io.github.zcchou.agent.airworthiness' or state.get('status') not in {'finalizing', 'completed'}:
            raise CoreDataError('migration_recovery_required', '模型转移已有恢复记录，请重新准备迁移。')
        if state['status'] == 'completed':
            return state
    shared_roots = [checked_path(Path(root).resolve()) for root in shared_roots]
    _write_json(journal, state)
    for record in model_sources:
        check_cancel()
        source = checked_path(Path(record['source_root']).resolve())
        target = checked_path(Path(record['target_root']).resolve())
        profile = checked_path(Path(record['profile_root']).resolve())
        reason = None
        if source == target or source.is_relative_to(target) or target.is_relative_to(source):
            reason = 'same_storage'
        elif not source.is_relative_to(profile):
            reason = 'external'
        elif any(source.is_relative_to(root) or root.is_relative_to(source) for root in shared_roots):
            reason = 'shared'
        if reason:
            state['preserved'].append({'source_root': str(source), 'reason': reason})
            _write_json(journal, state)
            continue
        files = dict(record['files'])
        for name, backup in record.get('metadata_backups', {}).items():
            backup = checked_path(Path(backup), file=True)
            files[name] = file_hash(backup, check_cancel)
        for name, digest in files.items():
            check_cancel()
            original = MigrationCoordinator._relative(source, name)
            mapped = record.get('target_files', {}).get(name, name)
            duplicate = MigrationCoordinator._relative(target, mapped)
            backup = record.get('metadata_backups', {}).get(name)
            if backup:
                duplicate = checked_path(Path(backup), file=True)
            previous = next((entry for entry in state['files'] if entry['source'] == str(original)), None)
            if not original.exists() and previous and previous['sha256'] == digest and previous['copy'] == str(duplicate):
                if not duplicate.is_file() or file_hash(duplicate, check_cancel) != digest:
                    raise CoreDataError('migration_file_changed', '已转移的模型副本校验失败，请恢复模型后继续。')
                continue
            if not original.is_file() or file_hash(original, check_cancel) != digest:
                state['preserved'].append({'source_root': str(source), 'name': name, 'reason': 'changed'})
                _write_json(journal, state)
                continue
            if not duplicate.is_file() or file_hash(duplicate, check_cancel) != digest:
                raise CoreDataError('migration_file_changed', '目标模型或元数据校验失败，源文件已保留。')
            entry = {'source': str(original), 'copy': str(duplicate), 'sha256': digest, 'bytes': original.stat().st_size}
            if previous is None:
                state['files'].append(entry)
            _write_json(journal, state)
            # Recheck after persisting the recovery record, immediately before removal.
            checked_path(original, file=True)
            if file_hash(original, check_cancel) != digest:
                raise CoreDataError('migration_file_changed', '清理前源模型发生更改，已停止转移。')
            original.unlink()
    state['status'] = 'completed'
    _write_json(journal, state)
    return state


def restore_model_transfer(workspace):
    workspace = checked_path(Path(workspace).resolve())
    journal = workspace / 'model-transfer.json'
    state = json.loads(journal.read_text(encoding='utf8'))
    if state.get('application_id') != 'io.github.zcchou.agent.airworthiness':
        raise CoreDataError('migration_recovery_invalid', '模型恢复记录不属于当前应用。')
    for record in state['files']:
        copy_managed_file(Path(record['copy']), Path(record['source']), record['sha256'], record['bytes'])
    state['status'] = 'restored'
    _write_json(journal, state)
