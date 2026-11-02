"""Expand the audited tool vocabulary without losing existing operations."""
import re
import sqlite3


def migrate(connection: sqlite3.Connection) -> None:
    sql = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='tool_operations'").fetchone()[0]
    if "'skills.load'" in sql:
        return
    indexes = [row[0] for row in connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='tool_operations' AND sql IS NOT NULL")]
    expanded = re.sub(r'CREATE TABLE ["`\[]?tool_operations["`\]]?', 'CREATE TABLE tool_operations_v23', sql, count=1)
    expanded = expanded.replace("'terminal.run'", "'terminal.run','skills.load','skills.read_resource','skills.save'", 1)
    connection.execute(expanded)
    connection.execute('INSERT INTO tool_operations_v23 SELECT * FROM tool_operations')
    connection.execute('DROP TABLE tool_operations')
    connection.execute('ALTER TABLE tool_operations_v23 RENAME TO tool_operations')
    for index in indexes:
        connection.execute(index)
    if connection.execute('PRAGMA foreign_key_check').fetchall():
        raise RuntimeError('Skill migration violated foreign keys')
