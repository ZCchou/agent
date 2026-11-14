"""Durable execution activity, recovery controls and validated checkpoints.

The storage migrator runs this in its backed-up migration transaction with
foreign keys disabled, as for earlier model_runs table rebuilds.
"""
import re
import sqlite3


def ensure_replay_columns(connection: sqlite3.Connection) -> None:
    columns = {row[1] for row in connection.execute('PRAGMA table_info(execution_wait_tasks)')}
    if not columns:
        return
    for column in ('replay_kind', 'replay_json'):
        if column not in columns:
            connection.execute(f'ALTER TABLE execution_wait_tasks ADD COLUMN {column} TEXT')


def migrate(connection: sqlite3.Connection) -> None:
    row = connection.execute("SELECT sql FROM sqlite_master WHERE name='model_runs'").fetchone()
    if row is not None and "'waiting_recovery'" not in row[0]:
        indexes = [item[0] for item in connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='model_runs' AND sql IS NOT NULL")]
        expanded = re.sub(r'CREATE TABLE ["`\[]?model_runs["`\]]?',
                          'CREATE TABLE model_runs_v32', row[0], count=1)
        expanded = expanded.replace("'waiting_tool'", "'waiting_tool','waiting_recovery'", 1)
        connection.execute(expanded)
        connection.execute('INSERT INTO model_runs_v32 SELECT * FROM model_runs')
        connection.execute('DROP TABLE model_runs')
        connection.execute('ALTER TABLE model_runs_v32 RENAME TO model_runs')
        for index in indexes:
            connection.execute(index)
    statements = [
        '''CREATE TABLE IF NOT EXISTS execution_wait_tasks (
            id TEXT PRIMARY KEY, intent_task_id TEXT, run_id TEXT, project_id TEXT,
            revision INTEGER NOT NULL DEFAULT 0, attempt INTEGER NOT NULL DEFAULT 0,
            sequence INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL CHECK(status IN
                ('running','waiting_recovery','completed','failed','cancelled','interrupted')),
            stage TEXT, policy_json TEXT NOT NULL, auto_recoveries INTEGER NOT NULL DEFAULT 0,
            command TEXT, error_code TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
            finished_at REAL, replay_kind TEXT, replay_json TEXT)''',
        '''CREATE UNIQUE INDEX IF NOT EXISTS execution_wait_run
            ON execution_wait_tasks(run_id) WHERE run_id IS NOT NULL''',
        '''CREATE UNIQUE INDEX IF NOT EXISTS execution_wait_intent
            ON execution_wait_tasks(intent_task_id) WHERE intent_task_id IS NOT NULL''',
        '''CREATE TABLE IF NOT EXISTS execution_wait_stages (
            task_id TEXT NOT NULL REFERENCES execution_wait_tasks(id) ON DELETE CASCADE,
            attempt INTEGER NOT NULL, stage TEXT NOT NULL, revision INTEGER NOT NULL,
            status TEXT NOT NULL, started_at REAL NOT NULL, finished_at REAL,
            progress_at REAL NOT NULL, heartbeat_at REAL NOT NULL,
            resource_at REAL, queue_at REAL, resource_active INTEGER, queue_waiting INTEGER,
            queue_wait_seconds REAL NOT NULL DEFAULT 0, elapsed_seconds REAL NOT NULL DEFAULT 0,
            completed REAL, total REAL, details_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY(task_id,attempt))''',
        '''CREATE TABLE IF NOT EXISTS execution_wait_activity (
            id INTEGER PRIMARY KEY, task_id TEXT NOT NULL REFERENCES execution_wait_tasks(id) ON DELETE CASCADE,
            attempt INTEGER NOT NULL, revision INTEGER NOT NULL, kind TEXT NOT NULL,
            recorded_at REAL NOT NULL, payload_json TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS execution_wait_checkpoints (
            task_id TEXT NOT NULL REFERENCES execution_wait_tasks(id) ON DELETE CASCADE,
            stage TEXT NOT NULL, checkpoint_key TEXT NOT NULL,
            identity_json TEXT NOT NULL, payload_json TEXT NOT NULL,
            attempt INTEGER NOT NULL, revision INTEGER NOT NULL, committed_at REAL NOT NULL,
            PRIMARY KEY(task_id,stage,checkpoint_key))''',
        '''CREATE TABLE IF NOT EXISTS execution_wait_controls (
            task_id TEXT NOT NULL REFERENCES execution_wait_tasks(id) ON DELETE CASCADE,
            idempotency_key TEXT NOT NULL, action TEXT NOT NULL,
            response_json TEXT NOT NULL, PRIMARY KEY(task_id,idempotency_key))''',
        '''CREATE INDEX IF NOT EXISTS execution_wait_activity_task
            ON execution_wait_activity(task_id,attempt,id)''',
    ]
    for statement in statements:
        connection.execute(statement)
    columns = {row[1] for row in connection.execute('PRAGMA table_info(execution_wait_tasks)')}
    if 'sequence' not in columns:
        connection.execute('ALTER TABLE execution_wait_tasks ADD COLUMN sequence INTEGER NOT NULL DEFAULT 0')
    ensure_replay_columns(connection)
    stage_columns = {row[1] for row in connection.execute('PRAGMA table_info(execution_wait_stages)')}
    for column in ('queue_wait_seconds', 'elapsed_seconds'):
        if column not in stage_columns:
            connection.execute(f'ALTER TABLE execution_wait_stages ADD COLUMN {column} REAL NOT NULL DEFAULT 0')
