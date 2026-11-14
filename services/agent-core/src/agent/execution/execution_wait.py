"""Core-only adaptive execution monitoring with durable attempt fencing."""
from __future__ import annotations

import asyncio
import inspect
import json
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from typing import Any, Awaitable, Callable

from agent.storage import CoreDataError, CoreRepository

HEARTBEAT_INTERVAL_SECONDS = 5
HEARTBEAT_LOSS_SECONDS = 30
TERMINAL = frozenset({'completed', 'failed', 'cancelled', 'interrupted'})
current_task_id: ContextVar[str | None] = ContextVar('execution_task_id', default=None)
current_activity: ContextVar[StageActivity | None] = ContextVar('execution_activity', default=None)
current_monitor: ContextVar[ExecutionMonitor | None] = ContextVar('execution_monitor', default=None)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


@dataclass(frozen=True)
class ExecutionWaitPolicy:
    mode: str = 'adaptive'
    long_running_notice_seconds: int = 90
    no_progress_probe_seconds: int = 300
    max_total_seconds: int | None = None
    max_auto_recoveries: int = 2

    def __post_init__(self) -> None:
        if self.mode not in {'adaptive', 'custom'}:
            raise CoreDataError('invalid_execution_wait_policy', 'Unsupported execution wait mode.')
        ranges = {
            'long_running_notice_seconds': (30, 3600),
            'no_progress_probe_seconds': (60, 3600),
            'max_total_seconds': (60, 86400),
            'max_auto_recoveries': (0, 2),
        }
        for name, (minimum, maximum) in ranges.items():
            value = getattr(self, name)
            if name == 'max_total_seconds' and value is None:
                continue
            if type(value) is not int or not minimum <= value <= maximum:
                raise CoreDataError('invalid_execution_wait_policy', f'{name} is outside its supported range.')

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None = None) -> ExecutionWaitPolicy:
        if value is not None and not isinstance(value, dict):
            raise CoreDataError('invalid_execution_wait_policy', 'Execution wait policy must be an object.')
        values = value or {}
        return cls(**{key: values[key] for key in cls.__dataclass_fields__ if key in values})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Diagnostic:
    process_exited: bool = False
    communication_lost: bool = False
    activity_observed: bool | None = None


class StageActivity:
    def __init__(self, monitor: ExecutionMonitor, task_id: str, stage: str,
                 attempt: int, revision: int) -> None:
        self.monitor = monitor
        self.task_id = task_id
        self.stage = stage
        self.attempt = attempt
        self.revision = revision
        self.progress_clock = monitor.active_clock()
        self.progress_sequence = 0
        self.started_clock = self.progress_clock
        self.heartbeat_clock = self.progress_clock
        self.heartbeat_seen = False
        self.resource_clock: float | None = None
        self.queue_clock: float | None = None

    def progress(self, completed: float | None = None, total: float | None = None,
                 **details: Any) -> bool:
        return self.monitor._record(self, 'progress', dict(details, completed=completed, total=total))

    def heartbeat(self, **details: Any) -> bool:
        return self.monitor._record(self, 'heartbeat', details)

    def resource(self, active: bool | None = None, **metrics: Any) -> bool:
        return self.monitor._record(self, 'resource', dict(metrics, active=active))

    def queue(self, waiting: bool = True, **details: Any) -> bool:
        return self.monitor._record(self, 'queue', dict(details, waiting=waiting))

    def checkpoint(self, key: str, identity: Any, payload: Any) -> bool:
        return self.monitor._checkpoint(self, key, identity, payload)

    def load_checkpoint(self, key: str, identity: Any) -> Any | None:
        with self.monitor.repository._connect() as connection:
            row = connection.execute(
                '''SELECT payload_json FROM execution_wait_checkpoints
                   WHERE task_id=? AND stage=? AND checkpoint_key=? AND identity_json=?''',
                (self.task_id, self.stage, key, _json(identity)),
            ).fetchone()
        return json.loads(row[0]) if row is not None else None


class ExecutionMonitor:
    def __init__(self, repository: CoreRepository, *, clock: Callable[[], float] = time.time,
                 active_clock: Callable[[], float] | None = None,
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
                 on_event: Callable[[dict[str, Any]], Any] | None = None) -> None:
        self.repository = repository
        self.clock = clock
        # Tests can inject one clock for both domains. Production elapsed time
        # always uses a monotonic source, independent of persisted wall time.
        self.active_clock = active_clock or (time.monotonic if clock is time.time else clock)
        self.sleep = sleep
        self.on_event = on_event
        self._active: dict[str, StageActivity] = {}
        from agent.migrations.v32_execution_wait import ensure_replay_columns
        with repository._connect() as connection:
            ensure_replay_columns(connection)

    @contextmanager
    def associate(self, task_id: str):
        token = current_task_id.set(task_id)
        monitor_token = current_monitor.set(self)
        try:
            yield
        finally:
            current_task_id.reset(token)
            current_monitor.reset(monitor_token)

    @staticmethod
    def _decode(row) -> dict[str, Any]:
        if row is None:
            raise CoreDataError('execution_task_not_found', 'The execution task does not exist.')
        result = dict(row)
        result.pop('replay_json', None)
        result['policy'] = json.loads(result.pop('policy_json'))
        return result

    def set_replay(self, task_id: str, kind: str, request: dict[str, Any]) -> None:
        if not isinstance(kind, str) or not kind or not isinstance(request, dict):
            raise CoreDataError('invalid_execution_replay', 'Execution replay metadata is invalid.')
        encoded = _json(request)
        with self.repository._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT replay_kind,replay_json FROM execution_wait_tasks WHERE id=?',
                                     (task_id,)).fetchone()
            if row is None:
                raise CoreDataError('execution_task_not_found', 'The execution task does not exist.')
            if row['replay_kind'] is not None:
                saved = json.loads(row['replay_json'])
                if row['replay_kind'] != kind or _json(saved['request']) != encoded:
                    raise CoreDataError('execution_replay_conflict', 'This execution identity belongs to a different request.')
                return
            connection.execute('UPDATE execution_wait_tasks SET replay_kind=?,replay_json=? WHERE id=?',
                               (kind, _json({'request': request}), task_id))

    def get_replay(self, task_id: str) -> dict[str, Any] | None:
        with self.repository._connect() as connection:
            row = connection.execute('SELECT replay_kind,replay_json FROM execution_wait_tasks WHERE id=?',
                                     (task_id,)).fetchone()
        if row is None:
            raise CoreDataError('execution_task_not_found', 'The execution task does not exist.')
        if row['replay_kind'] is None:
            return None
        return {'kind': row['replay_kind'], **json.loads(row['replay_json'])}

    def get_task(self, task_id: str) -> dict[str, Any]:
        with self.repository._connect() as connection:
            task = self._decode(connection.execute(
                'SELECT * FROM execution_wait_tasks WHERE id=?', (task_id,)).fetchone())
            stages = [dict(row) for row in connection.execute(
                'SELECT * FROM execution_wait_stages WHERE task_id=? ORDER BY attempt', (task_id,))]
        for stage in stages:
            stage['details'] = json.loads(stage.pop('details_json'))
        activity = self._active.get(task_id)
        if activity is not None and stages and stages[-1]['attempt'] == activity.attempt:
            if stages[-1]['status'] not in TERMINAL:
                stages[-1]['elapsed_seconds'] = max(0, self.active_clock() - activity.started_clock)
            if activity.queue_clock is not None:
                stages[-1]['queue_wait_seconds'] += max(0, self.active_clock() - activity.queue_clock)
        task['stages'] = stages
        task['current_stage'] = stages[-1] if stages else None
        task['progress'] = {key: stages[-1][key] for key in
                            ('completed', 'total', 'progress_at', 'heartbeat_at', 'resource_at',
                             'resource_active', 'queue_at', 'queue_waiting', 'queue_wait_seconds', 'details')} if stages else None
        task['elapsed_seconds'] = sum(stage['elapsed_seconds'] for stage in stages)
        task['seconds_since_progress'] = max(0, self.active_clock() - activity.progress_clock) \
            if activity is not None else max(0, self.clock() - stages[-1]['progress_at']) if stages else 0
        task['capabilities'] = {
            'continue': self._can_continue(task, task['current_stage']),
            'retry': task['status'] not in {'completed', 'cancelled'},
            'cancel': task['status'] not in TERMINAL,
        }
        task.update({f'can_{key}': value for key, value in task['capabilities'].items()})
        return task

    @staticmethod
    def _can_continue(task, stage) -> bool:
        return task['status'] not in TERMINAL \
            and task['error_code'] != 'execution_recovery_required' \
            and task['command'] != 'retry' \
            and stage is not None and stage['status'] in {'running', 'waiting_recovery'}

    def start_task(self, *, intent_task_id: str | None = None, run_id: str | None = None,
                   project_id: str | None = None, policy: ExecutionWaitPolicy | dict | None = None,
                   task_id: str | None = None) -> dict[str, Any]:
        if not intent_task_id and not run_id:
            raise CoreDataError('execution_identity_required', 'An intent or run identity is required.')
        policy = policy if isinstance(policy, ExecutionWaitPolicy) else ExecutionWaitPolicy.from_dict(policy)
        now = self.clock()
        task_id = task_id or str(uuid.uuid4())
        with self.repository._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            existing = connection.execute(
                '''SELECT * FROM execution_wait_tasks
                   WHERE id=? OR (intent_task_id IS NOT NULL AND intent_task_id=?)
                              OR (run_id IS NOT NULL AND run_id=?)''',
                (task_id, intent_task_id, run_id)).fetchone()
            if existing is not None:
                return self._decode(existing)
            connection.execute(
                '''INSERT INTO execution_wait_tasks
                   (id,intent_task_id,run_id,project_id,status,policy_json,created_at,updated_at)
                   VALUES (?,?,?,?,'running',?,?,?)''',
                (task_id, intent_task_id, run_id, project_id, _json(policy.to_dict()), now, now))
        return self.get_task(task_id)

    def bind_run(self, task_id: str, run_id: str) -> dict[str, Any]:
        with self.repository._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            task = self._decode(connection.execute(
                'SELECT * FROM execution_wait_tasks WHERE id=?', (task_id,)).fetchone())
            if task['run_id'] not in (None, run_id):
                raise CoreDataError('execution_identity_conflict', 'The task already belongs to a different run.')
            connection.execute('UPDATE execution_wait_tasks SET run_id=?,updated_at=? WHERE id=?',
                               (run_id, self.clock(), task_id))
        return self.get_task(task_id)

    def control(self, task_id: str, action: str, *, revision: int,
                idempotency_key: str) -> dict[str, Any]:
        if action not in {'status', 'continue', 'retry', 'cancel'} or not idempotency_key:
            raise CoreDataError('invalid_execution_control', 'The execution control is invalid.')
        with self.repository._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            previous = connection.execute(
                'SELECT action,response_json FROM execution_wait_controls WHERE task_id=? AND idempotency_key=?',
                (task_id, idempotency_key)).fetchone()
            if previous is not None:
                if previous['action'] != action:
                    raise CoreDataError('execution_idempotency_conflict', 'The control identity was already used.')
                return json.loads(previous['response_json'])
            task = self._decode(connection.execute(
                'SELECT * FROM execution_wait_tasks WHERE id=?', (task_id,)).fetchone())
            if task['revision'] != revision:
                raise CoreDataError('execution_revision_conflict', 'The task revision has changed.',
                                    {'revision': task['revision']})
            if action == 'continue' and task['status'] not in TERMINAL:
                stage = connection.execute(
                    'SELECT status FROM execution_wait_stages WHERE task_id=? AND attempt=?',
                    (task_id, task['attempt'])).fetchone()
                if not self._can_continue(task, stage):
                    raise CoreDataError('execution_continue_unavailable',
                                        'The execution request requires retry before waiting can continue.')
            may_resume = action == 'retry' and task['status'] in {'failed', 'interrupted'}
            if action != 'status' and (task['status'] not in TERMINAL or may_resume):
                status = 'cancelled' if action == 'cancel' else 'running'
                connection.execute(
                    '''UPDATE execution_wait_tasks SET revision=revision+1,sequence=sequence+1,status=?,command=?,
                       updated_at=?,finished_at=? WHERE id=?''',
                    (status, action, self.clock(), self.clock() if action == 'cancel' else None, task_id))
                task = self._decode(connection.execute(
                    'SELECT * FROM execution_wait_tasks WHERE id=?', (task_id,)).fetchone())
            connection.execute('INSERT INTO execution_wait_controls VALUES (?,?,?,?)',
                               (task_id, idempotency_key, action, _json(task)))
        activity = self._active.get(task_id)
        if activity is not None and action == 'continue':
            activity.revision = task['revision']
        if action != 'status':
            self._emit(task_id, f'control_{action}')
        return task

    def finish_task(self, task_id: str, status: str = 'completed', *,
                    error_code: str | None = None, replay_outcome: dict | None = None) -> dict[str, Any]:
        if status not in TERMINAL:
            raise CoreDataError('invalid_execution_status', 'A terminal execution status is required.')
        with self.repository._connect() as connection:
            if replay_outcome is not None:
                connection.execute('''UPDATE execution_wait_tasks
                    SET replay_json=json_set(replay_json,'$.outcome',json(?))
                    WHERE id=? AND status IN ('running','waiting_recovery') AND replay_kind IS NOT NULL''',
                    (_json(replay_outcome), task_id))
            cursor = connection.execute(
                '''UPDATE execution_wait_tasks SET status=?,error_code=?,finished_at=?,updated_at=?,
                   revision=revision+1,sequence=sequence+1 WHERE id=? AND status IN ('running','waiting_recovery')''',
                (status, error_code, self.clock(), self.clock(), task_id))
        if cursor.rowcount:
            self._emit(task_id, 'terminal')
        return self.get_task(task_id)

    def interrupt_active(self) -> int:
        now = self.clock()
        with self.repository._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute(
                '''UPDATE execution_wait_stages SET status='interrupted',finished_at=?
                   WHERE status IN ('running','waiting_recovery')''', (now,))
            cursor = connection.execute(
                '''UPDATE execution_wait_tasks SET status='interrupted',error_code='core_interrupted',
                   revision=revision+1,sequence=sequence+1,finished_at=?,updated_at=?
                   WHERE status IN ('running','waiting_recovery')''', (now, now))
            return cursor.rowcount

    def cleanup(self, *, retention_days: int = 30) -> int:
        with self.repository._connect() as connection:
            return connection.execute(
                'DELETE FROM execution_wait_tasks WHERE finished_at<?',
                (self.clock() - retention_days * 86400,)).rowcount

    def delete_task(self, task_id: str) -> None:
        with self.repository._connect() as connection:
            connection.execute('DELETE FROM execution_wait_tasks WHERE id=?', (task_id,))

    def invalidate_sources(self, *, library_id: str | None = None,
                           version_id: str | None = None, index_id: str | None = None) -> int:
        """Fence source-dependent roots and remove their affected checkpoints.

        Scope registration precedes inference, so activity can identify a root
        even before its first checkpoint. Business history and checkpoints for
        other sources survive. The active stage stays owned until its runner
        drains the prior request, preventing overlapping replacement work.
        """
        selectors = {'library_ids': library_id, 'version_ids': version_id, 'index_ids': index_id}
        if not any(value is not None for value in selectors.values()) or any(
                value is not None and (not isinstance(value, str) or not value)
                for value in selectors.values()):
            raise CoreDataError('execution_source_required', '请选择需要失效的资料身份。')
        checkpoint_clauses, activity_clauses, parameters = [], [], []
        for key, value in selectors.items():
            if value is None:
                continue
            checkpoint_clauses.append(
                f"EXISTS (SELECT 1 FROM json_each(identity_json,'$.sources.{key}') WHERE value=?)")
            activity_clauses.append(
                f"EXISTS (SELECT 1 FROM json_each(payload_json,'$.sources.{key}') WHERE value=?)")
            parameters.append(value)
        checkpoint_filter = ' OR '.join(checkpoint_clauses)
        activity_filter = ' OR '.join(activity_clauses)
        now = self.clock()
        with self.repository._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            affected = [row[0] for row in connection.execute(
                f'''SELECT task_id FROM execution_wait_checkpoints WHERE {checkpoint_filter}
                    UNION SELECT task_id FROM execution_wait_activity
                    WHERE kind='progress' AND ({activity_filter})''', (*parameters, *parameters))]
            connection.execute(f'DELETE FROM execution_wait_checkpoints WHERE {checkpoint_filter}', parameters)
            for task_id in affected:
                connection.execute("UPDATE execution_wait_tasks SET replay_json=json_remove(replay_json,'$.outcome') WHERE id=?",
                                   (task_id,))
                connection.execute(
                    '''UPDATE execution_wait_tasks SET status='interrupted',error_code='rag_evidence_stale',
                       revision=revision+1,sequence=sequence+1,finished_at=?,updated_at=?
                       WHERE id=? AND status IN ('running','waiting_recovery')''', (now, now, task_id))
        for task_id in affected:
            self._emit(task_id, 'sources_invalidated')
        return len(affected)

    @staticmethod
    def _valid(connection, activity: StageActivity) -> bool:
        return connection.execute(
            '''SELECT 1 FROM execution_wait_tasks t JOIN execution_wait_stages s
               ON s.task_id=t.id AND s.attempt=t.attempt
               WHERE t.id=? AND t.attempt=? AND t.revision=?
                 AND t.status IN ('running','waiting_recovery')
                 AND s.status IN ('running','waiting_recovery')''',
            (activity.task_id, activity.attempt, activity.revision)).fetchone() is not None

    def _record(self, activity: StageActivity, kind: str, payload: dict) -> bool:
        encoded = _json(payload)
        now = self.clock()
        with self.repository._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            if not self._valid(connection, activity):
                return False
            metrics = connection.execute(
                'SELECT details_json FROM execution_wait_stages WHERE task_id=? AND attempt=?',
                (activity.task_id, activity.attempt)).fetchone()
            details = json.loads(metrics['details_json'])
            details.update(payload)
            connection.execute(
                '''INSERT INTO execution_wait_activity
                   (task_id,attempt,revision,kind,recorded_at,payload_json) VALUES (?,?,?,?,?,?)''',
                (activity.task_id, activity.attempt, activity.revision, kind, now, encoded))
            assignments = {
                'progress': ('progress_at=?,completed=?,total=?', (now, payload.get('completed'), payload.get('total'))),
                'heartbeat': ('heartbeat_at=?', (now,)),
                'resource': ('resource_at=?,resource_active=?', (now, payload.get('active'))),
                'queue': ('queue_at=?,queue_waiting=?', (now, payload.get('waiting'))),
            }
            columns, values = assignments[kind]
            connection.execute(f'UPDATE execution_wait_stages SET {columns},details_json=? WHERE task_id=? AND attempt=?',
                               (*values, _json(details), activity.task_id, activity.attempt))
            if kind == 'queue' and activity.queue_clock is not None:
                connection.execute(
                    'UPDATE execution_wait_stages SET queue_wait_seconds=queue_wait_seconds+? WHERE task_id=? AND attempt=?',
                    (max(0, self.active_clock() - activity.queue_clock), activity.task_id, activity.attempt))
            connection.execute('UPDATE execution_wait_tasks SET sequence=sequence+1,updated_at=? WHERE id=?',
                               (now, activity.task_id))
        if kind == 'progress':
            activity.progress_clock = self.active_clock()
            activity.progress_sequence += 1
        elif kind == 'heartbeat':
            activity.heartbeat_clock = self.active_clock()
            activity.heartbeat_seen = True
        elif kind == 'resource':
            activity.resource_clock = self.active_clock()
        elif kind == 'queue':
            activity.queue_clock = self.active_clock() if payload['waiting'] else None
        self._emit(activity.task_id, kind)
        return True

    def _checkpoint(self, activity: StageActivity, key: str, identity: Any, payload: Any) -> bool:
        identity_json, payload_json = _json(identity), _json(payload)
        now = self.clock()
        with self.repository._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            if not self._valid(connection, activity):
                return False
            connection.execute(
                '''INSERT INTO execution_wait_checkpoints VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(task_id,stage,checkpoint_key) DO UPDATE SET
                   identity_json=excluded.identity_json,payload_json=excluded.payload_json,
                   attempt=excluded.attempt,revision=excluded.revision,committed_at=excluded.committed_at''',
                (activity.task_id, activity.stage, key, identity_json, payload_json,
                 activity.attempt, activity.revision, now))
            connection.execute('UPDATE execution_wait_tasks SET auto_recoveries=0,sequence=sequence+1,updated_at=? WHERE id=?',
                               (now, activity.task_id))
            connection.execute('UPDATE execution_wait_stages SET progress_at=? WHERE task_id=? AND attempt=?',
                               (now, activity.task_id, activity.attempt))
        activity.progress_clock = self.active_clock()
        activity.progress_sequence += 1
        self._emit(activity.task_id, 'checkpoint')
        return True

    def _emit(self, task_id: str, kind: str) -> None:
        if self.on_event is not None:
            with self.repository._connect() as connection:
                connection.execute('UPDATE execution_wait_tasks SET sequence=sequence+1 WHERE id=?', (task_id,))
            self.on_event(dict(self.get_task(task_id), event=kind))

    def _begin(self, task_id: str, stage: str) -> StageActivity:
        now = self.clock()
        with self.repository._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            task = self._decode(connection.execute(
                'SELECT * FROM execution_wait_tasks WHERE id=?', (task_id,)).fetchone())
            if task['status'] in TERMINAL:
                raise CoreDataError('execution_task_terminal', 'The execution task is already terminal.')
            active = connection.execute(
                "SELECT 1 FROM execution_wait_stages WHERE task_id=? AND status IN ('running','waiting_recovery')",
                (task_id,)).fetchone()
            if active:
                raise CoreDataError('execution_stage_active', 'The prior execution stage is still active.')
            attempt = task['attempt'] + 1
            connection.execute(
                '''UPDATE execution_wait_tasks SET attempt=?,stage=?,status='running',command=NULL,sequence=sequence+1,
                   updated_at=?,auto_recoveries=CASE WHEN stage=? THEN auto_recoveries ELSE 0 END WHERE id=?''',
                (attempt, stage, now, stage, task_id))
            connection.execute(
                '''INSERT INTO execution_wait_stages
                   (task_id,attempt,stage,revision,status,started_at,progress_at,heartbeat_at)
                   VALUES (?,?,?,?,'running',?,?,?)''',
                (task_id, attempt, stage, task['revision'], now, now, now))
        activity = StageActivity(self, task_id, stage, attempt, task['revision'])
        self._active[task_id] = activity
        self._emit(task_id, 'stage_started')
        return activity

    def _stage_status(self, activity: StageActivity, status: str) -> None:
        with self.repository._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            queue_elapsed = max(0, self.active_clock() - activity.queue_clock) \
                if activity.queue_clock is not None and status in TERMINAL else 0
            connection.execute(
                '''UPDATE execution_wait_stages SET status=?,finished_at=?,elapsed_seconds=?,
                   queue_wait_seconds=queue_wait_seconds+?
                   WHERE task_id=? AND attempt=? AND status IN ('running','waiting_recovery')''',
                (status, self.clock() if status in TERMINAL else None,
                 max(0, self.active_clock() - activity.started_clock), queue_elapsed, activity.task_id, activity.attempt))
            connection.execute('UPDATE execution_wait_tasks SET sequence=sequence+1 WHERE id=?', (activity.task_id,))
        if status in TERMINAL:
            activity.queue_clock = None

    def _pause(self, activity: StageActivity, reason: str) -> None:
        with self.repository._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute(
                '''UPDATE execution_wait_tasks SET status='waiting_recovery',error_code=?,updated_at=?,sequence=sequence+1
                   WHERE id=? AND status='running' AND attempt=?''',
                (reason, self.clock(), activity.task_id, activity.attempt))
            connection.execute(
                "UPDATE execution_wait_stages SET status='waiting_recovery' WHERE task_id=? AND attempt=? AND status='running'",
                (activity.task_id, activity.attempt))
        self._emit(activity.task_id, 'waiting_recovery')

    async def _invoke(self, activity: StageActivity, operation_factory):
        token = current_activity.set(activity)
        task_token = current_task_id.set(activity.task_id)
        monitor_token = current_monitor.set(self)
        try:
            return await operation_factory(activity)
        finally:
            current_activity.reset(token)
            current_task_id.reset(task_token)
            current_monitor.reset(monitor_token)

    @staticmethod
    async def _drain(operation: asyncio.Task) -> None:
        if not operation.done():
            operation.cancel()
        try:
            await operation
        except BaseException:
            pass

    async def execute_stage(self, task_id: str, stage: str, operation_factory, *,
                            safe_to_retry: bool = False, diagnose=None, recoverable=None):
        activity = self._begin(task_id, stage)
        policy = ExecutionWaitPolicy.from_dict(self.get_task(task_id)['policy'])
        operation = asyncio.create_task(self._invoke(activity, operation_factory))
        diagnosed_at: float | None = None
        first_lost_at: float | None = None
        first_lost_progress_sequence = 0
        continued_at = self.active_clock()
        total_deadline = policy.max_total_seconds
        previous_tick = continued_at
        notice_sent = False
        failure: BaseException | None = None
        try:
            while True:
                task = self.get_task(task_id)
                if task['status'] in TERMINAL:
                    await self._drain(operation)
                    raise asyncio.CancelledError()
                command = task['command']
                if command is not None:
                    with self.repository._connect() as connection:
                        connection.execute('UPDATE execution_wait_tasks SET command=NULL WHERE id=? AND revision=?',
                                           (task_id, task['revision']))
                    if command == 'retry':
                        self._stage_status(activity, 'interrupted')
                        await self._drain(operation)
                        activity = self._begin(task_id, stage)
                        operation = asyncio.create_task(self._invoke(activity, operation_factory))
                        failure = None
                    if command in {'retry', 'continue'}:
                        activity.revision = task['revision']
                        continued_at = self.active_clock()
                        if policy.max_total_seconds is not None:
                            total_deadline = task['elapsed_seconds'] + policy.max_total_seconds
                        diagnosed_at = first_lost_at = None
                        notice_sent = False
                        self._stage_status(activity, 'running')
                confirmed_failure = False
                if operation.done() and failure is None:
                    try:
                        result = operation.result()
                    except asyncio.CancelledError:
                        self._stage_status(activity, 'cancelled')
                        raise
                    except Exception as error:
                        if not safe_to_retry or recoverable is None or not recoverable(error):
                            self._stage_status(activity, 'failed')
                            raise
                        failure = error
                        confirmed_failure = True
                    else:
                        if self.get_task(task_id)['status'] in TERMINAL:
                            raise asyncio.CancelledError()
                        self._stage_status(activity, 'completed')
                        with self.repository._connect() as connection:
                            connection.execute(
                                '''UPDATE execution_wait_tasks SET auto_recoveries=0,status='running',error_code=NULL
                                   WHERE id=? AND status IN ('running','waiting_recovery') AND attempt=?''',
                                (task_id, activity.attempt))
                        self._emit(task_id, 'stage_completed')
                        return result
                now = self.active_clock()
                if now - previous_tick > HEARTBEAT_LOSS_SECONDS:
                    # A suspended event loop supplies no failure evidence.
                    # Recheck after a full observation window following wake.
                    continued_at = now
                    diagnosed_at = first_lost_at = None
                previous_tick = now
                if not notice_sent and now - continued_at >= policy.long_running_notice_seconds:
                    self._emit(task_id, 'long_running')
                    notice_sent = True
                if task['status'] == 'running' and failure is None:
                    with self.repository._connect() as connection:
                        metrics = connection.execute('SELECT * FROM execution_wait_stages WHERE task_id=? AND attempt=?',
                                                     (task_id, activity.attempt)).fetchone()
                    progress_at = max(activity.progress_clock, continued_at)
                    active_resource = metrics['resource_active'] == 1 and metrics['resource_at'] is not None \
                        and activity.resource_clock is not None and now - activity.resource_clock < HEARTBEAT_LOSS_SECONDS
                    if first_lost_at is not None and (
                            activity.progress_sequence != first_lost_progress_sequence or active_resource):
                        first_lost_at = None
                    stalled = now - progress_at >= policy.no_progress_probe_seconds
                    heartbeat_lost = activity.heartbeat_seen and \
                        now - max(activity.heartbeat_clock, continued_at, progress_at) >= HEARTBEAT_LOSS_SECONDS
                    total_reached = policy.mode == 'custom' and total_deadline is not None \
                        and task['elapsed_seconds'] >= total_deadline
                    if total_reached:
                        self._pause(activity, 'execution_total_wait_reached')
                    should_diagnose = (stalled or heartbeat_lost) and not active_resource and not total_reached
                    if should_diagnose and (diagnosed_at is None or now - diagnosed_at >= HEARTBEAT_LOSS_SECONDS):
                        diagnosed_at = now
                        try:
                            diagnostic = diagnose(activity) if diagnose is not None else Diagnostic()
                            if inspect.isawaitable(diagnostic):
                                diagnostic = await diagnostic
                        except Exception:
                            self._pause(activity, 'execution_diagnostic_unavailable')
                            diagnostic = Diagnostic()
                        observed_status = self.get_task(task_id)['status']
                        if observed_status in TERMINAL:
                            raise asyncio.CancelledError()
                        if observed_status == 'waiting_recovery':
                            continue
                        if diagnostic.process_exited:
                            confirmed_failure = True
                        elif diagnostic.communication_lost and diagnostic.activity_observed is False:
                            if first_lost_at is None:
                                first_lost_at = now
                                first_lost_progress_sequence = activity.progress_sequence
                            elif now - first_lost_at >= HEARTBEAT_LOSS_SECONDS:
                                confirmed_failure = True
                        elif diagnostic.activity_observed is True:
                            first_lost_at = None
                        else:
                            first_lost_at = None
                            self._pause(activity, 'execution_activity_uncertain')
                if confirmed_failure:
                    task = self.get_task(task_id)
                    if safe_to_retry and task['auto_recoveries'] < policy.max_auto_recoveries:
                        self._stage_status(activity, 'failed')
                        await self._drain(operation)
                        with self.repository._connect() as connection:
                            connection.execute('UPDATE execution_wait_tasks SET auto_recoveries=auto_recoveries+1 WHERE id=?',
                                               (task_id,))
                        activity = self._begin(task_id, stage)
                        operation = asyncio.create_task(self._invoke(activity, operation_factory))
                        failure = None
                        first_lost_at = diagnosed_at = None
                        continued_at = self.active_clock()
                        self._emit(task_id, 'recovery')
                    else:
                        self._pause(activity, 'execution_recovery_required')
                sleeper = asyncio.create_task(self.sleep(HEARTBEAT_INTERVAL_SECONDS))
                try:
                    # Completed failed operations must wait for a control without spinning.
                    if operation.done():
                        await sleeper
                    else:
                        await asyncio.wait({operation, sleeper}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    if not sleeper.done():
                        sleeper.cancel()
                    await self._drain(sleeper)
        except asyncio.CancelledError:
            await self._drain(operation)
            self._stage_status(activity, 'cancelled')
            self.finish_task(task_id, 'cancelled')
            raise
        finally:
            await self._drain(operation)
            if self._active.get(task_id) is activity:
                self._active.pop(task_id, None)
