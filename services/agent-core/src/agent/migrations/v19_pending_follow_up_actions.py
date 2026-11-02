"""Schema 19 persistence for one-turn conversational action confirmations."""

from __future__ import annotations

import sqlite3


def migrate(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS pending_follow_up_actions (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            source_run_id TEXT NOT NULL REFERENCES model_runs(id) ON DELETE CASCADE,
            source_message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            action_type TEXT NOT NULL CHECK(action_type IN ('web_search')),
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','consumed','rejected','expired')),
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            resolved_at TEXT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_follow_up_one_per_conversation
            ON pending_follow_up_actions(conversation_id) WHERE status = 'pending';
        CREATE INDEX IF NOT EXISTS idx_pending_follow_up_expiry
            ON pending_follow_up_actions(status, expires_at);
        """,
    )
