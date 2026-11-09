"""Additive OCR preview metadata; old evidence and bindings retain their identity."""


def migrate(connection):
    columns = {row[1] for row in connection.execute('PRAGMA table_info(rag_versions)')}
    if 'ocr_summary' not in columns:
        connection.execute("ALTER TABLE rag_versions ADD COLUMN ocr_summary TEXT NOT NULL DEFAULT '{}'")
    connection.execute('''CREATE TABLE IF NOT EXISTS rag_ocr_pages (
        cache_key TEXT PRIMARY KEY, original_hash TEXT NOT NULL, profile_id TEXT NOT NULL,
        page INTEGER NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL)''')
    connection.execute('''CREATE TABLE IF NOT EXISTS rag_ocr_jobs (
        job_id TEXT PRIMARY KEY REFERENCES rag_jobs(id), options TEXT NOT NULL,
        original_path TEXT, original_hash TEXT, total_pages INTEGER NOT NULL DEFAULT 0,
        completed_pages INTEGER NOT NULL DEFAULT 0, phase TEXT NOT NULL DEFAULT 'queued',
        latest_version_id TEXT REFERENCES rag_versions(id))''')

