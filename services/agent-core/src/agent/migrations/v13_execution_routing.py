from __future__ import annotations

import sqlite3


def migrate(connection: sqlite3.Connection) -> None:
    """Add immutable execution plans while preserving all v12 business records."""
    protected = tuple(
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' AND name != 'schema_info' ORDER BY name"
        ).fetchall()
    )
    before = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
              for table in protected}

    conversation_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(conversations)").fetchall()
    }
    for name, declaration in (
        ("model_selection_mode", "TEXT NOT NULL DEFAULT 'fixed' CHECK(model_selection_mode IN ('fixed','automatic'))"),
        ("routing_preset", "TEXT NOT NULL DEFAULT 'balanced' CHECK(routing_preset IN ('quality','balanced','economy','local_first'))"),
        ("coordinator_provider_profile_id", "TEXT NULL"),
        ("coordinator_model_id", "TEXT NULL"),
    ):
        if name not in conversation_columns:
            connection.execute(f"ALTER TABLE conversations ADD COLUMN {name} {declaration}")
    connection.execute(
        """UPDATE conversations SET coordinator_provider_profile_id = provider_profile_id,
                  coordinator_model_id = model_id, model_selection_mode = 'fixed'
           WHERE coordinator_provider_profile_id IS NULL AND provider_profile_id IS NOT NULL"""
    )

    existing_tables = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    run_columns = {row[1] for row in connection.execute("PRAGMA table_info(model_runs)").fetchall()}
    if {"execution_plans", "execution_steps", "execution_artifacts"}.issubset(existing_tables) \
            and {"execution_plan_id", "execution_policy_version"}.issubset(run_columns):
        after = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                 for table in protected}
        if before != after:
            raise RuntimeError("execution routing migration changed protected record counts")
        return

    connection.execute("ALTER TABLE model_runs RENAME TO model_runs_v12")
    connection.executescript(
        """
        CREATE TABLE model_runs (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            user_message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            assistant_message_id TEXT NULL REFERENCES messages(id) ON DELETE SET NULL,
            status TEXT NOT NULL CHECK(status IN
                ('queued','building_context','waiting_context','waiting_permission','waiting_budget',
                 'generating','waiting_tool','completed','failed','cancelled','interrupted')),
            idempotency_key TEXT NULL,
            provider_profile_id TEXT NOT NULL,
            model_id TEXT NOT NULL,
            initiator_kind TEXT NOT NULL DEFAULT 'user'
                CHECK(initiator_kind IN ('user','system','agent')),
            parent_run_id TEXT NULL REFERENCES model_runs(id) ON DELETE SET NULL,
            context_snapshot_id TEXT NULL,
            error_code TEXT NULL,
            error_message TEXT NULL,
            created_at TEXT NOT NULL,
            started_at TEXT NULL,
            finished_at TEXT NULL,
            selection_source TEXT NOT NULL DEFAULT 'global_default'
                CHECK(selection_source IN ('global_default','conversation_override','user_once','router')),
            selection_reason_code TEXT NOT NULL DEFAULT 'fixed_selection',
            routing_policy_version TEXT NULL,
            context_strategy TEXT NOT NULL DEFAULT 'default'
                CHECK(context_strategy IN ('default','expanded_retry')),
            context_generation INTEGER NOT NULL DEFAULT 0,
            automatic_compaction_used INTEGER NOT NULL DEFAULT 0 CHECK(automatic_compaction_used IN (0,1)),
            web_search_request_mode TEXT NOT NULL DEFAULT 'off'
                CHECK(web_search_request_mode IN ('off','auto','force_once')),
            web_search_route TEXT NOT NULL DEFAULT 'none'
                CHECK(web_search_route IN ('none','native','host_tool','host_presearch','specialist')),
            web_search_reason_code TEXT NOT NULL DEFAULT 'web_search_off',
            web_search_policy_version TEXT NULL,
            execution_plan_id TEXT NULL,
            execution_policy_version TEXT NULL
        );
        INSERT INTO model_runs(
            id, conversation_id, user_message_id, assistant_message_id, status, idempotency_key,
            provider_profile_id, model_id, initiator_kind, parent_run_id, context_snapshot_id,
            error_code, error_message, created_at, started_at, finished_at, selection_source,
            selection_reason_code, routing_policy_version, context_strategy, context_generation,
            automatic_compaction_used, web_search_request_mode, web_search_route,
            web_search_reason_code, web_search_policy_version
        ) SELECT
            id, conversation_id, user_message_id, assistant_message_id, status, idempotency_key,
            provider_profile_id, model_id, initiator_kind, parent_run_id, context_snapshot_id,
            error_code, error_message, created_at, started_at, finished_at, selection_source,
            selection_reason_code, routing_policy_version, context_strategy, context_generation,
            automatic_compaction_used, web_search_request_mode, web_search_route,
            web_search_reason_code, web_search_policy_version
          FROM model_runs_v12;
        DROP TABLE model_runs_v12;

        CREATE TABLE execution_plans (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL UNIQUE REFERENCES model_runs(id) ON DELETE CASCADE,
            status TEXT NOT NULL CHECK(status IN
                ('resolving','waiting_permission','waiting_budget','planned','executing',
                 'synthesizing','completed','failed','cancelled','interrupted','stale')),
            selection_mode TEXT NOT NULL CHECK(selection_mode IN ('fixed','automatic')),
            routing_preset TEXT NOT NULL CHECK(routing_preset IN ('quality','balanced','economy','local_first')),
            policy_version TEXT NOT NULL,
            requirements_json TEXT NOT NULL,
            requirements_hash TEXT NOT NULL,
            coordinator_provider_profile_id TEXT NOT NULL,
            coordinator_model_id TEXT NOT NULL,
            generation_protocol TEXT NOT NULL,
            web_route TEXT NOT NULL,
            web_adapter_or_backend_id TEXT NULL,
            reasoning_effort TEXT NOT NULL CHECK(reasoning_effort IN ('none','low','medium','high')),
            selection_source TEXT NOT NULL,
            selection_reason_code TEXT NOT NULL,
            budget_json TEXT NOT NULL,
            estimated_extra_online_cost_usd TEXT NULL,
            plan_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
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
                ('queued','running','completed','failed','cancelled','interrupted','unknown_outcome')),
            required INTEGER NOT NULL DEFAULT 1 CHECK(required IN (0,1)),
            input_source_ids_json TEXT NOT NULL DEFAULT '[]',
            input_hash TEXT NULL,
            artifact_id TEXT NULL,
            error_code TEXT NULL,
            usage_json TEXT NOT NULL DEFAULT '{}',
            started_at TEXT NULL,
            finished_at TEXT NULL,
            UNIQUE(plan_id, order_index, step_type)
        );
        CREATE TABLE execution_artifacts (
            id TEXT PRIMARY KEY,
            step_id TEXT NOT NULL UNIQUE REFERENCES execution_steps(id) ON DELETE CASCADE,
            artifact_type TEXT NOT NULL,
            content_json TEXT NOT NULL,
            trust TEXT NOT NULL CHECK(trust IN ('external_untrusted','model_inference','system_observation')),
            derived_from_untrusted INTEGER NOT NULL DEFAULT 0 CHECK(derived_from_untrusted IN (0,1)),
            source_refs_json TEXT NOT NULL DEFAULT '[]',
            content_hash TEXT NOT NULL,
            byte_size INTEGER NOT NULL CHECK(byte_size BETWEEN 0 AND 65536),
            created_at TEXT NOT NULL
        );
        """
    )
    connection.execute("ALTER TABLE run_attempts ADD COLUMN execution_step_id TEXT NULL")
    connection.execute(
        "CREATE INDEX idx_execution_steps_plan_status ON execution_steps(plan_id, status, order_index)"
    )
    connection.execute(
        "CREATE INDEX idx_execution_plans_status ON execution_plans(status, updated_at)"
    )

    after = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
             for table in protected}
    if before != after:
        raise RuntimeError("execution routing migration changed protected record counts")
    invalid_existing = connection.execute(
        "SELECT COUNT(*) FROM conversations WHERE model_selection_mode != 'fixed'"
    ).fetchone()[0]
    if invalid_existing:
        raise RuntimeError("execution routing migration changed existing conversation behavior")
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError("execution routing migration failed foreign-key validation")
