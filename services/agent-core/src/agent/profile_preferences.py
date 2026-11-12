"""Merge profile configuration into an isolated, locally owned candidate."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

from agent.local_models.paths import checked_path, file_hash
from agent.profile_files import copy_managed_file
from agent.profile_merge import _quote, _JSON_COLUMNS, refresh_content_hashes, rewrite_references
from agent.rag.models import _write_json
from agent.storage import CoreDataError


def _read(path, default):
    if not path.exists():
        return default
    checked_path(path, file=True)
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except (ValueError, OSError) as error:
        raise CoreDataError('migration_configuration_invalid', f'无法读取配置文件 {path.name}。') from error


_PROVIDER_FIELDS = {'provider_profile_id', 'coordinator_provider_profile_id', 'provider_id', 'providerProfileId'}


def _provider_references(value, mapping):
    if isinstance(value, dict):
        return {key: mapping.get(item, item) if key in _PROVIDER_FIELDS and isinstance(item, str)
                else _provider_references(item, mapping) for key, item in value.items()}
    if isinstance(value, list):
        return [_provider_references(item, mapping) for item in value]
    return value


def _provider_column(column, value, mapping):
    if column in _PROVIDER_FIELDS and isinstance(value, str):
        return mapping.get(value, value)
    if isinstance(value, str) and (column.endswith('_json') or column in _JSON_COLUMNS):
        try:
            parsed = json.loads(value)
        except ValueError:
            return value
        rewritten = _provider_references(parsed, mapping)
        if rewritten != parsed:
            return json.dumps(rewritten, ensure_ascii=False, separators=(',', ':'))
    return value


def rebase_managed_paths(value, source_root, target_root, *, field=''):
    if isinstance(value, dict):
        return {key: rebase_managed_paths(item, source_root, target_root, field=key) for key, item in value.items()}
    if isinstance(value, list):
        return [rebase_managed_paths(item, source_root, target_root, field=field) for item in value]
    if isinstance(value, str) and (field in {'path', 'root', 'directory'} or field.endswith(('_path', '_root'))):
        path = Path(value)
        if path.is_absolute() and path.is_relative_to(source_root):
            return str(Path(target_root) / path.relative_to(source_root))
    return value


def rebase_database_paths(database, source_root, target_root, *, check_cancel=lambda: None):
    def transform(column, value):
        if isinstance(value, str) and (column.endswith('_json') or column in _JSON_COLUMNS):
            try:
                parsed = json.loads(value)
            except ValueError:
                return value
            rewritten = rebase_managed_paths(parsed, source_root, target_root)
            return value if rewritten == parsed else json.dumps(rewritten, ensure_ascii=False, separators=(',', ':'))
        return rebase_managed_paths(value, source_root, target_root, field=column)
    _update_database_rows(database, transform, check_cancel)


def _update_database_rows(database, transform, check_cancel):
    with closing(sqlite3.connect(database)) as connection, connection:
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
                  if not row[0].startswith(('sqlite_', 'rag_fts'))]
        for table in tables:
            columns = connection.execute(f'PRAGMA table_info({_quote(table)})').fetchall()
            names = [column[1] for column in columns]
            primary = [column[1] for column in columns if column[5]]
            if not primary:
                continue
            for row in connection.execute(f'SELECT * FROM {_quote(table)}'):
                check_cancel()
                values = dict(zip(names, row))
                rewritten = {key: transform(key, value) for key, value in values.items()}
                rewritten = refresh_content_hashes(table, values, rewritten)
                if rewritten == values:
                    continue
                assignments = ','.join(_quote(name) + '=?' for name in names)
                where = ' AND '.join(_quote(name) + '=?' for name in primary)
                connection.execute(f'UPDATE {_quote(table)} SET {assignments} WHERE {where}',
                                   [rewritten[name] for name in names] + [values[name] for name in primary])


class ProfilePreferences:
    def __init__(self, candidate, *, published_root=None, check_cancel=lambda: None):
        self.candidate = Path(candidate)
        self.candidate.mkdir(parents=True, exist_ok=True)
        self.published_root = Path(published_root or candidate)
        self.check_cancel = check_cancel
        self.settings = None
        self.preferred = False
        self.grants = {'projects': {}, 'works': {}}
        self.skills = []
        self.credentials = []
        self.warnings = []

    def add_settings(self, source_root, label, *, preferred=True):
        source_root = Path(source_root)
        stored = _read(source_root / 'settings.json', {})
        settings = stored.get('settings', stored)
        if not settings:
            return {}
        if not isinstance(settings, dict) or not isinstance(settings.get('models', {}), dict):
            raise CoreDataError('migration_configuration_invalid', '模型设置结构无效。')
        providers = settings.get('models', {}).get('providers', [])
        if not isinstance(providers, list) or any(not isinstance(p, dict) or not isinstance(p.get('id'), str) for p in providers):
            raise CoreDataError('migration_configuration_invalid', '模型提供方配置无效。')
        preferred = preferred or bool(providers) or bool(settings.get('models', {}).get('default_model_id'))
        if self.settings is None or (preferred and not self.preferred):
            existing = self.settings['models']['providers'] if self.settings else []
            self.settings = {**settings, 'models': {**settings.get('models', {}), 'providers': existing}}
            self.preferred = preferred
        target = self.settings['models'].setdefault('providers', [])
        registered = {provider['id']: provider for provider in target}
        mapping = {}
        for provider in providers:
            self.check_cancel()
            identity = provider['id']
            if identity in registered and registered[identity] != provider:
                digest = hashlib.sha256(json.dumps(provider, sort_keys=True).encode()).hexdigest()[:16]
                identity = f'migrated-{digest}'
                mapping[provider['id']] = identity
            if identity not in registered:
                migrated = {**provider, 'id': identity}
                if identity != provider['id']:
                    migrated['name'] = f'{provider.get("name", provider["id"])} · {label}'
                    if provider.get('credential_ref'):
                        migrated['credential_ref'] = identity
                target.append(migrated)
                registered[identity] = migrated
            if provider.get('credential_ref'):
                self.credentials.append({'source_root': str(source_root), 'source_id': provider['id'], 'target_id': identity})
        _write_json(self.candidate / 'settings.json', {'settings': self.settings})
        return mapping

    def remap_database(self, database, mapping):
        if not mapping:
            return
        _update_database_rows(database, lambda column, value: _provider_column(column, value, mapping), self.check_cancel)

    def add_files(self, source_root, label, mapping):
        source_root = Path(source_root)
        grants = _read(source_root / 'work-tool-grants.json', {'projects': {}, 'works': {}})
        if not isinstance(grants, dict) or any(not isinstance(grants.get(key, {}), dict) for key in ('projects', 'works')):
            raise CoreDataError('migration_configuration_invalid', '项目目录授权配置无效。')
        grants = rewrite_references(grants, mapping)
        for group in ('projects', 'works'):
            for identity, record in grants.get(group, {}).items():
                self.grants[group].setdefault(identity, record)
        _write_json(self.candidate / 'work-tool-grants.json', self.grants)
        entries = _read(source_root / 'skills.json', [])
        if not isinstance(entries, list):
            raise CoreDataError('migration_configuration_invalid', '技能配置无效。')
        for entry in entries:
            self.check_cancel()
            if not isinstance(entry, dict) or not isinstance(entry.get('path'), str) or not isinstance(entry.get('id'), str):
                raise CoreDataError('migration_configuration_invalid', '技能配置缺少路径或身份。')
            original = checked_path(Path(entry['path']).resolve())
            migrated = dict(entry)
            if original.is_relative_to(source_root.resolve()):
                key = hashlib.sha256((str(original) + '\0' + label).encode()).hexdigest()[:24]
                destination = self.candidate / 'skills' / key
                if not original.is_dir():
                    migrated['enabled'] = False
                    self.warnings.append({'code': 'skill_package_missing', 'source': label, 'name': entry.get('name')})
                else:
                    for item in original.rglob('*'):
                        self.check_cancel()
                        checked_path(item)
                        if item.is_file():
                            copy_managed_file(item, destination / item.relative_to(original), file_hash(item, self.check_cancel),
                                              item.stat().st_size, check_cancel=self.check_cancel)
                migrated['path'] = str(self.published_root / 'skills' / key)
            prior = next((item for item in self.skills if item['path'] == migrated['path']), None)
            if prior:
                continue
            if any(item.get('name') == migrated.get('name') for item in self.skills):
                migrated['enabled'] = False
                self.warnings.append({'code': 'skill_name_conflict', 'source': label, 'name': migrated.get('name')})
            self.skills.append(migrated)
        _write_json(self.candidate / 'skills.json', self.skills)
