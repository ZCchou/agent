"""Prepare and verify isolated candidates for unified application data."""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sqlite3
import uuid
import time

from agent.local_models.paths import checked_path, file_hash
from agent.local_models.catalog import Catalog, content_digest
from agent.ocr.store import OcrModelStore
from agent.profile_files import copy_managed_file
from agent.profile_merge import merge_snapshots, rewrite_references, rewrite_storage_key
from agent.profile_preferences import ProfilePreferences, rebase_database_paths
from agent.profile_fingerprint import profile_state
from agent.rag.models import ModelStore, _write_json
from agent.storage import CoreDataError, CoreRepository, _utc_now


class MigrationCoordinator:
    def __init__(self, target_root, model_root, workspace, *, check_cancel=lambda: None, progress=None):
        self.target_root = checked_path(Path(target_root).resolve())
        self.model_root = checked_path(Path(model_root).resolve())
        self.workspace = checked_path(Path(workspace).resolve())
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.check_cancel = check_cancel
        self.progress = progress
        self._last_progress = 0.0
        self.state = {'application_id': 'io.github.zcchou.agent.airworthiness',
                      'target_root': str(self.target_root), 'model_root': str(self.model_root),
                      'status': 'running', 'phase': 'preparing', 'copied_files': 0, 'warnings': []}

    def _update(self, phase, **values):
        if phase != 'copying_files':
            for field in ('current_file', 'completed_bytes', 'total_bytes'):
                self.state.pop(field, None)
        if phase in {'prepared', 'completed'}:
            self.state.pop('current_model', None)
            self.state.pop('current_source', None)
        self.state.update(phase=phase, **values)
        _write_json(self.workspace / 'state.json', self.state)
        if self.progress:
            self.progress(dict(self.state))

    @staticmethod
    def _relative(root, key):
        if not isinstance(key, str) or not key or '\\' in key or ':' in key:
            raise CoreDataError('migration_path_invalid', '受管文件路径无效。')
        relative = PurePosixPath(key)
        if relative.is_absolute() or PureWindowsPath(key).is_absolute() or '..' in relative.parts:
            raise CoreDataError('migration_path_invalid', '受管文件路径超出资料目录。')
        return checked_path(root.joinpath(*relative.parts))

    def _snapshot(self, source, destination):
        self.check_cancel()
        checked_path(source, file=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as old, closing(sqlite3.connect(destination)) as new:
            def copied(status, remaining, total):
                self.check_cancel()
            old.backup(new, pages=256, progress=copied)
        raw = destination.with_name('original.db')
        copy_managed_file(destination, raw, file_hash(destination, self.check_cancel), destination.stat().st_size,
                          check_cancel=self.check_cancel)
        CoreRepository(destination)
        return raw

    def _copy(self, source, destination, digest, size):
        self._update('copying_files', current_file=Path(source).name, completed_bytes=0, total_bytes=size)
        def copied(completed, total):
            timestamp = time.monotonic()
            if timestamp - self._last_progress >= 0.25 or completed == total:
                self._last_progress = timestamp
                self._update('copying_files', current_file=Path(source).name, completed_bytes=completed, total_bytes=total)
        if copy_managed_file(source, destination, digest, size, check_cancel=self.check_cancel, progress=copied):
            self.state['copied_files'] += 1

    def _copy_attachment_manifest(self, source_root, candidate, key, mapping):
        match = re.search(r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}', Path(key).name, re.I)
        if match is None:
            return
        source = source_root / 'message-attachments' / (match[0] + '.json')
        if not source.is_file():
            return
        checked_path(source, file=True)
        value = json.loads(source.read_text(encoding='utf-8-sig'))
        rewritten = rewrite_references(value, mapping)
        for prefix in ('', 'model_'):
            storage_key = value.get(prefix + 'storage_key')
            if storage_key:
                self._copy(self._relative(source_root, storage_key),
                           self._relative(candidate, rewrite_storage_key(storage_key, mapping)),
                           value[prefix + 'sha256'], value[prefix + 'byte_size'])
        for artifact in value.get('artifacts', []):
            self._copy(self._relative(source_root, artifact['storage_key']),
                       self._relative(candidate, rewrite_storage_key(artifact['storage_key'], mapping)),
                       artifact['sha256'], artifact['byte_size'])
        identity = mapping.get(match[0], match[0])
        _write_json(candidate / 'message-attachments' / (identity + '.json'), rewritten)

    def _profile_files(self, source_root, snapshot, candidate, mapping):
        with closing(sqlite3.connect(snapshot)) as connection:
            for identity, key, digest, size in connection.execute('SELECT id,storage_key,sha256,byte_size FROM message_attachments'):
                self.check_cancel()
                self._copy(self._relative(source_root, key), self._relative(candidate, rewrite_storage_key(key, mapping)), digest, size)
                self._copy_attachment_manifest(source_root, candidate, key, mapping)
            for key, digest, size in connection.execute('SELECT storage_key,sha256,byte_size FROM attachment_artifacts'):
                self._copy(self._relative(source_root, key), self._relative(candidate, rewrite_storage_key(key, mapping)), digest, size)
            for digest, name, status in connection.execute('SELECT DISTINCT original_hash,original_name,status FROM rag_versions'):
                if not re.fullmatch(r'[a-f0-9]{64}', digest):
                    raise CoreDataError('migration_file_changed', '资料原件哈希无效。')
                key = f'rag/originals/{digest[:2]}/{digest}{Path(name).suffix.lower()}'
                source = self._relative(source_root, key)
                if not source.exists() and status == 'withdrawn':
                    self.state['warnings'].append({'code': 'withdrawn_original_missing', 'name': name})
                    continue
                self._copy(source, self._relative(candidate, key), digest, source.stat().st_size)
            for key, digest, size in connection.execute('SELECT storage_key,sha256,size_bytes FROM document_artifacts'):
                if Path(key).name != key:
                    raise CoreDataError('migration_path_invalid', '文档产物存储标识无效。')
                self._copy(source_root / 'document-artifacts' / key,
                           candidate / 'document-artifacts' / rewrite_storage_key(key, mapping), digest, size)

    def _model_storage(self):
        self.model_root.mkdir(parents=True, exist_ok=True)
        owner = self.model_root / '.agent-model-storage.json'
        if owner.exists():
            value = json.loads(owner.read_text(encoding='utf-8-sig'))
            if value.get('application_id') != self.state['application_id']:
                raise CoreDataError('migration_model_root_occupied', '所选模型目录属于其他应用。请选择新的 agent 模型目录。')
        elif any(self.model_root.iterdir()):
            raise CoreDataError('migration_model_root_occupied', '所选目录包含其他文件，请为 agent 选择独立模型目录。')
        else:
            _write_json(owner, {'application_id': self.state['application_id'], 'schema_version': 1})
        for name in ('main', 'auxiliary', 'ocr'):
            (self.model_root / name).mkdir(exist_ok=True)

    def _auxiliary_models(self, profiles, candidate):
        root = self.model_root / 'auxiliary'
        registry = {'schema_version': 1, 'root': str(root), 'models': []}
        data_dir = candidate / 'rag'
        data_dir.mkdir(exist_ok=True)
        _write_json(data_dir / 'rag-models.json', registry)
        store = ModelStore(data_dir)
        selected, model_sources = {}, []
        for profile in profiles:
            self.check_cancel()
            source_registry = Path(profile['root']) / 'rag/rag-models.json'
            if not source_registry.is_file():
                continue
            value = json.loads(source_registry.read_text(encoding='utf-8-sig'))
            if value.get('schema_version') != 1 or not isinstance(value.get('models'), list):
                raise CoreDataError('migration_model_invalid', '源检索模型登记信息无效。')
            source_root = checked_path(Path(value['root']).resolve())
            if source_root.is_relative_to(root) or root.is_relative_to(source_root):
                if source_root != root:
                    raise CoreDataError('migration_model_root_overlap', '源模型目录与目标目录不能相互包含。')
            for original in value['models']:
                self.check_cancel()
                record = {**original, 'status': 'imported'}
                if not re.fullmatch(r'[a-f0-9]{24}', record['id']) or record['id'] != record['digest'][:24]:
                    raise CoreDataError('migration_model_invalid', '源检索模型身份不一致。')
                existing = selected.get(record['id'])
                if existing and existing['digest'] != record['digest']:
                    raise CoreDataError('migration_model_conflict', '同一模型编号对应不同权重。')
                origin = source_root / record['id']
                destination = root / record['id']
                self._update('verifying_models', current_source=profile['label'], current_model=record['name'])
                try:
                    store._verify(record, root=origin, direct=True)
                except CoreDataError as source_error:
                    try:
                        store._verify(record, root=destination, direct=True)
                    except CoreDataError:
                        raise source_error
                    if existing is None:
                        selected[record['id']] = record
                        registry['models'].append(record)
                    self.state['warnings'].append({'code': 'source_model_repaired', 'severity': 'info',
                                                   'source': profile['label'], 'model': record['name'], 'digest': record['digest']})
                    continue
                manifest = json.loads((origin / '.rag-import.json').read_text(encoding='utf-8-sig'))
                if existing is None:
                    for relative, digest in manifest['files'].items():
                        source_file = self._relative(origin, relative)
                        self._copy(source_file, self._relative(destination, relative), digest, source_file.stat().st_size)
                    metadata = origin / '.rag-import.json'
                    self._copy(metadata, destination / '.rag-import.json', file_hash(metadata, self.check_cancel), metadata.stat().st_size)
                    store._verify(record, root=destination, direct=True)
                    selected[record['id']] = record
                    registry['models'].append(record)
                archive = self.workspace / 'model-metadata' / profile['migration_id'] / record['id'] / '.rag-import.json'
                metadata = origin / '.rag-import.json'
                self._copy(metadata, archive, file_hash(metadata, self.check_cancel), metadata.stat().st_size)
                model_sources.append({'kind': 'auxiliary', 'profile_root': profile['root'], 'source_root': str(origin),
                                      'target_root': str(destination), 'record': original,
                                      'metadata_backups': {'.rag-import.json': str(archive)}, 'files': manifest['files']})
        _write_json(data_dir / 'rag-models.json', registry)
        return model_sources

    def _main_models(self, profiles, candidate):
        root = self.model_root / 'main'
        repository = CoreRepository(candidate / 'agent.db')
        with repository._connect() as connection:
            connection.execute("DELETE FROM local_model_settings WHERE key IN ('pending_import','pending_root')")
            connection.execute("INSERT INTO local_model_settings(key,value) VALUES ('root',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(root),))
        catalog = Catalog(repository, recover=False)
        model_sources = []
        for profile in profiles:
            with closing(sqlite3.connect(profile['snapshot'])) as connection:
                connection.row_factory = sqlite3.Row
                row = connection.execute("SELECT value FROM local_model_settings WHERE key='root'").fetchone()
                source_root = checked_path(Path(row[0] if row else Path(profile['root']) / 'local-models').resolve())
                if source_root != root and (source_root.is_relative_to(root) or root.is_relative_to(source_root)):
                    raise CoreDataError('migration_model_root_overlap', '源模型目录与目标目录不能相互包含。')
                for row in connection.execute('SELECT * FROM local_models'):
                    self.check_cancel()
                    record = catalog._model(row)
                    if content_digest(record['files'], record['adapter_digest']) != record['id']:
                        raise CoreDataError('migration_model_invalid', '源主模型权重身份无效。')
                    self._update('verifying_models', current_source=profile['label'], current_model=record['name'])
                    preferred = catalog.get(record['id'])
                    if len(record['files']) != len(preferred['files']):
                        raise CoreDataError('migration_model_conflict', '同一主模型的分片信息不一致。')
                    origin = source_root / 'objects' / record['id']
                    destination = root / 'objects' / record['id']
                    try:
                        catalog.verify(record, root=source_root, cancel=self.check_cancel)
                    except CoreDataError as source_error:
                        try:
                            catalog.verify(preferred, cancel=self.check_cancel)
                        except CoreDataError:
                            raise source_error
                        self.state['warnings'].append({'code': 'source_model_repaired', 'severity': 'info',
                            'source': profile['label'], 'model': record['name'], 'digest': record['digest']})
                        continue
                    target_files, files = {}, {}
                    for old_file, new_file in zip(record['files'], preferred['files']):
                        if (old_file['sha256'], old_file['size_bytes']) != (new_file['sha256'], new_file['size_bytes']):
                            raise CoreDataError('migration_model_conflict', '同一主模型的分片哈希不一致。')
                        self._copy(self._relative(origin, old_file['name']), self._relative(destination, new_file['name']),
                                   old_file['sha256'], old_file['size_bytes'])
                        target_files[old_file['name']] = new_file['name']
                        files[old_file['name']] = old_file['sha256']
                    catalog.verify(preferred, cancel=self.check_cancel)
                    model_sources.append({'kind': 'main', 'profile_root': profile['root'], 'source_root': str(origin),
                                          'target_root': str(destination), 'record': record, 'files': files,
                                          'target_files': target_files, 'metadata_backups': {}})
        with repository._connect() as connection:
            connection.execute("UPDATE local_models SET status='imported',error=NULL")
        return model_sources

    def _ocr_models(self, profiles, candidate):
        root = self.model_root / 'ocr'
        store = OcrModelStore(root)
        model_sources = []
        for profile in profiles:
            self.check_cancel()
            data_dir = Path(profile['root']) / 'rag'
            configuration = data_dir / 'ocr-storage.json'
            if configuration.is_file():
                value = json.loads(configuration.read_text(encoding='utf-8-sig'))
                if value.get('schema_version') != 1:
                    raise CoreDataError('ocr_model_root_invalid', '源 OCR 模型目录配置无效。')
                source_root = checked_path(Path(value['root']).resolve())
            else:
                source_root = data_dir / 'ocr-models'
            if not source_root.is_dir():
                continue
            if source_root != root and (source_root.is_relative_to(root) or root.is_relative_to(source_root)):
                raise CoreDataError('migration_model_root_overlap', '源 OCR 模型目录与目标目录不能相互包含。')
            for origin in sorted(source_root.iterdir()):
                if not re.fullmatch(r'[a-f0-9]{64}', origin.name) or not origin.is_dir():
                    continue
                self.check_cancel()
                self._update('verifying_models', current_source=profile['label'], current_model=origin.name)
                try:
                    inspected = store.inspect(origin)
                except CoreDataError as source_error:
                    try:
                        verified = store.get_profile(origin.name)
                    except CoreDataError:
                        raise source_error
                    self.state['warnings'].append({'code': 'source_model_repaired', 'severity': 'info',
                        'source': profile['label'], 'model': verified['name'], 'digest': origin.name})
                    continue
                if inspected['id'] != origin.name:
                    raise CoreDataError('migration_model_invalid', '源 OCR 模型身份无效。')
                destination = root / inspected['id']
                try:
                    existing = store.get_profile(inspected['id'])
                except CoreDataError:
                    existing = None
                manifest = inspected['manifest']
                if existing is None:
                    for relative, digest in manifest['files'].items():
                        source_file = self._relative(origin, relative)
                        self._copy(source_file, self._relative(destination, relative), digest, source_file.stat().st_size)
                    metadata = origin / 'ocr-model.json'
                    self._copy(metadata, destination / metadata.name, file_hash(metadata, self.check_cancel), metadata.stat().st_size)
                    store.get_profile(inspected['id'])
                _write_json(destination / '.ocr-meta.json', {key: value for key, value in inspected.items() if key not in {'path', 'manifest'}})
                metadata_backups = {}
                for name in ('ocr-model.json', '.ocr-meta.json'):
                    source_file = origin / name
                    if source_file.is_file():
                        archive = self.workspace / 'model-metadata' / profile['migration_id'] / origin.name / name
                        self._copy(source_file, archive, file_hash(source_file, self.check_cancel), source_file.stat().st_size)
                        metadata_backups[name] = str(archive)
                model_sources.append({'kind': 'ocr', 'profile_root': profile['root'], 'source_root': str(origin),
                                      'target_root': str(destination), 'record': {key: value for key, value in inspected.items() if key != 'manifest'},
                                      'files': manifest['files'], 'metadata_backups': metadata_backups})
        _write_json(candidate / 'rag/ocr-storage.json', {'schema_version': 1, 'root': str(root)})
        return model_sources

    def prepare(self, sources):
        from agent.profile_preflight import preview
        estimate = preview(self.target_root, self.model_root, self.workspace, sources, check_cancel=self.check_cancel)
        self._update('preflight', preview=estimate)
        if not estimate['can_prepare']:
            raise CoreDataError('migration_insufficient_space', '迁移暂存盘或模型盘空间不足，请释放空间或更换目标磁盘。')
        attempt = self.workspace / ('attempt-' + uuid.uuid4().hex)
        candidate = attempt / 'candidate'
        candidate.mkdir(parents=True)
        preferences = ProfilePreferences(candidate, published_root=self.target_root, check_cancel=self.check_cancel)
        profiles = []
        try:
            self._update('snapshotting', candidate_root=str(candidate))
            if (self.target_root / 'agent.db').is_file():
                current = attempt / 'sources/current/agent.db'
                raw = self._snapshot(self.target_root / 'agent.db', current)
                source_state = profile_state(self.target_root, database=raw, check_cancel=self.check_cancel)
                self._snapshot(current, candidate / 'agent.db')
                with closing(sqlite3.connect(current)) as connection:
                    configured = bool(connection.execute('SELECT 1 FROM projects UNION ALL SELECT 1 FROM conversations UNION ALL SELECT 1 FROM local_models LIMIT 1').fetchone())
                preferences.add_settings(self.target_root, 'agent', preferred=configured)
                preferences.add_files(self.target_root, 'agent', {})
                self._profile_files(self.target_root, candidate / 'agent.db', candidate, {})
                profiles.append({'root': str(self.target_root), 'snapshot': str(current), 'label': 'agent',
                                 'raw_snapshot': str(raw), 'source_state': source_state, 'migration_id': 'current', 'id_map': {}})
            else:
                CoreRepository(candidate / 'agent.db')
            for index, selected in enumerate(sources):
                self.check_cancel()
                root = checked_path(Path(selected['root']).resolve())
                if root == self.target_root:
                    raise CoreDataError('migration_source_invalid', '当前应用数据由迁移器单独管理。')
                snapshot = attempt / 'sources' / str(index) / 'agent.db'
                raw = self._snapshot(root / 'agent.db', snapshot)
                source_state = profile_state(root, database=raw, check_cancel=self.check_cancel)
                provider_map = preferences.add_settings(root, selected['label'])
                preferences.remap_database(snapshot, provider_map)
                self._update('merging', current_source=selected['label'])
                report = merge_snapshots(candidate / 'agent.db', snapshot, source_label=selected['label'], check_cancel=self.check_cancel)
                self._update('copying_documents')
                self._profile_files(root, snapshot, candidate, report['id_map'])
                preferences.add_files(root, selected['label'], report['id_map'])
                profiles.append({'root': str(root), 'snapshot': str(snapshot), 'raw_snapshot': str(raw),
                                 'source_state': source_state, 'provider_map': provider_map, 'label': selected['label'], **report})
            self._model_storage()
            model_sources = self._auxiliary_models(profiles, candidate)
            model_sources.extend(self._main_models(profiles, candidate))
            model_sources.extend(self._ocr_models(profiles, candidate))
            if profiles:
                preferred = profiles[0]
                if preferred['migration_id'] == 'current' and len(profiles) > 1:
                    from agent.global_instructions import DEFAULT_GLOBAL_INSTRUCTIONS
                    with closing(sqlite3.connect(preferred['snapshot'])) as connection:
                        empty = not connection.execute('SELECT 1 FROM projects UNION ALL SELECT 1 FROM conversations UNION ALL SELECT 1 FROM local_models LIMIT 1').fetchone()
                        instructions = connection.execute('SELECT content,enabled FROM global_instructions WHERE id=1').fetchone()
                    if empty and instructions == (DEFAULT_GLOBAL_INSTRUCTIONS, 1):
                        preferred = profiles[1]
                with closing(sqlite3.connect(preferred['snapshot'])) as source, closing(sqlite3.connect(candidate / 'agent.db')) as destination, destination:
                    row = source.execute('SELECT * FROM global_instructions WHERE id=1').fetchone()
                    destination.execute('DELETE FROM global_instructions')
                    destination.execute('INSERT INTO global_instructions VALUES (' + ','.join('?' for _ in row) + ')', row)
            for profile in profiles:
                rebase_database_paths(candidate / 'agent.db', Path(profile['root']), self.target_root, check_cancel=self.check_cancel)
            # Apply the existing startup recovery rules to the isolated copy.
            # Completed operations retain their original audit records.
            from agent.repositories.execution_repository import ExecutionRepository
            from agent.context_engine import ContextEngineService
            from agent.tool_repository import ToolRepository
            from agent.rag.repository import RagRepository
            repository = CoreRepository(candidate / 'agent.db')
            repository.interrupt_active_runs()
            ExecutionRepository(repository.database_path).interrupt_active()
            ContextEngineService(repository).context_repository.interrupt_compactions()
            ToolRepository(repository.database_path).interrupt_active()
            RagRepository(repository)
            with repository._connect() as connection:
                connection.execute("UPDATE local_model_jobs SET status='interrupted',error=?,updated_at=? WHERE status IN ('queued','running')",
                    (json.dumps({'code': 'interrupted', 'message': 'Migration preserved this unfinished job for review.'}), _utc_now()))
            self.check_cancel()
            with closing(sqlite3.connect(candidate / 'agent.db')) as connection:
                if connection.execute('PRAGMA foreign_key_check').fetchall():
                    raise CoreDataError('migration_integrity_failed', '迁移候选资料存在无效关联。')
            self.state['warnings'].extend(preferences.warnings)
            result = {'candidate_root': str(candidate), 'profiles': profiles, 'model_sources': model_sources,
                      'credentials': preferences.credentials}
            _write_json(self.workspace / 'candidate.json', result)
            self._update('prepared', status='prepared')
            return result
        except InterruptedError:
            self._update(self.state['phase'], status='cancelled')
            raise
        except Exception as error:
            self._update(self.state['phase'], status='failed', error={'code': getattr(error, 'code', 'migration_failed'), 'message': str(error)})
            raise
