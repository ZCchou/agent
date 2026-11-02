"""Transactional tool CHECK expansion; caller disables FKs before BEGIN."""
import sqlite3
import re


def migrate(connection: sqlite3.Connection) -> None:
    sql = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='tool_operations'").fetchone()[0]
    if "'terminal.run'" in sql:
        return
    indexes = [row[0] for row in connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='tool_operations' AND sql IS NOT NULL")]
    expanded = re.sub(r'CREATE TABLE ["`\[]?tool_operations["`\]]?', 'CREATE TABLE tool_operations_v22', sql, count=1)
    expanded = expanded.replace("'fs.propose_changes'", "'fs.propose_changes','terminal.run'", 1)
    connection.execute(expanded)
    connection.execute('INSERT INTO tool_operations_v22 SELECT * FROM tool_operations')
    connection.execute('DROP TABLE tool_operations')
    connection.execute('ALTER TABLE tool_operations_v22 RENAME TO tool_operations')
    for index in indexes:
        connection.execute(index)
    if connection.execute('PRAGMA foreign_key_check').fetchall():
        raise RuntimeError('Terminal migration violated foreign keys')
