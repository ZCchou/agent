"""Dedicated migration and health worker entry points for the desktop Host."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from agent.local_models.paths import checked_path
from agent.profile_migration import MigrationCoordinator
from agent.rag.models import _write_json
from agent.storage import CoreDataError, CoreRepository, SCHEMA_VERSION


APPLICATION_ID = 'io.github.zcchou.agent.airworthiness'
LEGACY_PROFILES = {'io.github.zcchou.agent.documentpreview', 'io.github.zcchou.agent.ocrpreview'}


def _request_path(value):
    # Native canonicalize returns extended Windows paths. Keep one local path
    # representation before URI creation and ownership comparisons.
    value = str(Path(value))
    if value.startswith('\\\\?\\UNC\\'):
        value = '\\\\' + value[8:]
    elif value.startswith('\\\\?\\'):
        value = value[4:]
    return checked_path(Path(value).resolve())


def emit(value):
    print(json.dumps(value, ensure_ascii=True, separators=(',', ':')), flush=True)


def health(root):
    from agent.protocol import BUILD_ID, CORE_VERSION, PROTOCOL_VERSION
    root = _request_path(root)
    database = checked_path(root / 'agent.db', file=True)
    CoreRepository(database)
    with closing(sqlite3.connect(database)) as connection:
        integrity = connection.execute('PRAGMA integrity_check').fetchone()[0]
        if integrity != 'ok' or connection.execute('PRAGMA foreign_key_check').fetchone():
            raise CoreDataError('migration_integrity_failed', '迁移后的数据库完整性检查失败。')
    return {'application_id': APPLICATION_ID, 'schema_version': SCHEMA_VERSION,
            'core_build_id': BUILD_ID, 'core_version': CORE_VERSION, 'ipc_version': PROTOCOL_VERSION,
            'integrity': 'ok'}


def run_request(path):
    path = checked_path(_request_path(path), file=True)
    if path.stat().st_size > 1024 * 1024:
        raise CoreDataError('migration_request_invalid', '迁移请求超过大小限制。')
    request = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(request, dict) or request.get('application_id') != APPLICATION_ID or request.get('action') not in {'preview', 'prepare', 'activate', 'rollback', 'finalize'}:
        raise CoreDataError('migration_request_invalid', '迁移请求不属于当前应用或动作无效。')
    target = _request_path(request['target_root'])
    workspace = _request_path(request['workspace'])
    model_root = _request_path(request['model_root'])
    if target.name != APPLICATION_ID or path.parent != workspace or not workspace.is_relative_to(target / 'profile-migrations'):
        raise CoreDataError('migration_request_invalid', '迁移控制目录与应用身份不一致。')
    sources = request.get('sources')
    if not isinstance(sources, list) or not 1 <= len(sources) <= 2:
        raise CoreDataError('migration_request_invalid', '请选择要迁入的预览版。')
    seen = set()
    normalized_sources = []
    for source in sources:
        root = _request_path(source['root'])
        if root.name not in LEGACY_PROFILES or root in seen or not isinstance(source.get('label'), str):
            raise CoreDataError('migration_request_invalid', '迁移来源须为独立的 agent 预览版资料目录。')
        seen.add(root)
        normalized_sources.append({**source, 'root': str(root)})
    sources = normalized_sources
    request.update(target_root=str(target), workspace=str(workspace), model_root=str(model_root), sources=sources)
    _write_json(path, request)
    def cancelled():
        if (workspace / 'cancel.flag').exists():
            raise InterruptedError('cancelled')
    if request['action'] == 'preview':
        from agent.profile_preflight import preview
        estimate = preview(target, model_root, workspace, sources, check_cancel=cancelled)
        state = {'status': 'preview_ready', 'phase': 'preview_ready', 'preview': estimate}
        _write_json(workspace / 'state.json', state)
        emit(state)
        return {'status': 'preview_ready'}
    if request['action'] == 'rollback':
        from agent.profile_activation import rollback_activation
        from agent.profile_model_transfer import restore_model_transfer
        if (workspace / 'model-transfer.json').is_file():
            restore_model_transfer(workspace)
        rollback_activation(workspace)
        _write_json(workspace / 'state.json', {'status': 'rolled_back', 'phase': 'rolled_back'})
        return {'status': 'rolled_back'}
    if request['action'] == 'finalize':
        from agent.profile_model_transfer import finalize_model_transfer, find_shared_model_roots, legacy_profile_busy
        activation = json.loads((workspace / 'activation.json').read_text(encoding='utf8'))
        if activation.get('status') != 'completed' or Path(activation['target_root']).resolve() != target:
            raise CoreDataError('migration_health_failed', '请先完成资料激活与健康检查。')
        health(target)
        result = json.loads((workspace / 'candidate.json').read_text(encoding='utf8'))
        profiles = [Path(profile['root']).resolve() for profile in result['profiles']]
        old_profiles = [profile for profile in profiles if profile != target]
        _write_json(workspace / 'state.json', {'status': 'running', 'phase': 'finalizing'})
        if not result['model_sources']:
            state = {'status': 'completed', 'phase': 'completed', 'transferred_files': 0}
            _write_json(workspace / 'state.json', state)
            return state
        if any(legacy_profile_busy(profile) for profile in old_profiles):
            state = {'status': 'cleanup_pending', 'phase': 'cleanup_pending',
                     'warnings': [{'code': 'legacy_profile_busy'}]}
            _write_json(workspace / 'state.json', state)
            return state
        others = [path for path in target.parent.iterdir() if path.is_dir() and path.resolve() not in [target, *profiles]]
        shared, warnings = find_shared_model_roots(others)
        if warnings:
            state = {'status': 'cleanup_pending', 'phase': 'cleanup_pending', 'warnings': warnings}
            _write_json(workspace / 'state.json', state)
            return state
        transferred = finalize_model_transfer(result['model_sources'], workspace, health_verified=True,
            shared_roots=shared, check_cancel=cancelled)
        state = {'status': 'completed', 'phase': 'completed', 'transferred_files': len(transferred['files']),
                 'warnings': [{'code': 'source_model_preserved', 'name': entry.get('name'), 'reason': entry['reason']}
                              for entry in transferred['preserved']]}
        _write_json(workspace / 'state.json', state)
        return state
    if request['action'] == 'activate':
        from agent.profile_activation import activate_candidate
        from agent.profile_fingerprint import check_source_states
        result = json.loads((workspace / 'candidate.json').read_text(encoding='utf8'))
        candidate = checked_path(Path(result['candidate_root']).resolve())
        if not candidate.is_relative_to(workspace) or candidate == workspace:
            raise CoreDataError('migration_request_invalid', '候选目录超出当前迁移任务。')
        check_source_states(result['profiles'], check_cancel=cancelled)
        from agent.profile_model_transfer import legacy_profile_busy
        if any(legacy_profile_busy(Path(profile['root'])) for profile in result['profiles']
               if Path(profile['root']).resolve() != target):
            raise CoreDataError('migration_application_busy', '请关闭来源预览版后再启用迁移结果。')
        def verify():
            from agent.protocol import BUILD_ID
            command = [sys.executable] if getattr(sys, 'frozen', False) else [sys.executable, '-m', 'agent.main']
            checked = subprocess.run([*command, '--profile-health', str(target)], capture_output=True, timeout=60,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            try:
                response = json.loads(checked.stdout)
            except ValueError:
                response = {}
            if checked.returncode or response.get('core_build_id') != BUILD_ID or response.get('integrity') != 'ok':
                raise CoreDataError('migration_health_failed', '迁移后的 Core 健康检查失败。')
        _write_json(workspace / 'state.json', {'status': 'publishing', 'phase': 'publishing'})
        activate_candidate(candidate, target, workspace, health_check=verify, check_cancel=cancelled)
        _write_json(workspace / 'state.json', {'status': 'awaiting_restart', 'phase': 'awaiting_restart'})
        return {'status': 'awaiting_restart'}
    coordinator = MigrationCoordinator(target, model_root, workspace, check_cancel=cancelled, progress=emit)
    coordinator.prepare(sources)
    return {'status': 'prepared', 'workspace': str(workspace)}


def main(arguments):
    try:
        if len(arguments) != 2:
            raise CoreDataError('migration_request_invalid', '迁移命令参数无效。')
        command, path = arguments
        if command == '--profile-health':
            emit(health(path))
        elif command == '--profile-migration':
            emit(run_request(path))
        else:
            raise CoreDataError('migration_request_invalid', '迁移命令无效。')
        return 0
    except InterruptedError:
        emit({'status': 'cancelled'})
        return 2
    except Exception as error:
        emit({'status': 'failed', 'error': {'code': getattr(error, 'code', 'migration_failed'),
              'message': error.message if isinstance(error, CoreDataError) else '迁移工作进程未能完成，请查看诊断记录。'}})
        return 1
