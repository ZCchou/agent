"""Independent goal requirements and persisted final verification."""


def migrate(connection):
    connection.execute('''CREATE TABLE IF NOT EXISTS task_goal_requirements (
        task_id TEXT NOT NULL REFERENCES execution_wait_tasks(id) ON DELETE CASCADE,
        revision INTEGER NOT NULL, request_digest TEXT NOT NULL,
        requirements_json TEXT NOT NULL, provenance TEXT NOT NULL,
        created_at TEXT NOT NULL, PRIMARY KEY(task_id,revision))''')
    connection.execute('''CREATE TABLE IF NOT EXISTS task_goal_verifications (
        task_id TEXT NOT NULL REFERENCES execution_wait_tasks(id) ON DELETE CASCADE,
        execution_revision INTEGER NOT NULL, attempt INTEGER NOT NULL,
        requirements_revision INTEGER NOT NULL, result_digest TEXT NOT NULL,
        result_json TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(task_id,execution_revision,attempt,requirements_revision,result_digest))''')
