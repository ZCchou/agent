from __future__ import annotations

import sqlite3


def migrate(connection: sqlite3.Connection) -> None:
    """Persist exact invocation routes without rewriting completed v13 audit data."""
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
    for name, declaration in (
        ("route_catalog_hash", "TEXT NOT NULL DEFAULT 'legacy-v13'"),
        ("route_policy_version", "TEXT NOT NULL DEFAULT 'execution-routing-policy-v1'"),
    ):
        if name not in plan_columns:
            connection.execute(f"ALTER TABLE execution_plans ADD COLUMN {name} {declaration}")
    step_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(execution_steps)").fetchall()
    }
    for name, declaration in (
        ("invocation_route_id", "TEXT NOT NULL DEFAULT 'legacy.primary'"),
        ("adapter_id", "TEXT NOT NULL DEFAULT 'legacy'"),
        ("capability_signature_json", "TEXT NOT NULL DEFAULT '[\"text\"]'"),
        ("route_config_fingerprint", "TEXT NOT NULL DEFAULT 'legacy-v13'"),
    ):
        if name not in step_columns:
            connection.execute(f"ALTER TABLE execution_steps ADD COLUMN {name} {declaration}")

    # A v13 active plan did not persist its exact transport route. Replaying it
    # under v14 would be an implicit protocol switch, so fail closed.
    active_plans = tuple(
        row[0] for row in connection.execute(
            "SELECT id FROM execution_plans WHERE status IN "
            "('resolving','waiting_permission','waiting_budget','planned','executing','synthesizing')"
        ).fetchall()
    )
    if active_plans:
        placeholders = ",".join("?" for _ in active_plans)
        connection.execute(
            f"UPDATE execution_steps SET status='interrupted', error_code='invocation_route_stale' "
            f"WHERE plan_id IN ({placeholders}) AND status IN ('queued','running')",
            active_plans,
        )
        connection.execute(
            f"UPDATE execution_plans SET status='stale' WHERE id IN ({placeholders})",
            active_plans,
        )
        connection.execute(
            f"UPDATE model_runs SET status='interrupted', error_code='invocation_route_stale', "
            f"error_message='The previous execution plan did not record an exact invocation route.' "
            f"WHERE execution_plan_id IN ({placeholders}) AND status NOT IN "
            "('completed','failed','cancelled','interrupted')",
            active_plans,
        )

    after = {
        table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in protected
    }
    if before != after:
        raise RuntimeError("invocation routing migration changed protected record counts")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise RuntimeError("invocation routing migration failed foreign-key validation")
