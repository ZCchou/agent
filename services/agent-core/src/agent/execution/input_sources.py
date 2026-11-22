"""Immutable run-scoped user input with bounded model views and paged originals."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
from pathlib import Path
from uuid import uuid4

from agent.context_engine.policy import estimate_tokens
from agent.storage import CoreDataError, _open_database

_LOCK = threading.RLock()


def input_tokens(text):
    return max(estimate_tokens(text), (len(text.encode('utf-8')) + 2) // 3)


def input_descriptor(content):
    return {'result_id': 'user-input', 'sha256': hashlib.sha256(content.encode('utf-8')).hexdigest(),
            'character_count': len(content), 'byte_size': len(content.encode('utf-8')),
            'coverage_required': 'entire_source_before_full_task_completion',
            'instruction': 'Read the complete original user input with tools.read_result result_id=user-input before completing the full task. For a partial task, read and identify every range needed for that scope. Preview text may omit requirements.'}


def project_input(content, budget_tokens):
    """Preserve ordinary input exactly; describe long input without losing its identity."""
    if input_tokens(content) <= budget_tokens:
        return content
    descriptor = input_descriptor(content)
    retained = min(len(content), max(0, budget_tokens * 2))
    while True:
        half = retained // 2
        descriptor['preview'] = (content[:half] + '\n[original input available through tools.read_result]\n'
                                 + content[-(retained - half):]) if retained else ''
        rendered = json.dumps(descriptor, ensure_ascii=False, separators=(',', ':'))
        if input_tokens(rendered) <= budget_tokens:
            return rendered
        if not retained:
            raise CoreDataError('user_input_context_too_small', '当前上下文不足以保留原始输入的回读标识。')
        retained //= 2


class UserInputSources:
    def __init__(self, database_path):
        self.database_path = Path(database_path)
        self.root = self.database_path.parent / 'user-input-sources'

    def _manifest(self, run_id):
        identity = hashlib.sha256(run_id.encode('utf-8')).hexdigest()
        return self.root / 'runs' / (identity + '.json')

    @staticmethod
    def _write(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.' + uuid4().hex + '.tmp')
        try:
            with temporary.open('xb') as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _original(self, run_id):
        with _open_database(self.database_path) as db:
            row = db.execute('SELECT m.content FROM model_runs r JOIN messages m ON m.id=r.user_message_id WHERE r.id=?',
                             (run_id,)).fetchone()
        if row is None:
            raise CoreDataError('user_input_forbidden', '原始输入不属于当前任务或已被删除。')
        return row['content']

    def _metadata(self, run_id):
        self._original(run_id)
        try:
            metadata = json.loads(self._manifest(run_id).read_text(encoding='utf-8'))
            digest = metadata['sha256']
            if metadata['run_id'] != run_id or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
                raise ValueError()
            return metadata
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise CoreDataError('user_input_unavailable', '已保存的原始输入不可用。') from error

    def save(self, run_id, content=None):
        """Capture the canonical user message once and preserve it across retries and edits."""
        with _LOCK:
            original = self._original(run_id)
            if self._manifest(run_id).exists():
                metadata = self._metadata(run_id)
                if content is not None and hashlib.sha256(content.encode('utf-8')).hexdigest() != metadata['sha256']:
                    raise CoreDataError('user_input_conflict', '已保存的原始输入不可替换。')
                return metadata
            if content is not None and content != original:
                raise CoreDataError('user_input_conflict', '原始输入须与当前任务的用户消息一致。')
            metadata = {**input_descriptor(original), 'run_id': run_id}
            self._write(self.root / 'objects' / (metadata['sha256'] + '.txt'), original.encode('utf-8'))
            self._write(self._manifest(run_id), json.dumps(metadata, ensure_ascii=False).encode('utf-8'))
            return metadata

    def _text(self, metadata):
        try:
            data = (self.root / 'objects' / (metadata['sha256'] + '.txt')).read_bytes()
            if len(data) != metadata['byte_size'] or hashlib.sha256(data).hexdigest() != metadata['sha256']:
                raise ValueError()
            return data.decode('utf-8')
        except (OSError, UnicodeError, ValueError) as error:
            raise CoreDataError('user_input_corrupt', '已保存的原始输入校验失败。') from error

    @staticmethod
    def _cursor(metadata, offset):
        return base64.urlsafe_b64encode(json.dumps([metadata['run_id'], metadata['sha256'], offset]).encode()).decode()

    def read(self, run_id, *, cursor=None, length=4000):
        if type(length) is not int or not 1 <= length <= 16000:
            raise CoreDataError('invalid_tool_call', '原始输入读取长度无效。')
        metadata = self._metadata(run_id)
        offset = 0
        if cursor is not None:
            try:
                identity, digest, offset = json.loads(base64.urlsafe_b64decode(cursor))
                if identity != run_id or digest != metadata['sha256'] or type(offset) is not int or not 0 <= offset <= metadata['character_count']:
                    raise ValueError()
            except (ValueError, TypeError, UnicodeError) as error:
                raise CoreDataError('user_input_cursor_invalid', '原始输入读取游标无效。',
                    {'first_read': {'result_id': 'user-input', 'cursor': None},
                     'next_read': 'Use only the next_cursor returned by this task.'}) from error
        text = self._text(metadata)
        end = min(len(text), offset + length)
        return {'result_id': 'user-input', 'sha256': metadata['sha256'], 'result_sha256': metadata['sha256'],
                'source_ref': {'kind': 'original_user_message', 'run_id': run_id}, 'source': 'original_user_message',
                'offset': offset, 'content': text[offset:end], 'total': len(text),
                'next_cursor': self._cursor(metadata, end) if end < len(text) else None}

    def search(self, run_id, query, *, limit=10):
        if not isinstance(query, str) or not query or len(query) > 1000 or type(limit) is not int or not 1 <= limit <= 50:
            raise CoreDataError('invalid_tool_call', '原始输入搜索参数无效。')
        metadata = self._metadata(run_id)
        text = self._text(metadata)
        matches, start = [], 0
        while len(matches) <= limit:
            offset = text.find(query, start)
            if offset < 0:
                break
            matches.append({'offset': offset, 'content': text[max(0, offset - 100):offset + len(query) + 100],
                            'cursor': self._cursor(metadata, max(0, offset - 100))})
            start = offset + len(query)
        return {'result_id': 'user-input', 'result_sha256': metadata['sha256'],
                'source_ref': {'kind': 'original_user_message', 'run_id': run_id},
                'matches': matches[:limit], 'truncated': len(matches) > limit,
                'coverage_required': metadata['coverage_required']}
