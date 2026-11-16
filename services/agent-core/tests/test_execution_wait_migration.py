import json

from agent.execution.execution_wait import ExecutionMonitor
from agent.storage import CoreRepository, SCHEMA_VERSION


def test_fresh_repository_exposes_adaptive_execution_tables(tmp_path):
    repository = CoreRepository(tmp_path / 'core.db')
    with repository._connect() as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert SCHEMA_VERSION == 36
        assert {'execution_wait_tasks', 'execution_wait_stages', 'execution_wait_checkpoints'} <= tables
        schema = connection.execute("SELECT sql FROM sqlite_master WHERE name='model_runs'").fetchone()[0]
        assert "'waiting_recovery'" in schema


def test_schema34_upgrade_preserves_existing_plan_and_creates_independent_goals(tmp_path):
    from agent.execution.task_plans import TaskPlans
    repository = CoreRepository(tmp_path / 'core.db')
    chat = repository.create_conversation('standard_chat', '旧对话', category='work')
    repository._insert_message(chat['id'], 'user', '原始请求')
    monitor = ExecutionMonitor(repository)
    root = monitor.start_task(intent_task_id='schema34-request')
    plans = TaskPlans(repository)
    plans.create(root['id'], {'content': '审阅模板', 'conversation_id': chat['id']})
    plans.update(root['id'], expected_revision=0, steps=[], success_criteria=['完成审阅'])
    with repository._connect() as connection:
        connection.execute('DROP TABLE task_goal_verifications')
        connection.execute('DROP TABLE task_goal_requirements')
        connection.execute('UPDATE schema_info SET version=34')
    upgraded = CoreRepository(repository.database_path)
    migrated = TaskPlans(upgraded).get(root['id'])
    assert migrated['success_criteria'] == ['完成审阅']
    assert migrated['goal_requirements'] is None
    assert upgraded.get_conversation(chat['id'])['messages'][0]['content'] == '原始请求'
    assert not TaskPlans(upgraded).completion(root['id'], require_declared=True)['ready']
    with upgraded._connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM task_goal_verifications').fetchone()[0] == 0
        assert not connection.execute('PRAGMA foreign_key_check').fetchall()
    assert list(tmp_path.glob('*.pre-schema36-*.bak'))


def test_schema31_upgrade_keeps_business_rows_and_creates_backup(tmp_path):
    repository = CoreRepository(tmp_path / 'core.db')
    with repository._connect() as connection:
        connection.execute('UPDATE schema_info SET version=31')
        connection.commit()
        before = connection.execute('SELECT COUNT(*) FROM projects').fetchone()[0]
    migrated = CoreRepository(repository.database_path)
    with migrated._connect() as connection:
        assert connection.execute('SELECT version FROM schema_info').fetchone()[0] == 36
        assert connection.execute('SELECT COUNT(*) FROM projects').fetchone()[0] == before
        assert connection.execute('PRAGMA foreign_key_check').fetchall() == []
    assert list(tmp_path.glob('*.pre-schema36-*.bak'))


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
