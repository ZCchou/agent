from __future__ import annotations

import sqlite3


def migrate(connection: sqlite3.Connection) -> None:
    """Persist resolved intent and conditional-step state for the v3.1 pipeline."""
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

    plan_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(execution_plans)").fetchall()
    }
    if "resolved_intent_json" not in plan_columns:
        connection.execute(
            "ALTER TABLE execution_plans ADD COLUMN resolved_intent_json "
            "TEXT NOT NULL DEFAULT '{}'"
        )
    if "resolution_hash" not in plan_columns:
        connection.execute(
            "ALTER TABLE execution_plans ADD COLUMN resolution_hash TEXT NOT NULL DEFAULT ''"
        )
    if "resolved_at" not in plan_columns:
        connection.execute("ALTER TABLE execution_plans ADD COLUMN resolved_at TEXT NULL")
    connection.execute(
        """UPDATE execution_plans
              SET resolved_intent_json = requirements_json,
                  resolution_hash = requirements_hash,
                  resolved_at = COALESCE(resolved_at, created_at)
            WHERE resolution_hash = ''"""
    )

    step_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(execution_steps)").fetchall()
    }
    if "activation_condition" not in step_columns:
        connection.executescript(
            """
            ALTER TABLE execution_steps RENAME TO execution_steps_v15;
            CREATE TABLE execution_steps (
                id TEXT PRIMARY KEY,
                plan_id TEXT NOT NULL REFERENCES execution_plans(id) ON DELETE CASCADE,
                phase TEXT NOT NULL CHECK(phase IN ('analysis','specialist','synthesis')),
                order_index INTEGER NOT NULL CHECK(order_index >= 0),
                step_type TEXT NOT NULL CHECK(step_type IN
                    ('intent_analysis','vision_analysis','web_research','reasoning_analysis','final_response')),
                provider_profile_id TEXT NOT NULL,
                model_id TEXT NOT NULL,
                protocol_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN
                    ('queued','running','completed','skipped','failed','cancelled','interrupted','unknown_outcome','stale')),
                required INTEGER NOT NULL DEFAULT 1 CHECK(required IN (0,1)),
                activation_condition TEXT NOT NULL DEFAULT 'always'
                    CHECK(activation_condition IN ('always','when_web_required','when_vision_required')),
                skip_reason_code TEXT NULL,
                input_source_ids_json TEXT NOT NULL DEFAULT '[]',
                input_hash TEXT NULL,
                artifact_id TEXT NULL,
                error_code TEXT NULL,
                usage_json TEXT NOT NULL DEFAULT '{}',
                started_at TEXT NULL,
                finished_at TEXT NULL,
                invocation_route_id TEXT NOT NULL DEFAULT 'legacy.primary',
                adapter_id TEXT NOT NULL DEFAULT 'legacy',
                capability_signature_json TEXT NOT NULL DEFAULT '["text"]',
                route_config_fingerprint TEXT NOT NULL DEFAULT 'legacy',
                planned_target_fingerprint TEXT NOT NULL DEFAULT 'legacy-v14',
                UNIQUE(plan_id, order_index, step_type)
            );
            INSERT INTO execution_steps(
                id, plan_id, phase, order_index, step_type, provider_profile_id,
                model_id, protocol_id, status, required, activation_condition,
                input_source_ids_json, input_hash, artifact_id, error_code, usage_json,
                started_at, finished_at, invocation_route_id, adapter_id,
                capability_signature_json, route_config_fingerprint,
                planned_target_fingerprint
            )
            SELECT id, plan_id, phase, order_index, step_type, provider_profile_id,
                   model_id, protocol_id, status, required,
                   CASE WHEN required = 0 AND step_type = 'web_research'
                        THEN 'when_web_required' ELSE 'always' END,
                   input_source_ids_json, input_hash, artifact_id, error_code, usage_json,
                   started_at, finished_at, invocation_route_id, adapter_id,
                   capability_signature_json, route_config_fingerprint,
                   planned_target_fingerprint
              FROM execution_steps_v15;
            DROP TABLE execution_steps_v15;
            CREATE INDEX idx_execution_steps_plan_status
                ON execution_steps(plan_id, status, order_index);
            """
        )

    active = tuple(
        row[0] for row in connection.execute(
            "SELECT id FROM execution_plans WHERE status IN "
            "('resolving','waiting_permission','waiting_budget','planned','executing','synthesizing')"
        ).fetchall()
    )
    if active:
        placeholders = ",".join("?" for _ in active)
        connection.execute(
            f"UPDATE execution_steps SET status='interrupted', error_code='execution_plan_stale' "
            f"WHERE plan_id IN ({placeholders}) AND status IN ('queued','running')",
            active,
        )
        connection.execute(
            f"UPDATE execution_plans SET status='stale' WHERE id IN ({placeholders})", active,
        )

    after = {
        table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in protected
    }
    if before != after:
        raise RuntimeError("v3.1 migration changed protected record counts")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise RuntimeError("v3.1 migration failed foreign-key validation")
