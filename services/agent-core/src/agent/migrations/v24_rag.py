"""Local evidence libraries and audited RAG tools. Caller owns the transaction."""
import re
import sqlite3


STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS rag_libraries (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, embedding_model_id TEXT NOT NULL,
        reranker_model_id TEXT NOT NULL, active_index_id TEXT, generation INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL, archived_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS rag_projects (
        project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
        generation INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS rag_bindings (
        library_id TEXT NOT NULL REFERENCES rag_libraries(id) ON DELETE CASCADE,
        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
        index_id TEXT REFERENCES rag_indexes(id),
        PRIMARY KEY(library_id,project_id))""",
    """CREATE TABLE IF NOT EXISTS rag_documents (
        id TEXT PRIMARY KEY, library_id TEXT NOT NULL REFERENCES rag_libraries(id),
        source_key TEXT NOT NULL, created_at TEXT NOT NULL,
        UNIQUE(library_id,source_key))""",
    """CREATE TABLE IF NOT EXISTS rag_versions (
        id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES rag_documents(id),
        title TEXT NOT NULL, revision TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
        document_number TEXT NOT NULL DEFAULT '', original_hash TEXT NOT NULL,
        original_name TEXT NOT NULL, media_type TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('pending','confirmed','withdrawn')),
        warnings TEXT NOT NULL DEFAULT '[]', chunk_count INTEGER NOT NULL,
        created_at TEXT NOT NULL, confirmed_at TEXT, withdrawn_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS rag_chunks (
        id TEXT PRIMARY KEY, version_id TEXT NOT NULL REFERENCES rag_versions(id),
        ordinal INTEGER NOT NULL, text TEXT NOT NULL, text_hash TEXT NOT NULL,
        clause_number TEXT, locator TEXT NOT NULL, UNIQUE(version_id,ordinal))""",
    """CREATE TABLE IF NOT EXISTS rag_jobs (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, library_id TEXT NOT NULL REFERENCES rag_libraries(id),
        status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed','cancelled','interrupted')),
        progress REAL NOT NULL DEFAULT 0, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS rag_indexes (
        id TEXT PRIMARY KEY, library_id TEXT NOT NULL REFERENCES rag_libraries(id),
        embedding_model_id TEXT NOT NULL, embedding_digest TEXT NOT NULL,
        reranker_model_id TEXT NOT NULL, reranker_digest TEXT NOT NULL,
        dimension INTEGER NOT NULL, version_ids TEXT NOT NULL,
        splitter_versions TEXT NOT NULL DEFAULT '["fixed-1200-v1"]',
        chunk_count INTEGER NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS rag_index_chunks (
        index_id TEXT NOT NULL REFERENCES rag_indexes(id) ON DELETE CASCADE,
        chunk_id TEXT NOT NULL REFERENCES rag_chunks(id), embedding BLOB NOT NULL,
        PRIMARY KEY(index_id,chunk_id))""",
    """CREATE VIRTUAL TABLE IF NOT EXISTS rag_fts USING fts5(
        chunk_id UNINDEXED, tokens, tokenize='unicode61 remove_diacritics 0')""",
    """CREATE TABLE IF NOT EXISTS rag_searches (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE, run_id TEXT,
        query TEXT NOT NULL, generation INTEGER NOT NULL, index_ids TEXT NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS rag_evidence (
        id TEXT PRIMARY KEY, search_id TEXT NOT NULL REFERENCES rag_searches(id) ON DELETE CASCADE,
        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE, library_id TEXT NOT NULL REFERENCES rag_libraries(id),
        index_id TEXT NOT NULL REFERENCES rag_indexes(id), version_id TEXT NOT NULL REFERENCES rag_versions(id),
        chunk_id TEXT NOT NULL REFERENCES rag_chunks(id), payload TEXT NOT NULL)""",
    'CREATE INDEX IF NOT EXISTS idx_rag_chunks_version ON rag_chunks(version_id,ordinal)',
    'CREATE INDEX IF NOT EXISTS idx_rag_documents_library ON rag_documents(library_id)',
    'CREATE INDEX IF NOT EXISTS idx_rag_bindings_project ON rag_bindings(project_id,enabled)',
    'CREATE INDEX IF NOT EXISTS idx_rag_versions_document ON rag_versions(document_id,status)',
]


def create_tables(connection: sqlite3.Connection) -> None:
    for statement in STATEMENTS:
        connection.execute(statement)
    columns = {row[1] for row in connection.execute('PRAGMA table_info(rag_bindings)')}
    if 'index_id' not in columns:
        connection.execute('ALTER TABLE rag_bindings ADD COLUMN index_id TEXT REFERENCES rag_indexes(id)')
        connection.execute('''UPDATE rag_bindings SET index_id=(SELECT active_index_id FROM rag_libraries
            WHERE rag_libraries.id=rag_bindings.library_id) WHERE enabled=1''')
    index_columns = {row[1] for row in connection.execute('PRAGMA table_info(rag_indexes)')}
    if 'splitter_versions' not in index_columns:
        connection.execute('''ALTER TABLE rag_indexes ADD COLUMN splitter_versions TEXT NOT NULL DEFAULT '["fixed-1200-v1"]' ''')


def migrate(connection: sqlite3.Connection) -> None:
    row = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='tool_operations'").fetchone()
    if row is not None and "'rag.search'" not in row[0]:
        sql = row[0]
        indexes = [item[0] for item in connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='tool_operations' AND sql IS NOT NULL")]
        expanded = re.sub(r'CREATE TABLE ["`\[]?tool_operations["`\]]?', 'CREATE TABLE tool_operations_v24', sql, count=1)
        expanded = expanded.replace("'skills.load'", "'skills.load','rag.search','rag.read'", 1)
        if expanded == sql or "'rag.search'" not in expanded:
            raise RuntimeError('Unrecognized tool operation schema for RAG migration')
        connection.execute(expanded)
        connection.execute('INSERT INTO tool_operations_v24 SELECT * FROM tool_operations')
        connection.execute('DROP TABLE tool_operations')
        connection.execute('ALTER TABLE tool_operations_v24 RENAME TO tool_operations')
        for index in indexes:
            connection.execute(index)
    create_tables(connection)
    if connection.execute('PRAGMA foreign_key_check').fetchall():
        raise RuntimeError('RAG migration violated foreign keys')
