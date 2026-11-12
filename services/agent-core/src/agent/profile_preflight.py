"""Read-only conflict and storage estimates before copying profile data."""
from contextlib import closing
import json
from pathlib import Path
import re
import shutil
import sqlite3

from agent.local_models.paths import checked_path
from agent.profile_merge import _graph, _tables
from agent.storage import CoreDataError


def _json(path, default):
    if not path.exists():
        return default
    checked_path(path, file=True)
    if path.stat().st_size > 8 * 1024 ** 2:
        raise CoreDataError('migration_configuration_invalid', '迁移配置超过大小限制。')
    return json.loads(path.read_text(encoding='utf-8-sig'))


def _tree_size(root, check_cancel):
    if not root.exists():
        return 0
    checked_path(root)
    total = 0
    for path in root.rglob('*'):
        check_cancel()
        checked_path(path)
        if path.is_file():
            total += path.stat().st_size
    return total


def _model_roots(root, connection):
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'local_models' in tables:
        configured = connection.execute("SELECT value FROM local_model_settings WHERE key='root'").fetchone()
        main = Path(configured[0]) if configured else root / 'local-models'
        for identity, in connection.execute('SELECT id FROM local_models'):
            if not re.fullmatch('[a-f0-9]{64}', identity):
                raise CoreDataError('migration_model_invalid', '源模型身份无效。')
            yield main / 'objects' / identity
    auxiliary = _json(root / 'rag/rag-models.json', {})
    for model in auxiliary.get('models', []):
        identity = model.get('id', '')
        if not re.fullmatch('[a-f0-9]{24}', identity):
            raise CoreDataError('migration_model_invalid', '源检索模型身份无效。')
        yield Path(auxiliary['root']) / identity
    ocr = Path(_json(root / 'rag/ocr-storage.json', {}).get('root', root / 'rag/ocr-models'))
    if ocr.is_dir():
        for child in ocr.iterdir():
            if re.fullmatch('[a-f0-9]{64}', child.name):
                yield child


def _storage(path, purpose, required):
    path = Path(path).resolve()
    existing = path
    while not existing.exists():
        existing = existing.parent
    usage = shutil.disk_usage(existing)
    return {'purpose': purpose, 'path': str(path), 'volume': path.anchor.casefold(),
            'required_bytes': required, 'available_bytes': usage.free}


def preview(target, model_root, workspace, sources, *, check_cancel=lambda: None):
    target, model_root = checked_path(Path(target).resolve()), checked_path(Path(model_root).resolve())
    profiles = ([{'root': str(target), 'label': 'agent'}] if (target / 'agent.db').is_file() else []) + sources
    database_bytes, document_bytes, model_bytes, conflicting_groups = 0, 0, 0, 0
    seen_nodes, seen_models = {}, set()
    for profile in profiles:
        check_cancel()
        root = checked_path(Path(profile['root']).resolve())
        database = checked_path(root / 'agent.db', file=True)
        database_bytes += database.stat().st_size
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as connection:
            connection.execute('BEGIN')
            hashes, groups, _ = _graph(connection, _tables(connection))
            for group in groups:
                overlap = group.intersection(seen_nodes)
                if overlap and any(seen_nodes[node] != hashes[node] for node in overlap):
                    conflicting_groups += 1
            seen_nodes.update(hashes)
            for directory in _model_roots(root, connection):
                directory = checked_path(directory.resolve())
                if directory not in seen_models:
                    model_bytes += _tree_size(directory, check_cancel)
                    seen_models.add(directory)
        for key in ('message-attachments', 'document-artifacts', 'rag/originals', 'skills', 'file-recovery'):
            document_bytes += _tree_size(root / key, check_cancel)
    storage = [_storage(workspace, 'profile', database_bytes * 4 + document_bytes * 2 + 128 * 1024 ** 2),
               _storage(model_root, 'models', model_bytes + 64 * 1024 ** 2)]
    for item in storage:
        required_on_volume = sum(entry['required_bytes'] for entry in storage if entry['volume'] == item['volume'])
        item['sufficient'] = item['available_bytes'] >= required_on_volume
    return {'sources': [{'label': entry['label'], 'root': entry['root']} for entry in profiles],
            'conflicting_groups': conflicting_groups, 'storage': storage,
            'can_prepare': all(item['sufficient'] for item in storage)}
