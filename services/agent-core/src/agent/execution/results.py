"""Task-scoped immutable UTF-8 tool outputs; model views are bounded projections."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from agent.storage import CoreDataError, _open_database, _utc_now


RESULT_TOOLS = ('tools.read_result', 'tools.search_result')


class ResultStore:
    def __init__(self, database_path):
        self.database_path = Path(database_path)
        self.root = self.database_path.parent / 'tool-results'

    def path(self, digest):
        if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise CoreDataError('tool_result_invalid', '结果身份无效。')
        return self.root / digest[:2] / (digest + '.txt')

    def save(self, operation_id, text, *, success, connection=None):
        data = text.encode('utf-8')
        digest = hashlib.sha256(data).hexdigest()
        destination = self.path(digest)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix('.' + uuid4().hex + '.tmp')
        try:
            with temporary.open('xb') as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        def register(db):
            operation = db.execute('SELECT * FROM tool_operations WHERE id=?', (operation_id,)).fetchone()
            if operation is None:
                raise CoreDataError('tool_result_invalid', '工具操作不存在。')
            previous = db.execute('SELECT sha256,success FROM tool_result_objects WHERE operation_id=?', (operation_id,)).fetchone()
            if previous and (previous['sha256'] != digest or bool(previous['success']) != bool(success)):
                raise CoreDataError('tool_result_conflict', '已保存的工具结果不可替换。')
            arguments = json.loads(operation['arguments_json'])
            source = {'tool': operation['tool_name'], 'arguments': {key: arguments[key] for key in
                ('attachment_id', 'template_id', 'artifact_id', 'evidence_id', 'result_id', 'path') if key in arguments}}
            try:
                value = json.loads(text)
                if isinstance(value, dict) and isinstance(value.get('source_sha256'), str):
                    source['source_sha256'] = value['source_sha256']
            except ValueError:
                pass
            db.execute('''INSERT OR IGNORE INTO tool_result_objects VALUES (?,?,?,?,?,?,?,?)''',
                (operation_id, operation['run_id'], digest, len(data), len(text), int(success),
                 json.dumps(source, ensure_ascii=False), _utc_now()))
            return {'result_id': operation_id, 'sha256': digest, 'byte_size': len(data),
                    'character_count': len(text), 'success': bool(success), 'complete': True}
        if connection is not None:
            return register(connection)
        with _open_database(self.database_path) as db:
            return register(db)

    def metadata(self, run_id, result_id):
        with _open_database(self.database_path) as db:
            row = db.execute('SELECT * FROM tool_result_objects WHERE operation_id=? AND run_id=?', (result_id, run_id)).fetchone()
        if row is None:
            raise CoreDataError('tool_result_forbidden', '该结果不属于当前任务或已被删除。')
        with _open_database(self.database_path) as db:
            operation = db.execute('SELECT error_code FROM tool_operations WHERE id=?', (result_id,)).fetchone()
        return {'result_id': row['operation_id'], **dict(row), 'source': json.loads(row['source_json']),
                'error_code': operation['error_code'] if operation else None,
                'success': bool(row['success']), 'complete': True}

    def _text(self, metadata):
        try:
            data = self.path(metadata['sha256']).read_bytes()
        except OSError as error:
            raise CoreDataError('tool_result_unavailable', '已保存结果不可用。') from error
        if len(data) != metadata['byte_size'] or hashlib.sha256(data).hexdigest() != metadata['sha256']:
            raise CoreDataError('tool_result_corrupt', '已保存结果校验失败。')
        return data.decode('utf-8')

    @staticmethod
    def _cursor(metadata, offset):
        return base64.urlsafe_b64encode(json.dumps([metadata['result_id'], metadata['sha256'], offset]).encode()).decode()

    def read(self, run_id, result_id=None, *, cursor=None, length=4000):
        if result_id is None:
            sequence = 2**63 - 1
            if cursor:
                try:
                    kind, owner, sequence = json.loads(base64.urlsafe_b64decode(cursor))
                    if kind != 'catalog' or owner != run_id or type(sequence) is not int or sequence < 0:
                        raise ValueError()
                except (ValueError, TypeError, UnicodeError) as error:
                    raise CoreDataError('tool_result_cursor_invalid', '结果目录游标无效。') from error
            with _open_database(self.database_path) as db:
                rows = db.execute('''SELECT r.operation_id AS result_id,r.sha256,r.byte_size,r.success,
                    o.tool_name,o.sequence FROM tool_result_objects r JOIN tool_operations o ON o.id=r.operation_id
                    WHERE r.run_id=? AND o.sequence<? ORDER BY o.sequence DESC LIMIT 21''', (run_id, sequence)).fetchall()
            values = [dict(row) for row in rows[:20]]
            next_cursor = base64.urlsafe_b64encode(json.dumps(['catalog', run_id, values[-1]['sequence']]).encode()).decode() if len(rows) > 20 else None
            return {'results': values, 'scope': 'current_run', 'limit': 20, 'next_cursor': next_cursor}
        metadata = self.metadata(run_id, result_id)
        if type(length) is not int or not 1 <= length <= 16000:
            raise CoreDataError('invalid_tool_call', '结果读取长度无效。')
        offset = 0
        if cursor is not None:
            try:
                identity, digest, offset = json.loads(base64.urlsafe_b64decode(cursor))
                if identity != result_id or digest != metadata['sha256'] or type(offset) is not int or not 0 <= offset <= metadata['character_count']:
                    raise ValueError()
            except (ValueError, TypeError, UnicodeError) as error:
                raise CoreDataError('tool_result_cursor_invalid', '结果读取游标已失效。') from error
        text = self._text(metadata)
        end = min(len(text), offset + length)
        return {'result_id': result_id, 'sha256': metadata['sha256'], 'trust': 'untrusted',
                'offset': offset, 'content': text[offset:end], 'total': len(text),
                'next_cursor': self._cursor(metadata, end) if end < len(text) else None}

    def search(self, run_id, result_id, query, *, limit=10):
        if not isinstance(query, str) or not query or len(query) > 1000 or type(limit) is not int or not 1 <= limit <= 50:
            raise CoreDataError('invalid_tool_call', '结果搜索参数无效。')
        metadata = self.metadata(run_id, result_id)
        text = self._text(metadata)
        matches, start = [], 0
        while len(matches) <= limit:
            offset = text.find(query, start)
            if offset < 0:
                break
            matches.append({'offset': offset, 'content': text[max(0, offset - 100):offset + len(query) + 100],
                            'cursor': self._cursor(metadata, max(0, offset - 100))})
            start = offset + len(query)
        return {'result_id': result_id, 'matches': matches[:limit], 'truncated': len(matches) > limit, 'trust': 'untrusted'}

    def envelope(self, metadata, *, max_characters=4000):
        value = {key: metadata[key] for key in ('result_id', 'sha256', 'success', 'complete', 'byte_size')}
        if metadata.get('error_code'):
            value['error_code'] = metadata['error_code']
        value.update({'trust': 'untrusted', 'preview': '', 'next_action': 'Use tools.read_result or tools.search_result for full content.'})
        body = self._text(metadata)
        if metadata.get('error_code'):
            body = 'ERROR ' + metadata['error_code'] + ': ' + body
        available = max(0, max_characters - len(json.dumps(value, ensure_ascii=False)) - 16)
        value['preview'] = body[:available]
        while len(json.dumps(value, ensure_ascii=False)) > max_characters and value['preview']:
            value['preview'] = value['preview'][:len(value['preview']) // 2]
        return json.dumps(value, ensure_ascii=False)
