"""Managed durable index for source-bound incremental attachment reads."""
import hashlib
import json
import sqlite3
from pathlib import Path

from agent.document_parser.protocol import plain_path
from agent.storage import CoreDataError


class ReadIndex:
    def __init__(self, root, identity):
        self.path = Path(root) / (identity + '.document-index.sqlite')
        if self.path.exists():
            plain_path(self.path)
            if self.path.stat().st_nlink != 1:
                raise CoreDataError('attachment_changed', '文档索引完整性校验失败。')
        self.connection = sqlite3.connect(self.path)
        self.connection.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)')
        self.connection.execute('CREATE TABLE IF NOT EXISTS blocks (position INTEGER PRIMARY KEY, id TEXT UNIQUE, payload TEXT, digest TEXT, page INTEGER, heading TEXT)')
        self.connection.execute('CREATE INDEX IF NOT EXISTS block_pages ON blocks(page,position)')
        self.identity = identity

    def get(self, key, default=None):
        row = self.connection.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        self.connection.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', (key, json.dumps(value, ensure_ascii=False)))
        self.connection.commit()

    def append(self, chunks):
        for chunk in chunks:
            locator = chunk['locator']
            parent = locator.get('block_id', str(locator))
            fragment = locator.get('offset', 0)
            heading = locator.get('heading_path', locator.get('section', []))
            heading = ' / '.join(str(item) for item in heading) if isinstance(heading, list) else str(heading)
            for offset in range(0, len(chunk['text']), 256):
                block = {'id': f'{parent}-fragment-{fragment}-char-{offset}', 'text': chunk['text'][offset:offset + 256],
                         'locator': locator, 'character_offset': fragment + offset}
                payload = json.dumps(block, ensure_ascii=False, sort_keys=True)
                digest = hashlib.sha256(payload.encode()).hexdigest()
                row = self.connection.execute('SELECT digest FROM blocks WHERE id=?', (block['id'],)).fetchone()
                if row is not None and row[0] != digest:
                    raise CoreDataError('attachment_changed', '文档索引来源发生变化。')
                self.connection.execute('INSERT OR IGNORE INTO blocks(id,payload,digest,page,heading) VALUES (?,?,?,?,?)',
                    (block['id'], payload, digest, locator.get('physical_page', locator.get('page')), heading.casefold()))
        self.connection.commit()

    def page(self, offset, limit, physical_page, section):
        clauses, values = [], []
        if physical_page is not None:
            clauses.append('page=?')
            values.append(physical_page)
        if section is not None:
            clauses.append('instr(heading,?)>0')
            values.append(section.casefold())
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        total = self.connection.execute('SELECT COUNT(*) FROM blocks' + where, values).fetchone()[0]
        rows = self.connection.execute('SELECT payload,digest FROM blocks' + where + ' ORDER BY position LIMIT ? OFFSET ?',
            [*values, limit, offset]).fetchall()
        blocks = []
        for payload, digest in rows:
            if hashlib.sha256(payload.encode()).hexdigest() != digest:
                raise CoreDataError('attachment_changed', '文档索引完整性校验失败。')
            blocks.append(json.loads(payload))
        return total, blocks

    def coverage(self):
        return [json.loads(row[0]) for row in self.connection.execute('SELECT payload FROM blocks ORDER BY position')]

    def close(self):
        self.connection.close()
