from __future__ import annotations

import sqlite3
import uuid


def migrate(connection: sqlite3.Connection) -> None:
    """Add immutable invocation specs and attachment artifacts without losing v14 audit data."""
    protected = tuple(
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name != 'schema_info' ORDER BY name"
        ).fetchall()
    )
    before = {
        table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in protected
    }
    existing = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }

    if "attachment_artifacts" not in existing:
        connection.executescript(
            """
            CREATE TABLE attachment_artifacts (
                id TEXT PRIMARY KEY,
                attachment_id TEXT NOT NULL REFERENCES message_attachments(id) ON DELETE CASCADE,
                kind TEXT NOT NULL CHECK(kind IN
                    ('text.utf8','image.normalized','document.text','document.page_image',
                     'audio.transcript','archive.manifest')),
                processor_id TEXT NOT NULL,
                processor_version TEXT NOT NULL,
                media_type TEXT NOT NULL,
                byte_size INTEGER NOT NULL CHECK(byte_size >= 0),
                sha256 TEXT NOT NULL,
                storage_key TEXT NOT NULL,
                width INTEGER NULL CHECK(width IS NULL OR width > 0),
                height INTEGER NULL CHECK(height IS NULL OR height > 0),
                trust TEXT NOT NULL DEFAULT 'external_untrusted'
                    CHECK(trust = 'external_untrusted'),
                created_at TEXT NOT NULL,
                UNIQUE(attachment_id, kind, sha256)
            );
            CREATE INDEX idx_attachment_artifacts_attachment
                ON attachment_artifacts(attachment_id, kind);
            CREATE INDEX idx_attachment_artifacts_storage
                ON attachment_artifacts(storage_key);

            CREATE TABLE message_attachment_rejections (
                id TEXT PRIMARY KEY,
                message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                display_name TEXT NOT NULL,
                detected_media_type TEXT NULL,
                error_code TEXT NOT NULL,
                processor_hint TEXT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX idx_attachment_rejections_message
                ON message_attachment_rejections(message_id, created_at);
            """
        )
        rows = connection.execute("SELECT * FROM message_attachments").fetchall()
        for row in rows:
            if row["kind"] == "text":
                values = (
                    str(uuid.uuid4()), row["id"], "text.utf8", "builtin.utf8_text",
                    "utf8-text-v1", row["media_type"], row["byte_size"], row["sha256"],
                    row["storage_key"], None, None, row["created_at"],
                )
            elif row["model_storage_key"]:
                values = (
                    str(uuid.uuid4()), row["id"], "image.normalized", "builtin.image",
                    row["normalization_version"] or "image-normalization-v1",
                    row["model_media_type"], row["model_byte_size"], row["model_sha256"],
                    row["model_storage_key"], row["model_width"], row["model_height"],
                    row["created_at"],
                )
            elif row["media_type"] in {"image/png", "image/jpeg"}:
                values = (
                    str(uuid.uuid4()), row["id"], "image.normalized", "legacy.passthrough",
                    "legacy_passthrough", row["media_type"], row["byte_size"], row["sha256"],
                    row["storage_key"], row["width"], row["height"], row["created_at"],
                )
            else:
                continue
            connection.execute(
                """INSERT INTO attachment_artifacts(
                       id, attachment_id, kind, processor_id, processor_version, media_type,
                       byte_size, sha256, storage_key, width, height, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                values,
            )

    attachment_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(message_attachments)").fetchall()
    }
    if "model_storage_key" in attachment_columns:
        connection.executescript(
            """
            CREATE TABLE message_attachments_v15 (
                id TEXT PRIMARY KEY,
                message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                kind TEXT NOT NULL CHECK(kind IN ('text','image')),
                name TEXT NOT NULL,
                media_type TEXT NOT NULL,
                byte_size INTEGER NOT NULL CHECK(byte_size >= 0),
                sha256 TEXT NOT NULL,
                storage_key TEXT NOT NULL,
                width INTEGER NULL CHECK(width IS NULL OR width > 0),
                height INTEGER NULL CHECK(height IS NULL OR height > 0),
                source_attachment_id TEXT NULL REFERENCES message_attachments(id),
                created_at TEXT NOT NULL
            );
            INSERT INTO message_attachments_v15(
                id, message_id, kind, name, media_type, byte_size, sha256,
                storage_key, width, height, source_attachment_id, created_at
            )
            SELECT id, message_id, kind, name, media_type, byte_size, sha256,
                   storage_key, width, height, source_attachment_id, created_at
            FROM message_attachments;
            DROP TABLE message_attachments;
            ALTER TABLE message_attachments_v15 RENAME TO message_attachments;
            """
        )

    if "invocation_specs" not in existing:
        connection.executescript(
            """
            CREATE TABLE invocation_specs (
                id TEXT PRIMARY KEY,
                plan_id TEXT NOT NULL REFERENCES execution_plans(id) ON DELETE CASCADE,
                step_id TEXT NOT NULL REFERENCES execution_steps(id) ON DELETE CASCADE,
                round_no INTEGER NOT NULL CHECK(round_no >= 0),
                attempt_no INTEGER NOT NULL CHECK(attempt_no >= 1),
                kind TEXT NOT NULL CHECK(kind IN ('model','host_search')),
                provider_or_backend_id TEXT NOT NULL,
                model_id TEXT NULL,
                route_id TEXT NOT NULL,
                protocol_id TEXT NOT NULL,
                adapter_id TEXT NOT NULL,
                endpoint_role TEXT NOT NULL,
                route_fingerprint TEXT NOT NULL,
                capability_signature_json TEXT NOT NULL,
                input_refs_json TEXT NOT NULL,
                tools_json TEXT NOT NULL,
                generation_json TEXT NOT NULL,
                limits_json TEXT NOT NULL,
                serialization_version TEXT NOT NULL,
                authority_generation INTEGER NOT NULL CHECK(authority_generation >= 0),
                status TEXT NOT NULL CHECK(status IN
                    ('prepared','dispatched','streaming','completed','failed','cancelled',
                     'interrupted','unknown_outcome','stale')),
                spec_hash TEXT NOT NULL,
                request_fingerprint TEXT NULL,
                error_code TEXT NULL,
                created_at TEXT NOT NULL,
                dispatched_at TEXT NULL,
                finished_at TEXT NULL,
                UNIQUE(step_id, round_no, attempt_no),
                UNIQUE(spec_hash)
            );
            CREATE INDEX idx_invocation_specs_plan_status
                ON invocation_specs(plan_id, status, created_at);
            """
        )

    plan_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(execution_plans)").fetchall()
    }
    if "authority_generation" not in plan_columns:
        connection.execute(
            "ALTER TABLE execution_plans ADD COLUMN authority_generation "
            "INTEGER NOT NULL DEFAULT 0"
        )
    if "web_backend_kind" not in plan_columns:
        connection.execute(
            "ALTER TABLE execution_plans ADD COLUMN web_backend_kind TEXT NULL "
            "CHECK(web_backend_kind IS NULL OR web_backend_kind IN ('tavily','searxng'))"
        )

    attempt_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(run_attempts)").fetchall()
    }
    if "invocation_spec_id" not in attempt_columns:
        connection.execute("ALTER TABLE run_attempts ADD COLUMN invocation_spec_id TEXT NULL")
    step_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(execution_steps)").fetchall()
    }
    if "planned_target_fingerprint" not in step_columns:
        connection.execute(
            "ALTER TABLE execution_steps ADD COLUMN planned_target_fingerprint "
            "TEXT NOT NULL DEFAULT 'legacy-v14'"
        )
        connection.execute(
            "UPDATE execution_steps SET planned_target_fingerprint = route_config_fingerprint"
        )

    # v14 activity has no per-dispatch immutable spec and cannot be safely resumed.
    active_plans = tuple(
        row[0] for row in connection.execute(
            "SELECT id FROM execution_plans WHERE status IN "
            "('resolving','waiting_permission','waiting_budget','planned','executing','synthesizing')"
        ).fetchall()
    )
    if active_plans:
        placeholders = ",".join("?" for _ in active_plans)
        connection.execute(
            f"UPDATE execution_steps SET status='interrupted', error_code='invocation_spec_stale' "
            f"WHERE plan_id IN ({placeholders}) AND status IN ('queued','running')",
            active_plans,
        )
        connection.execute(
            f"UPDATE execution_plans SET status='stale' WHERE id IN ({placeholders})",
            active_plans,
        )
        connection.execute(
            f"UPDATE model_runs SET status='interrupted', error_code='invocation_spec_stale', "
            f"error_message='The previous run did not persist an immutable invocation specification.' "
            f"WHERE execution_plan_id IN ({placeholders}) AND status NOT IN "
            "('completed','failed','cancelled','interrupted')",
            active_plans,
        )

    after = {
        table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in protected
    }
    if before != after:
        raise RuntimeError("unified invocation migration changed protected record counts")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise RuntimeError("unified invocation migration failed foreign-key validation")
