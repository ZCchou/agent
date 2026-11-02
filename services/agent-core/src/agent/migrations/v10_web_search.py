from __future__ import annotations

import sqlite3


def migrate(connection: sqlite3.Connection) -> None:
    """Add auditable web-search policy and operation metadata without copying page bodies."""
    protected = ("projects", "conversations", "messages", "model_runs")
    before = {
        table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in protected
    }

    connection.executescript(
        """
        ALTER TABLE projects ADD COLUMN web_access_policy TEXT NOT NULL DEFAULT 'disabled'
            CHECK(web_access_policy IN ('disabled','allow_search'));
        ALTER TABLE conversations ADD COLUMN web_search_mode TEXT NOT NULL DEFAULT 'off'
            CHECK(web_search_mode IN ('off','auto'));
        ALTER TABLE model_runs ADD COLUMN web_search_request_mode TEXT NOT NULL DEFAULT 'off'
            CHECK(web_search_request_mode IN ('off','auto','force_once'));
        ALTER TABLE model_runs ADD COLUMN web_search_route TEXT NOT NULL DEFAULT 'none'
            CHECK(web_search_route IN ('none','native','host_tool','host_presearch'));
        ALTER TABLE model_runs ADD COLUMN web_search_reason_code TEXT NOT NULL DEFAULT 'web_search_off';
        ALTER TABLE model_runs ADD COLUMN web_search_policy_version TEXT NULL;

        CREATE TABLE web_operations (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES model_runs(id) ON DELETE CASCADE,
            sequence INTEGER NOT NULL CHECK(sequence >= 1),
            idempotency_key TEXT NOT NULL UNIQUE,
            route TEXT NOT NULL CHECK(route IN ('native','host_tool','host_presearch')),
            backend_id TEXT NULL,
            query_text TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN
                ('prepared','dispatched','searching','reading_sources','completed','failed',
                 'cancelled','interrupted','unknown_outcome')),
            result_count INTEGER NOT NULL DEFAULT 0 CHECK(result_count >= 0),
            usage_json TEXT NOT NULL DEFAULT '{}',
            error_code TEXT NULL,
            created_at TEXT NOT NULL,
            dispatched_at TEXT NULL,
            finished_at TEXT NULL,
            UNIQUE(run_id, sequence)
        );
        CREATE TABLE web_sources (
            id TEXT PRIMARY KEY,
            operation_id TEXT NOT NULL REFERENCES web_operations(id) ON DELETE CASCADE,
            position INTEGER NOT NULL CHECK(position >= 0),
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            domain TEXT NOT NULL,
            snippet TEXT NULL,
            published_at TEXT NULL,
            retrieved_at TEXT NOT NULL,
            content_hash TEXT NULL,
            provider_source_id TEXT NULL,
            trust TEXT NOT NULL DEFAULT 'external_untrusted'
                CHECK(trust = 'external_untrusted'),
            UNIQUE(operation_id, position)
        );
        """
    )

    after = {
        table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in protected
    }
    if before != after:
        raise RuntimeError("Web-search migration changed protected record counts")
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError("Web-search migration produced foreign-key violations")
