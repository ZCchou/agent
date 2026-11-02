from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def migrate(connection: sqlite3.Connection) -> None:
    """Upgrade schema v8 to the auditable Context Engine v2 schema.

    The caller owns the transaction and must disable foreign-key rewriting while
    the two constrained tables are rebuilt.
    """
    protected = ("projects", "conversations", "messages", "model_runs")
    before = {
        table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in protected
    }

    connection.execute("ALTER TABLE conversations ADD COLUMN context_generation INTEGER NOT NULL DEFAULT 0")
    connection.execute("ALTER TABLE messages ADD COLUMN sequence_no INTEGER NULL")
    conversation_ids = connection.execute("SELECT id FROM conversations ORDER BY id").fetchall()
    for conversation in conversation_ids:
        rows = connection.execute(
            "SELECT id FROM messages WHERE conversation_id = ? ORDER BY created_at, rowid",
            (conversation["id"],),
        ).fetchall()
        for sequence, row in enumerate(rows, start=1):
            connection.execute(
                "UPDATE messages SET sequence_no = ? WHERE id = ?", (sequence, row["id"]),
            )
        connection.execute(
            "UPDATE conversations SET context_generation = ? WHERE id = ?",
            (len(rows), conversation["id"]),
        )

    connection.execute("ALTER TABLE model_runs RENAME TO model_runs_v8")
    connection.executescript(
        """
        CREATE TABLE model_runs (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            user_message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            assistant_message_id TEXT NULL REFERENCES messages(id) ON DELETE SET NULL,
            status TEXT NOT NULL CHECK(status IN
                ('queued','building_context','waiting_context','generating','waiting_tool',
                 'completed','failed','cancelled','interrupted')),
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
            automatic_compaction_used INTEGER NOT NULL DEFAULT 0
                CHECK(automatic_compaction_used IN (0,1))
        );
        INSERT INTO model_runs(
            id, conversation_id, user_message_id, assistant_message_id, status,
            idempotency_key, provider_profile_id, model_id, initiator_kind,
            parent_run_id, context_snapshot_id, error_code, error_message,
            created_at, started_at, finished_at, selection_source,
            selection_reason_code, routing_policy_version, context_strategy,
            context_generation, automatic_compaction_used
        ) SELECT
            r.id, r.conversation_id, r.user_message_id, r.assistant_message_id, r.status,
            r.idempotency_key, r.provider_profile_id, r.model_id, r.initiator_kind,
            r.parent_run_id, r.context_snapshot_id, r.error_code, r.error_message,
            r.created_at, r.started_at, r.finished_at, r.selection_source,
            r.selection_reason_code, r.routing_policy_version, 'default',
            c.context_generation, 0
          FROM model_runs_v8 r JOIN conversations c ON c.id = r.conversation_id;
        DROP TABLE model_runs_v8;
        """
    )

    connection.execute("ALTER TABLE context_snapshots RENAME TO context_snapshots_v8")
    connection.executescript(
        """
        CREATE TABLE context_snapshots (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES model_runs(id) ON DELETE CASCADE,
            round_no INTEGER NOT NULL DEFAULT 0 CHECK(round_no >= 0),
            assembler_version TEXT NOT NULL,
            policy_version TEXT NOT NULL DEFAULT 'legacy-v1',
            policy_json TEXT NOT NULL DEFAULT '{}',
            context_generation INTEGER NOT NULL DEFAULT 0,
            context_window INTEGER NOT NULL,
            input_target INTEGER NOT NULL,
            output_reserve INTEGER NOT NULL DEFAULT 0,
            token_method TEXT NOT NULL DEFAULT 'estimate-v1',
            estimated_tokens INTEGER NOT NULL,
            stable_prefix_hash TEXT NOT NULL DEFAULT '',
            dynamic_suffix_hash TEXT NOT NULL DEFAULT '',
            content_hash TEXT NOT NULL,
            retrieval_confidence TEXT NOT NULL DEFAULT 'none'
                CHECK(retrieval_confidence IN ('high','medium','low','none')),
            compression_applied INTEGER NOT NULL DEFAULT 0 CHECK(compression_applied IN (0,1)),
            omitted_items INTEGER NOT NULL DEFAULT 0 CHECK(omitted_items >= 0),
            created_at TEXT NOT NULL,
            UNIQUE(run_id, round_no)
        );
        INSERT INTO context_snapshots(
            id, run_id, round_no, assembler_version, policy_version, policy_json,
            context_generation, context_window, input_target, output_reserve,
            token_method, estimated_tokens, stable_prefix_hash, dynamic_suffix_hash,
            content_hash, retrieval_confidence, compression_applied, omitted_items, created_at
        ) SELECT
            id, run_id, 0, assembler_version, 'legacy-v1', '{}', 0,
            token_budget, token_budget, 0, 'estimate-v1', estimated_tokens,
            '', '', content_hash, 'none', 0, 0, created_at
          FROM context_snapshots_v8;
        DROP TABLE context_snapshots_v8;
        """
    )

    for statement in (
        "ALTER TABLE context_items ADD COLUMN section TEXT NOT NULL DEFAULT 'dynamic'",
        "ALTER TABLE context_items ADD COLUMN representation TEXT NOT NULL DEFAULT 'original'",
        "ALTER TABLE context_items ADD COLUMN trust TEXT NOT NULL DEFAULT 'user_authored'",
        "ALTER TABLE context_items ADD COLUMN derived_from_untrusted INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE context_items ADD COLUMN source_start INTEGER NULL",
        "ALTER TABLE context_items ADD COLUMN source_end INTEGER NULL",
        "ALTER TABLE context_items ADD COLUMN source_hash TEXT NULL",
        "ALTER TABLE context_items ADD COLUMN source_version TEXT NULL",
        "ALTER TABLE context_items ADD COLUMN inclusion_reason TEXT NULL",
        "ALTER TABLE context_items ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'",
        "ALTER TABLE run_attempts ADD COLUMN input_tokens INTEGER NULL",
        "ALTER TABLE run_attempts ADD COLUMN output_tokens INTEGER NULL",
        "ALTER TABLE run_attempts ADD COLUMN cached_input_tokens INTEGER NULL",
        "ALTER TABLE run_attempts ADD COLUMN estimated_cost REAL NULL",
    ):
        connection.execute(statement)

    connection.executescript(
        """
        CREATE TABLE context_checkpoints (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            parent_checkpoint_id TEXT NULL REFERENCES context_checkpoints(id) ON DELETE SET NULL,
            start_sequence INTEGER NOT NULL CHECK(start_sequence >= 1),
            start_offset INTEGER NOT NULL DEFAULT 0 CHECK(start_offset >= 0),
            through_sequence INTEGER NOT NULL CHECK(through_sequence >= start_sequence),
            through_offset INTEGER NULL CHECK(through_offset IS NULL OR through_offset >= 0),
            segmenter_version TEXT NOT NULL DEFAULT 'paragraph-unicode-v1',
            content_json TEXT NOT NULL,
            source_hash TEXT NOT NULL,
            provider_profile_id TEXT NOT NULL,
            model_id TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('valid','legacy')),
            input_tokens INTEGER NULL,
            output_tokens INTEGER NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE context_compaction_attempts (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            run_id TEXT NULL REFERENCES model_runs(id) ON DELETE SET NULL,
            checkpoint_id TEXT NULL REFERENCES context_checkpoints(id) ON DELETE SET NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            context_generation INTEGER NOT NULL,
            source_hash TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN
                ('queued','running','valid','failed','interrupted','stale')),
            input_tokens INTEGER NULL,
            output_tokens INTEGER NULL,
            estimated_cost REAL NULL,
            error_code TEXT NULL,
            created_at TEXT NOT NULL,
            finished_at TEXT NULL
        );
        """
    )

    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversation_summaries'"
    ).fetchone() is not None:
        summaries = connection.execute(
            "SELECT * FROM conversation_summaries ORDER BY conversation_id, version, created_at"
        ).fetchall()
        parent_by_conversation: dict[str, str] = {}
        for summary in summaries:
            message = connection.execute(
                "SELECT sequence_no, length(content) AS content_length FROM messages WHERE id = ?",
                (summary["through_message_id"],),
            ).fetchone()
            if message is None or message["sequence_no"] is None:
                continue
            checkpoint_id = str(uuid.uuid4())
            content_json = json.dumps(
                {"legacy_text": summary["summary"], "legacy_summary_id": summary["id"]},
                ensure_ascii=True,
                separators=(",", ":"),
            )
            connection.execute(
                """INSERT INTO context_checkpoints(
                       id, conversation_id, parent_checkpoint_id, start_sequence, start_offset,
                       through_sequence, through_offset, content_json, source_hash,
                       provider_profile_id, model_id, policy_version, status, created_at
                   ) VALUES (?, ?, ?, 1, 0, ?, ?, ?, ?, ?, ?, 'legacy-v1', 'legacy', ?)""",
                (
                    checkpoint_id,
                    summary["conversation_id"],
                    parent_by_conversation.get(summary["conversation_id"]),
                    message["sequence_no"],
                    message["content_length"],
                    content_json,
                    f"legacy:{summary['id']}",
                    summary["provider_profile_id"],
                    summary["model_id"],
                    summary["created_at"],
                ),
            )
            parent_by_conversation[summary["conversation_id"]] = checkpoint_id
        connection.execute("ALTER TABLE conversation_summaries RENAME TO conversation_summaries_legacy_v8")

    after = {
        table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in protected
    }
    if before != after:
        raise RuntimeError("Context Engine migration changed protected record counts")
    if connection.execute("SELECT 1 FROM messages WHERE sequence_no IS NULL LIMIT 1").fetchone():
        raise RuntimeError("Context Engine migration left messages without sequence numbers")
    if connection.execute(
        """SELECT 1 FROM messages GROUP BY conversation_id, sequence_no
           HAVING COUNT(*) > 1 LIMIT 1"""
    ).fetchone():
        raise RuntimeError("Context Engine migration created duplicate message sequence numbers")
