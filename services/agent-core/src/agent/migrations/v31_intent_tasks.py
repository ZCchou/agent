"""Durable, revisioned preparation tasks before a model run is admitted."""
import re


def migrate(connection):
    sql = connection.execute("SELECT sql FROM sqlite_master WHERE name='tool_operations'").fetchone()[0]
    if "'document.read_attachment'" not in sql:
        indexes = [row[0] for row in connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='tool_operations' AND sql IS NOT NULL")]
        expanded = re.sub(r'CREATE TABLE ["`\[]?tool_operations["`\]]?', 'CREATE TABLE tool_operations_v31', sql, count=1)
        expanded = expanded.replace("'document.export'", "'document.export','document.read_attachment'", 1)
        connection.execute(expanded)
        connection.execute('INSERT INTO tool_operations_v31 SELECT * FROM tool_operations')
        connection.execute('DROP TABLE tool_operations')
        connection.execute('ALTER TABLE tool_operations_v31 RENAME TO tool_operations')
        for index in indexes:
            connection.execute(index)
    connection.execute('''CREATE TABLE IF NOT EXISTS intent_tasks (
        id TEXT PRIMARY KEY,
        idempotency_key TEXT NOT NULL UNIQUE,
        fingerprint TEXT NOT NULL,
        scope TEXT NOT NULL,
        revision INTEGER NOT NULL,
        state_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL)''')
    connection.execute('CREATE INDEX IF NOT EXISTS intent_tasks_scope ON intent_tasks(scope,updated_at)')
