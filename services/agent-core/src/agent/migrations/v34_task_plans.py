"""Revisable business plans and source-scoped work coverage."""
import re


def migrate(connection):
    original = connection.execute("SELECT sql FROM sqlite_master WHERE name='tool_operations'").fetchone()[0]
    indexes = [row[0] for row in connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='tool_operations' AND sql IS NOT NULL")]
    expanded = re.sub(r'CREATE TABLE ["`\[]?tool_operations["`\]]?',
                      'CREATE TABLE tool_operations_v34', original, count=1)
    expanded = expanded.replace("'document.read_attachment'",
        "'document.read_attachment','tools.discover','task.update_plan','task.request_input','document.review_targets'", 1)
    connection.execute(expanded)
    connection.execute('INSERT INTO tool_operations_v34 SELECT * FROM tool_operations')
    connection.execute('DROP TABLE tool_operations')
    connection.execute('ALTER TABLE tool_operations_v34 RENAME TO tool_operations')
    for index in indexes:
        connection.execute(index)
    connection.execute('''CREATE TABLE IF NOT EXISTS task_plan_revisions (
        task_id TEXT NOT NULL REFERENCES execution_wait_tasks(id) ON DELETE CASCADE,
        revision INTEGER NOT NULL, goal TEXT NOT NULL, request_json TEXT NOT NULL,
        plan_json TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(task_id,revision))''')
    connection.execute('''CREATE TABLE IF NOT EXISTS task_work_items (
        task_id TEXT NOT NULL REFERENCES execution_wait_tasks(id) ON DELETE CASCADE,
        source_id TEXT NOT NULL, source_hash TEXT NOT NULL, target_id TEXT NOT NULL,
        content_json TEXT NOT NULL, outcome TEXT NOT NULL DEFAULT 'pending',
        result_json TEXT, updated_at TEXT NOT NULL,
        PRIMARY KEY(task_id,source_id,target_id))''')
    connection.execute('''CREATE TABLE IF NOT EXISTS task_questions (
        id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES execution_wait_tasks(id) ON DELETE CASCADE,
        fingerprint TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL, payload_json TEXT NOT NULL, answers_json TEXT,
        created_at TEXT NOT NULL, UNIQUE(task_id,fingerprint))''')
    connection.execute('''CREATE TABLE IF NOT EXISTS task_sources (
        task_id TEXT NOT NULL REFERENCES execution_wait_tasks(id) ON DELETE CASCADE,
        source_id TEXT NOT NULL, operation TEXT NOT NULL, artifact_id TEXT NOT NULL,
        source_hash TEXT NOT NULL, PRIMARY KEY(task_id,source_id,operation))''')
