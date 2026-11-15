"""Share one local probe and retain its outcome across process restarts."""
import asyncio
import json

from agent.storage import CoreDataError, _utc_now


PROBE_VERSION = 1


class ToolVerification:
    def __init__(self, repository):
        self.repository = repository
        self.pending = {}
        with repository._connect() as connection:
            connection.execute("UPDATE tool_verifications SET status='incomplete',error_json=? WHERE status='probing'",
                (json.dumps({'code': 'core_interrupted', 'message': '工具检测已中断，可以重新检测。'}),))

    def get(self, identity):
        with self.repository._connect() as connection:
            row = connection.execute('SELECT * FROM tool_verifications WHERE identity=?', (identity,)).fetchone()
        if row is None:
            return {'status': 'untested', 'tool_mode': 'text_only', 'verified': False, 'error': None}
        return {'status': row['status'], 'tool_mode': row['mode'] or 'text_only',
                'verified': row['status'] in {'verified', 'unsupported'},
                'error': json.loads(row['error_json']) if row['error_json'] else None,
                'updated_at': row['updated_at']}

    def _save(self, identity, status, mode=None, error=None):
        with self.repository._connect() as connection:
            connection.execute('''INSERT INTO tool_verifications VALUES (?,?,?,?,?)
                ON CONFLICT(identity) DO UPDATE SET status=excluded.status,mode=excluded.mode,
                error_json=excluded.error_json,updated_at=excluded.updated_at''',
                (identity, status, mode, json.dumps(error) if error else None, _utc_now()))

    def for_model(self, digest, template_digest):
        prefix = 'tool-mode:' + digest + ':' + template_digest + ':'
        with self.repository._connect() as connection:
            row = connection.execute('SELECT identity FROM tool_verifications WHERE identity LIKE ? ORDER BY updated_at DESC LIMIT 1',
                                     (prefix + '%',)).fetchone()
        return self.get(row['identity'] if row else prefix)

    async def _run(self, identity, probe):
        self._save(identity, 'probing')
        try:
            result = await probe()
            mode = result['tool_mode']
            if mode not in {'native', 'structured', 'text_only'} or not result.get('verified'):
                raise CoreDataError('tool_probe_incomplete', '工具检测未返回有效结果。')
            self._save(identity, 'unsupported' if mode == 'text_only' else 'verified', mode)
            return self.get(identity)
        except BaseException as error:
            self._save(identity, 'incomplete', error={
                'code': getattr(error, 'code', 'tool_probe_interrupted' if isinstance(error, asyncio.CancelledError)
                                else 'tool_probe_failed'),
                'message': str(error) or '工具检测已中断。'})
            raise

    async def ensure(self, identity, probe, *, force=False):
        shared = self.pending.get(identity)
        if shared is None:
            saved = self.get(identity)
            if not force and saved['status'] in {'verified', 'unsupported'}:
                return saved
            shared = {'task': asyncio.create_task(self._run(identity, probe)), 'waiters': 0}
            self.pending[identity] = shared
        shared['waiters'] += 1
        try:
            return await asyncio.shield(shared['task'])
        finally:
            shared['waiters'] -= 1
            if shared['waiters'] == 0:
                if not shared['task'].done():
                    shared['task'].cancel()
                await asyncio.gather(shared['task'], return_exceptions=True)
                if self.pending.get(identity) is shared:
                    self.pending.pop(identity)
