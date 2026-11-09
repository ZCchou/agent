"""Document-centered RAG workspace and auditable import tasks."""
import json


def migrate(connection):
    additions = {
        'rag_jobs': {
            'parent_id': 'TEXT', 'request_key': 'TEXT', 'filename': "TEXT NOT NULL DEFAULT ''",
            'phase': "TEXT NOT NULL DEFAULT 'queued'", 'completed_units': 'INTEGER NOT NULL DEFAULT 0',
            'total_units': 'INTEGER', 'stage_started_at': 'TEXT', 'started_at': 'TEXT', 'finished_at': 'TEXT',
            'termination_source': 'TEXT', 'payload': "TEXT NOT NULL DEFAULT '{}'", 'attempt': 'INTEGER NOT NULL DEFAULT 1',
            'last_version_id': 'TEXT', 'resolved_at': 'TEXT', 'error_code': 'TEXT',
        },
        'rag_documents': {'adopted_version_id': 'TEXT'},
        'rag_versions': {'processing_stage': "TEXT NOT NULL DEFAULT 'final'", 'human_reviewed': 'INTEGER NOT NULL DEFAULT 0'},
        'rag_libraries': {'ocr_mode': "TEXT NOT NULL DEFAULT 'auto'"},
    }
    for table, fields in additions.items():
        existing = {row[1] for row in connection.execute(f'PRAGMA table_info({table})')}
        for name, declaration in fields.items():
            if name not in existing:
                connection.execute(f'ALTER TABLE {table} ADD COLUMN {name} {declaration}')
    connection.execute('''CREATE TABLE IF NOT EXISTS rag_task_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL REFERENCES rag_jobs(id),
        action TEXT NOT NULL, source TEXT NOT NULL, phase TEXT NOT NULL, created_at TEXT NOT NULL)''')
    connection.execute('CREATE INDEX IF NOT EXISTS rag_jobs_parent ON rag_jobs(parent_id,created_at)')
    connection.execute('CREATE UNIQUE INDEX IF NOT EXISTS rag_jobs_request_key ON rag_jobs(library_id,request_key) WHERE request_key IS NOT NULL')
    for row in connection.execute('SELECT id,ocr_summary FROM rag_versions').fetchall():
        summary = json.loads(row[1] or '{}')
        if summary.get('pending_pages') and 'ocr_pages' not in summary:
            connection.execute("UPDATE rag_versions SET processing_stage='initial' WHERE id=?", (row[0],))
    # Existing published indexes determine adoption. Historical evidence keeps its IDs.
    for index in connection.execute('''SELECT i.version_ids FROM rag_libraries l JOIN rag_indexes i ON i.id=l.active_index_id''').fetchall():
        for version_id in json.loads(index[0]):
            connection.execute('''UPDATE rag_documents SET adopted_version_id=? WHERE id=(
                SELECT document_id FROM rag_versions WHERE id=? AND status='confirmed')''', (version_id, version_id))
    connection.execute("UPDATE rag_jobs SET phase=CASE WHEN status='completed' THEN 'completed' ELSE status END")

    connection.execute("""UPDATE rag_jobs SET resolved_at=(SELECT i.created_at FROM rag_libraries l JOIN rag_indexes i ON i.id=l.active_index_id WHERE l.id=rag_jobs.library_id)
        WHERE status IN ('failed','interrupted') AND kind='index.build' AND created_at < COALESCE((SELECT i.created_at FROM rag_libraries l JOIN rag_indexes i ON i.id=l.active_index_id WHERE l.id=rag_jobs.library_id),'')""")
