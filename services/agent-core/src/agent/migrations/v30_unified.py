"""Durable provenance and recovery metadata for unified-profile migration."""


def create_tables(connection):
    connection.execute('''CREATE TABLE IF NOT EXISTS profile_migration_sources (
        id TEXT PRIMARY KEY, source_label TEXT NOT NULL, source_digest TEXT NOT NULL,
        report_json TEXT NOT NULL, UNIQUE(source_label,source_digest))''')
    connection.execute('''CREATE TABLE IF NOT EXISTS profile_migration_jobs (
        id TEXT PRIMARY KEY, status TEXT NOT NULL
            CHECK(status IN ('queued','running','verifying','prepared','publishing','awaiting_restart',
                             'finalizing','completed','rolled_back','cancelled','interrupted','failed')),
        phase TEXT NOT NULL, sources_json TEXT NOT NULL, options_json TEXT NOT NULL,
        progress_json TEXT NOT NULL DEFAULT '{}', result_json TEXT, error_json TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')


def migrate(connection):
    create_tables(connection)
