import json

from agent.execution.execution_wait import ExecutionMonitor
from agent.storage import CoreRepository, SCHEMA_VERSION


def test_fresh_repository_exposes_adaptive_execution_tables(tmp_path):
    repository = CoreRepository(tmp_path / 'core.db')
    with repository._connect() as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert SCHEMA_VERSION == 32
        assert {'execution_wait_tasks', 'execution_wait_stages', 'execution_wait_checkpoints'} <= tables
        schema = connection.execute("SELECT sql FROM sqlite_master WHERE name='model_runs'").fetchone()[0]
        assert "'waiting_recovery'" in schema


def test_schema31_upgrade_keeps_business_rows_and_creates_backup(tmp_path):
    repository = CoreRepository(tmp_path / 'core.db')
    with repository._connect() as connection:
        connection.execute('UPDATE schema_info SET version=31')
        connection.commit()
        before = connection.execute('SELECT COUNT(*) FROM projects').fetchone()[0]
    migrated = CoreRepository(repository.database_path)
    with migrated._connect() as connection:
        assert connection.execute('SELECT version FROM schema_info').fetchone()[0] == 32
        assert connection.execute('SELECT COUNT(*) FROM projects').fetchone()[0] == before
        assert connection.execute('PRAGMA foreign_key_check').fetchall() == []
    assert list(tmp_path.glob('*.pre-schema32-*.bak'))


def test_permanent_conversation_delete_cleans_unadmitted_intent_and_execution(tmp_path):
    repository = CoreRepository(tmp_path / 'core.db')
    conversation = repository.create_conversation('standard_chat', '审阅', category='work')
    state = json.dumps({'request': {'conversation_id': conversation['id']}})
    with repository._connect() as connection:
        connection.execute('INSERT INTO intent_tasks VALUES (?,?,?,?,?,?,?,?)',
                           ('intent-delete', 'delete-key', 'fingerprint', conversation['id'], 1, state, 'now', 'now'))
    root = ExecutionMonitor(repository).start_task(intent_task_id='intent-delete')
    repository.delete_conversation(conversation['id'])
    repository.permanently_delete_trash('conversation', conversation['id'])
    with repository._connect() as connection:
        assert connection.execute('SELECT 1 FROM execution_wait_tasks WHERE id=?', (root['id'],)).fetchone() is None
        assert connection.execute('SELECT 1 FROM intent_tasks WHERE id=?', ('intent-delete',)).fetchone() is None
