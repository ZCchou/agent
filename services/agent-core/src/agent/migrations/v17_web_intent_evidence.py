from __future__ import annotations

import sqlite3


def migrate(connection: sqlite3.Connection) -> None:
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

    operation_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(web_operations)").fetchall()
    }
    if "evidence_status" not in operation_columns:
        connection.execute(
            "ALTER TABLE web_operations ADD COLUMN evidence_status "
            "TEXT NOT NULL DEFAULT 'not_evaluated'"
        )
    if "verified_source_count" not in operation_columns:
        connection.execute(
            "ALTER TABLE web_operations ADD COLUMN verified_source_count "
            "INTEGER NOT NULL DEFAULT 0 CHECK(verified_source_count >= 0)"
        )
    if "evidence_manifest_hash" not in operation_columns:
        connection.execute(
            "ALTER TABLE web_operations ADD COLUMN evidence_manifest_hash TEXT NULL"
        )
    if "provider_reference_id" not in operation_columns:
        connection.execute(
            "ALTER TABLE web_operations ADD COLUMN provider_reference_id TEXT NULL"
        )
    connection.execute(
        """UPDATE web_operations
              SET evidence_status = CASE
                    WHEN status != 'completed' THEN 'not_evaluated'
                    WHEN result_count > 0 THEN 'traceable_sources'
                    ELSE 'legacy_unknown'
                  END,
                  verified_source_count = result_count"""
    )

    run_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(model_runs)").fetchall()
    }
    if "web_grounding_status" not in run_columns:
        connection.execute(
            "ALTER TABLE model_runs ADD COLUMN web_grounding_status "
            "TEXT NOT NULL DEFAULT 'not_required'"
        )
    connection.execute(
        """UPDATE model_runs
              SET web_grounding_status = CASE
                    WHEN web_search_route = 'none' THEN 'not_required'
                    WHEN EXISTS (
                        SELECT 1 FROM web_operations o
                         WHERE o.run_id = model_runs.id AND o.result_count > 0
                    ) THEN 'grounded'
                    ELSE 'failed'
                  END"""
    )

    step_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(execution_steps)").fetchall()
    }
    if step_columns and "activation_condition" in step_columns:
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='execution_steps'"
        ).fetchone()[0]
        if "grounding_fallback" not in sql:
            connection.executescript(
                """
                ALTER TABLE execution_steps RENAME TO execution_steps_v16;
                CREATE TABLE execution_steps (
                    id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL REFERENCES execution_plans(id) ON DELETE CASCADE,
                    phase TEXT NOT NULL CHECK(phase IN ('analysis','specialist','synthesis')),
                    order_index INTEGER NOT NULL CHECK(order_index >= 0),
                    step_type TEXT NOT NULL CHECK(step_type IN
                        ('intent_analysis','vision_analysis','web_research','reasoning_analysis',
                         'final_response','grounding_fallback')),
                    provider_profile_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    protocol_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN
                        ('queued','running','completed','skipped','failed','cancelled','interrupted',
                         'unknown_outcome','stale')),
                    required INTEGER NOT NULL DEFAULT 1 CHECK(required IN (0,1)),
                    activation_condition TEXT NOT NULL DEFAULT 'always'
                        CHECK(activation_condition IN
                            ('always','when_web_required','when_vision_required',
                             'when_web_evidence_missing')),
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
                INSERT INTO execution_steps
                SELECT * FROM execution_steps_v16;
                DROP TABLE execution_steps_v16;
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
        raise RuntimeError("v17 migration changed protected record counts")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise RuntimeError("v17 migration failed foreign-key validation")
