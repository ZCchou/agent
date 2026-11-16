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


def _source_ref(source, *, run_id=None):
    """Bound public provenance without shortening identifiers into other identities."""
    if (run_id is not None and source['tool'] in RESULT_TOOLS
            and source.get('arguments', {}).get('result_id') == 'user-input'):
        return {'kind': 'original_user_message', 'run_id': run_id}
    value = {'tool': source['tool'], 'arguments': {}, 'range': {}}
    for key, item in source.get('arguments', {}).items():
        if isinstance(item, str) and len(item) <= 256:
            value['arguments'][key] = item
        else:
            value['truncated'] = True
    digest = source.get('source_sha256')
    if isinstance(digest, str) and len(digest) == 64:
        value['source_sha256'] = digest
    for key, item in source.get('range', {}).items():
        if (type(item) is int and 0 <= item <= 2**63 - 1
                or item is None or isinstance(item, str) and len(item) <= 256):
            value['range'][key] = item
        else:
            value['truncated'] = True
    return value


def _result_navigation(tool_name, arguments, value):
    """Copy only opaque continuation returned by a genuine saved-result tool."""
    if tool_name not in RESULT_TOOLS or not isinstance(value, dict):
        return {}
    entry = {} if arguments.get('result_id') is None else {'result_id': arguments['result_id']}
    navigation = {'read_entry': entry}
    cursor = value.get('next_cursor')
    if tool_name == 'tools.read_result' and isinstance(cursor, str) and 0 < len(cursor) <= 1024:
        navigation['next_read'] = {**entry, 'cursor': cursor}
    return navigation


def _identity(metadata):
    return {'result_id': metadata['result_id'], 'result_sha256': metadata['sha256'],
            'sha256': metadata['sha256'], 'source_ref': metadata.get('source_ref')}


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
            source['range'] = {key: arguments[key] for key in
                ('offset', 'limit', 'length', 'physical_page', 'section') if key in arguments}
            try:
                value = json.loads(text)
                if isinstance(value, dict):
                    if isinstance(value.get('source_sha256'), str):
                        source['source_sha256'] = value['source_sha256']
                    source['range'].update({key: value[key] for key in
                        ('offset', 'next_offset', 'physical_page', 'section') if key in value})
                    if success:
                        source.update(_result_navigation(operation['tool_name'], arguments, value))
            except ValueError:
                pass
            db.execute('''INSERT OR IGNORE INTO tool_result_objects VALUES (?,?,?,?,?,?,?,?)''',
                (operation_id, operation['run_id'], digest, len(data), len(text), int(success),
                 json.dumps(source, ensure_ascii=False), _utc_now()))
            return {'result_id': operation_id, 'result_sha256': digest, 'sha256': digest,
                    'source_ref': _source_ref(source, run_id=operation['run_id']), 'byte_size': len(data),
                    'character_count': len(text), 'success': bool(success), 'complete': True,
                    **{key: source[key] for key in ('read_entry', 'next_read') if key in source}}
        if connection is not None:
            return register(connection)
        with _open_database(self.database_path) as db:
            return register(db)

    @staticmethod
    def project(value, metadata, *, tool_name, arguments, run_id):
        """Attach saved-result identities without trusting provenance from tool text."""
        source_ref = metadata.get('source_ref')
        if tool_name in RESULT_TOOLS and arguments.get('result_id') == 'user-input':
            source_ref = {'kind': 'original_user_message', 'run_id': run_id}
        projected = {key: item for key, item in value.items() if key not in {'read_entry', 'next_read'}}
        navigation = _result_navigation(tool_name, arguments, value) if metadata.get('success') else {}
        projected.update(result_id=metadata['result_id'], result_sha256=metadata['sha256'],
            source_ref=source_ref, read_entry={'result_id': metadata['result_id']},
            _saved_result={key: metadata[key] for key in ('sha256', 'success', 'byte_size')})
        projected.update(navigation)
        if tool_name == 'tools.search_result' and isinstance(value.get('matches'), list):
            matches = []
            for match in value['matches']:
                if not isinstance(match, dict):
                    matches.append(match)
                    continue
                item = {key: field for key, field in match.items() if key != 'read_entry'}
                cursor = item.get('cursor')
                if navigation and isinstance(cursor, str) and 0 < len(cursor) <= 1024:
                    item['read_entry'] = {**navigation['read_entry'], 'cursor': cursor}
                matches.append(item)
            projected['matches'] = matches
        return projected

    def metadata(self, run_id, result_id):
        with _open_database(self.database_path) as db:
            row = db.execute('SELECT * FROM tool_result_objects WHERE operation_id=? AND run_id=?', (result_id, run_id)).fetchone()
        if row is None:
            raise CoreDataError('tool_result_forbidden', '该结果不属于当前任务或已被删除。')
        with _open_database(self.database_path) as db:
            operation = db.execute('SELECT error_code FROM tool_operations WHERE id=?', (result_id,)).fetchone()
        source = json.loads(row['source_json'])
        return {'result_id': row['operation_id'], **dict(row), 'result_sha256': row['sha256'],
                'source': source, 'source_ref': _source_ref(source, run_id=run_id),
                'error_code': operation['error_code'] if operation else None,
                'success': bool(row['success']), 'complete': True,
                **{key: source[key] for key in ('read_entry', 'next_read') if key in source}}

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
            if cursor is not None:
                try:
                    kind, owner, sequence = json.loads(base64.urlsafe_b64decode(cursor))
                    if kind != 'catalog' or owner != run_id or type(sequence) is not int or sequence < 0:
                        raise ValueError()
                except (ValueError, TypeError, UnicodeError) as error:
                    raise CoreDataError('tool_result_cursor_invalid', '结果目录游标无效。',
                        {'first_read': {'cursor': None}, 'instruction': 'Omit cursor or use null for the first read. Copy returned next_cursor to continue.'}) from error
            with _open_database(self.database_path) as db:
                rows = db.execute('''SELECT r.operation_id AS result_id,r.sha256,r.byte_size,r.character_count,r.success,r.source_json,
                    o.tool_name,o.sequence FROM tool_result_objects r JOIN tool_operations o ON o.id=r.operation_id
                    WHERE r.run_id=? AND o.sequence<? ORDER BY o.sequence DESC LIMIT 21''', (run_id, sequence)).fetchall()
            values = [dict(row) for row in rows[:20]]
            for value in values:
                value['result_sha256'] = value['sha256']
                value['source_ref'] = _source_ref(json.loads(value.pop('source_json')), run_id=run_id)
                value['result_range'] = {'start': 0, 'end': value['character_count'], 'unit': 'character'}
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
                raise CoreDataError('tool_result_cursor_invalid', '结果读取游标已失效。',
                    {'first_read': {'result_id': result_id, 'cursor': None, 'length': length},
                     'instruction': 'Omit cursor or use null for the first read. Copy returned next_cursor to continue.'}) from error
        text = self._text(metadata)
        end = min(len(text), offset + length)
        return {**_identity(metadata), 'trust': 'untrusted',
                'result_range': {'start': offset, 'end': end, 'unit': 'character'},
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
            begin, end = max(0, offset - 100), min(len(text), offset + len(query) + 100)
            matches.append({'offset': offset, 'content': text[begin:end],
                            'result_range': {'start': begin, 'end': end, 'unit': 'character'},
                            'cursor': self._cursor(metadata, max(0, offset - 100))})
            start = offset + len(query)
        return {**_identity(metadata), 'matches': matches[:limit], 'truncated': len(matches) > limit, 'trust': 'untrusted'}

    def envelope(self, metadata, *, max_characters=4000):
        value = {**_identity(metadata), **{key: metadata[key] for key in ('success', 'complete', 'byte_size')},
                 'trust': 'untrusted', 'read_entry': metadata.get('read_entry', {'result_id': metadata['result_id']})}
        if 'next_read' in metadata:
            value['next_read'] = metadata['next_read']
        if metadata.get('error_code'):
            value['error_code'] = metadata['error_code']
        value['preview'] = ''
        def render():
            return json.dumps(value, ensure_ascii=False, separators=(',', ':'))
        for optional in ('sha256', 'source_ref', 'byte_size', 'source_ref_available'):
            if len(render()) <= max_characters:
                break
            value.pop(optional, None)
            if optional == 'source_ref':
                value['source_ref_available'] = True
        if len(render()) > max_characters:
            error = json.dumps({'error_code': 'tool_result_budget_too_small'}, separators=(',', ':'))
            if len(error) > max_characters:
                raise CoreDataError('tool_result_budget_too_small', '结果预算不足以保留错误信息。')
            return error
        body = self._text(metadata)
        if metadata.get('error_code'):
            body = 'ERROR ' + metadata['error_code'] + ': ' + body
        available = max(0, max_characters - len(render()))
        value['preview'] = body[:available]
        while len(render()) > max_characters and value['preview']:
            value['preview'] = value['preview'][:len(value['preview']) // 2]
        return render()
