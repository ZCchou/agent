"""Schema 20 global behavior-instruction migration."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone

from agent.global_instructions import (
    DEFAULT_GLOBAL_INSTRUCTIONS,
    DEFAULT_GLOBAL_INSTRUCTIONS_VERSION,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def migrate(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS global_instructions (
               id INTEGER PRIMARY KEY CHECK(id = 1),
               content TEXT NOT NULL,
               enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
               revision INTEGER NOT NULL CHECK(revision >= 1),
               content_hash TEXT NOT NULL,
               default_version TEXT NOT NULL,
               updated_at TEXT NOT NULL
           )"""
    )
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(context_snapshots)").fetchall()
    }
    if "global_instruction_revision" not in columns:
        connection.execute(
            "ALTER TABLE context_snapshots ADD COLUMN global_instruction_revision INTEGER NOT NULL DEFAULT 0"
        )
    if "global_instruction_hash" not in columns:
        connection.execute(
            "ALTER TABLE context_snapshots ADD COLUMN global_instruction_hash TEXT NULL"
        )
    content_hash = hashlib.sha256(DEFAULT_GLOBAL_INSTRUCTIONS.encode("utf-8")).hexdigest()
    connection.execute(
        """INSERT OR IGNORE INTO global_instructions(
               id, content, enabled, revision, content_hash, default_version, updated_at
           ) VALUES (1, ?, 1, 1, ?, ?, ?)""",
        (DEFAULT_GLOBAL_INSTRUCTIONS, content_hash, DEFAULT_GLOBAL_INSTRUCTIONS_VERSION, _utc_now()),
    )
