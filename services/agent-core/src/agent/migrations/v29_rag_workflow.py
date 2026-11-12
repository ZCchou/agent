"""Conversation evidence policy and recoverable document artifacts."""
import re


def migrate(connection):
    sql = connection.execute("SELECT sql FROM sqlite_master WHERE name='tool_operations'").fetchone()[0]
    if "'document.export'" not in sql:
        indexes = [row[0] for row in connection.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='tool_operations' AND sql IS NOT NULL")]
        expanded = re.sub(r'CREATE TABLE ["`\[]?tool_operations["`\]]?', 'CREATE TABLE tool_operations_v29', sql, count=1)
        expanded = expanded.replace("'rag.read'", "'rag.read','document.inspect_template','document.fill_template','document.export'", 1)
        connection.execute(expanded)
        connection.execute('INSERT INTO tool_operations_v29 SELECT * FROM tool_operations')
        connection.execute('DROP TABLE tool_operations')
        connection.execute('ALTER TABLE tool_operations_v29 RENAME TO tool_operations')
        for index in indexes:
            connection.execute(index)
    additions = {
        'message_attachments': {'purpose': "TEXT NOT NULL DEFAULT 'reference' CHECK(purpose IN ('reference','template'))"},
        'conversations': {'rag_mode': "TEXT NOT NULL DEFAULT 'auto' CHECK(rag_mode IN ('auto','required'))"},
        'model_runs': {'rag_requested_mode': "TEXT NOT NULL DEFAULT 'auto' CHECK(rag_requested_mode IN ('auto','required'))",
                       'rag_decision_json': "TEXT NOT NULL DEFAULT '{}'"},
    }
    for table, fields in additions.items():
        existing = {row[1] for row in connection.execute(f'PRAGMA table_info({table})')}
        for name, declaration in fields.items():
            if name not in existing:
                connection.execute(f'ALTER TABLE {table} ADD COLUMN {name} {declaration}')
                if table == 'message_attachments' and name == 'purpose':
                    connection.execute("UPDATE message_attachments SET purpose='template' WHERE lower(name) LIKE '%.docx' AND (name LIKE '%模板%' OR lower(name) LIKE '%template%')")
    connection.execute('''CREATE TABLE IF NOT EXISTS document_artifacts (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
        run_id TEXT NOT NULL REFERENCES model_runs(id), kind TEXT NOT NULL,
        storage_key TEXT NOT NULL, sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
        template_id TEXT, template_sha256 TEXT, manifest_json TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'staged', output_path TEXT, created_at TEXT NOT NULL)''')
    connection.execute('CREATE INDEX IF NOT EXISTS document_artifacts_run ON document_artifacts(run_id)')
