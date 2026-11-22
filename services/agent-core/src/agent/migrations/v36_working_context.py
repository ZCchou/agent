"""Immutable tool results and resumable, bounded working contexts."""
import re


def migrate(connection):
    original = connection.execute("SELECT sql FROM sqlite_master WHERE name='tool_operations'").fetchone()[0]
    indexes = [row[0] for row in connection.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='tool_operations' AND sql IS NOT NULL")]
    expanded = re.sub(r'CREATE TABLE ["`\[]?tool_operations["`\]]?', 'CREATE TABLE tool_operations_v36', original, count=1)
    expanded = expanded.replace("'tools.discover'", "'tools.discover','tools.read_result','tools.search_result'", 1)
    connection.execute(expanded)
    connection.execute('INSERT INTO tool_operations_v36 SELECT * FROM tool_operations')
    connection.execute('DROP TABLE tool_operations')
    connection.execute('ALTER TABLE tool_operations_v36 RENAME TO tool_operations')
    for index in indexes:
        connection.execute(index)
    connection.execute('''CREATE TABLE IF NOT EXISTS run_tool_segments (
        run_id TEXT NOT NULL REFERENCES model_runs(id) ON DELETE CASCADE,
        attempt_id TEXT NOT NULL, call_offset INTEGER NOT NULL, read_offset INTEGER NOT NULL,
        PRIMARY KEY(run_id,attempt_id))''')
    connection.execute('''CREATE TABLE IF NOT EXISTS tool_result_objects (
        operation_id TEXT PRIMARY KEY REFERENCES tool_operations(id) ON DELETE CASCADE,
        run_id TEXT NOT NULL REFERENCES model_runs(id) ON DELETE CASCADE,
        sha256 TEXT NOT NULL, byte_size INTEGER NOT NULL, character_count INTEGER NOT NULL,
        success INTEGER NOT NULL, source_json TEXT NOT NULL, created_at TEXT NOT NULL)''')
    connection.execute('''CREATE TABLE IF NOT EXISTS run_working_contexts (
        run_id TEXT PRIMARY KEY REFERENCES model_runs(id) ON DELETE CASCADE,
        attempt_id TEXT NOT NULL, revision INTEGER NOT NULL,
        context_json TEXT NOT NULL, created_at TEXT NOT NULL)''')
