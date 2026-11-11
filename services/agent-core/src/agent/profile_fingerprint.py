"""Content identities for detecting edits made after migration snapshots."""
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3

from agent.local_models.paths import checked_path, file_hash
from agent.profile_merge import _quote, _row_hash
from agent.storage import CoreDataError


def profile_state(root, *, database=None, check_cancel=lambda: None):
    root = checked_path(Path(root).resolve())
    path = checked_path(Path(database or root / 'agent.db').resolve(), file=True)
    digest = hashlib.sha256()
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as connection:
        connection.execute('BEGIN')
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
                  if not row[0].startswith(('sqlite_', 'rag_fts'))]
        for table in tables:
            check_cancel()
            hashes = []
            columns = [row[1] for row in connection.execute(f'PRAGMA table_info({_quote(table)})')]
            for row in connection.execute(f'SELECT * FROM {_quote(table)}'):
                check_cancel()
                if table == 'local_models':
                    # Residency/error observations change during a normal Core stop.
                    # Model identity, selected device and context configuration remain checked.
                    row = tuple(None if name in {'status', 'error'} else value for name, value in zip(columns, row))
                hashes.append(_row_hash(row))
            digest.update(table.encode('utf8') + b'\0' + ''.join(sorted(hashes)).encode('ascii'))
    files = {}
    for key in ('settings.json', 'skills.json', 'work-tool-grants.json', 'rag/rag-models.json', 'rag/ocr-storage.json'):
        path = root / key
        files[key] = file_hash(checked_path(path, file=True), check_cancel) if path.exists() else None
    return {'database': digest.hexdigest(), 'files': files}


def check_source_states(profiles, *, check_cancel=lambda: None):
    for profile in profiles:
        expected = profile.get('source_state')
        if not expected or profile_state(profile['root'], check_cancel=check_cancel) != expected:
            raise CoreDataError('migration_source_changed', '准备期间原应用资料或设置发生了更改，请重新准备迁移。原资料已保留。')
