from __future__ import annotations

import sqlite3


def migrate(connection: sqlite3.Connection) -> None:
    protected = ("projects", "conversations", "messages", "model_runs", "message_attachments")
    before = {
        table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in protected
    }
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(message_attachments)").fetchall()
    }
    additions = (
        ("model_storage_key", "TEXT NULL"),
        ("model_media_type", "TEXT NULL"),
        ("model_byte_size", "INTEGER NULL CHECK(model_byte_size >= 0)"),
        ("model_sha256", "TEXT NULL"),
        ("model_width", "INTEGER NULL CHECK(model_width > 0)"),
        ("model_height", "INTEGER NULL CHECK(model_height > 0)"),
        ("normalization_version", "TEXT NULL"),
    )
    for name, declaration in additions:
        if name not in columns:
            connection.execute(
                f"ALTER TABLE message_attachments ADD COLUMN {name} {declaration}"
            )
    connection.execute(
        """UPDATE message_attachments SET normalization_version = 'legacy_passthrough'
           WHERE kind = 'image' AND normalization_version IS NULL"""
    )
    after = {
        table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in protected
    }
    if before != after:
        raise RuntimeError("multimodal image migration changed protected record counts")
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError("multimodal image migration failed foreign-key validation")
