"""Preserve attachment records while admitting immutable Office/PDF sources."""
import sqlite3
import re


def migrate(connection: sqlite3.Connection) -> None:
    declaration = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='attachment_artifacts'"
    ).fetchone()[0]
    if "'document.source'" in declaration:
        return
    declaration = re.sub(r'^CREATE TABLE\s+(?:"attachment_artifacts"|attachment_artifacts)',
                         'CREATE TABLE attachment_artifacts_v27', declaration, count=1, flags=re.IGNORECASE)
    declaration = declaration.replace("'document.text'", "'document.source','document.text'", 1)
    connection.execute(declaration)
    connection.execute('INSERT INTO attachment_artifacts_v27 SELECT * FROM attachment_artifacts')
    connection.execute('DROP TABLE attachment_artifacts')
    connection.execute('ALTER TABLE attachment_artifacts_v27 RENAME TO attachment_artifacts')
    connection.execute('CREATE INDEX idx_attachment_artifacts_attachment ON attachment_artifacts(attachment_id,kind)')
    connection.execute('CREATE INDEX idx_attachment_artifacts_storage ON attachment_artifacts(storage_key)')
