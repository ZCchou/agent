from __future__ import annotations

import json
import hashlib
import ipaddress
import math
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from agent.behavior import BEHAVIOR_POLICIES
from agent.migrations.v09_context_engine import migrate as migrate_v09_context_engine
from agent.migrations.v10_web_search import migrate as migrate_v10_web_search
from agent.migrations.v11_web_search_usability import migrate as migrate_v11_web_search_usability
from agent.migrations.v12_multimodal_images import migrate as migrate_v12_multimodal_images
from agent.migrations.v13_execution_routing import migrate as migrate_v13_execution_routing
from agent.migrations.v14_invocation_routing import migrate as migrate_v14_invocation_routing
from agent.migrations.v15_unified_invocation import migrate as migrate_v15_unified_invocation
from agent.migrations.v16_unified_invocation_v31 import migrate as migrate_v16_unified_invocation_v31
from agent.migrations.v17_web_intent_evidence import migrate as migrate_v17_web_intent_evidence
from agent.migrations.v18_capability_contract import migrate as migrate_v18_capability_contract
from agent.migrations.v19_pending_follow_up_actions import migrate as migrate_v19_pending_follow_up_actions
from agent.migrations.v20_global_instructions import migrate as migrate_v20_global_instructions
from agent.migrations.v21_reliability import migrate as migrate_v21_reliability
from agent.global_instructions import (
    DEFAULT_GLOBAL_INSTRUCTIONS,
    DEFAULT_GLOBAL_INSTRUCTIONS_VERSION,
    MAX_GLOBAL_INSTRUCTION_CHARS,
)
from agent.web_search.domain import WEB_OPERATION_TRANSITIONS, WebOperationStatus

SCHEMA_VERSION = 23
TRASH_RETENTION_DAYS = 30
CATEGORIES = {"casual", "research", "work", "writing", "coding", "gaming", "other"}
CONVERSATION_KINDS = {"standard_chat", "project_work"}
DATABASE_BUSY_TIMEOUT_MS = 5_000


class CoreDataError(Exception):
    def __init__(
        self, code: str, message: str, details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _uuid() -> str:
    return str(uuid.uuid4())


def _open_database(database_path: Path) -> sqlite3.Connection:
    """Open one consistently configured SQLite connection.

    The IPC reader serves read requests on worker threads while model runs write
    from the asyncio thread.  WAL prevents those short reads from blocking the
    final assistant-message commit.
    """
    connection = sqlite3.connect(
        database_path,
        timeout=DATABASE_BUSY_TIMEOUT_MS / 1_000,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {DATABASE_BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA synchronous = NORMAL")
    return connection


def _name(value: str, code: str, label: str) -> str:
    normalized = value.strip()
    if not 1 <= len(normalized) <= 80:
        raise CoreDataError(code, f"{label} must contain 1 to 80 characters.")
    return normalized


def _category(value: str) -> str:
    if value not in CATEGORIES:
        raise CoreDataError("invalid_category", "Category is not supported.")
    return value


def _validate_ids(ordered_ids: list[str]) -> None:
    if any(not isinstance(value, str) or not value for value in ordered_ids):
        raise CoreDataError("invalid_order", "Ordered ids must contain non-empty strings.")
    if len(ordered_ids) != len(set(ordered_ids)):
        raise CoreDataError("invalid_order", "Ordered ids cannot contain duplicates.")


class CoreRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)
        self.removed_legacy_backups = 0
        self.legacy_backup_cleanup_failures = 0
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return _open_database(self.database_path)

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("CREATE TABLE IF NOT EXISTS schema_info (version INTEGER NOT NULL)")
            rows = connection.execute("SELECT version FROM schema_info").fetchall()
            if len(rows) > 1:
                raise CoreDataError("unsupported_schema", "The local data schema is invalid.")
            if not rows:
                self._create_schema_v4(connection)
                connection.execute("INSERT INTO schema_info(version) VALUES (4)")
                connection.commit()
                version = 4
            else:
                version = rows[0]["version"]
            if version < 2 or version > SCHEMA_VERSION:
                raise CoreDataError("unsupported_schema", "The local data schema is not supported and was not changed.")
            if rows and version < SCHEMA_VERSION:
                # SQLite's backup API includes committed WAL pages. Unique names
                # retain prior recovery points; startup never restores over data.
                backup_path = self.database_path.with_name(
                    f"{self.database_path.name}.pre-schema{SCHEMA_VERSION}-{uuid.uuid4().hex}.bak"
                )
                with sqlite3.connect(backup_path) as backup:
                    connection.backup(backup)
            if version == 2:
                self._migrate_v2_to_v3(connection)
                connection.execute("UPDATE schema_info SET version = 3")
                connection.commit()
                version = 3
            if version == 3:
                # Rebuilding a referenced SQLite table requires foreign keys to be
                # disabled before the migration transaction begins.
                connection.commit()
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute("PRAGMA legacy_alter_table = ON")
                connection.execute("BEGIN IMMEDIATE")
                try:
                    self._migrate_v3_to_v4(connection)
                    connection.execute("UPDATE schema_info SET version = 4")
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
                finally:
                    connection.execute("PRAGMA legacy_alter_table = OFF")
                    connection.execute("PRAGMA foreign_keys = ON")
                version = 4
            if version == 4:
                self._migrate_v4_to_v5(connection)
                connection.execute("UPDATE schema_info SET version = 5")
                connection.commit()
                version = 5
            if version == 5:
                self._migrate_v5_to_v6(connection)
                connection.execute("UPDATE schema_info SET version = 6")
                connection.commit()
                version = 6
            if version == 6:
                connection.commit()
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute("PRAGMA legacy_alter_table = ON")
                connection.execute("BEGIN IMMEDIATE")
                try:
                    self._migrate_v6_to_v7(connection)
                    connection.execute("UPDATE schema_info SET version = 7")
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
                finally:
                    connection.execute("PRAGMA legacy_alter_table = OFF")
                    connection.execute("PRAGMA foreign_keys = ON")
                version = 7
            if version == 7:
                self._migrate_v7_to_v8(connection)
                connection.execute("UPDATE schema_info SET version = 8")
                connection.commit()
                version = 8
            if version == 8:
                connection.commit()
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute("PRAGMA legacy_alter_table = ON")
                connection.execute("BEGIN IMMEDIATE")
                try:
                    migrate_v09_context_engine(connection)
                    connection.execute("UPDATE schema_info SET version = 9")
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
                finally:
                    connection.execute("PRAGMA legacy_alter_table = OFF")
                    connection.execute("PRAGMA foreign_keys = ON")
                version = 9
            if version == 9:
                migrate_v10_web_search(connection)
                connection.execute("UPDATE schema_info SET version = 10")
                connection.commit()
                version = 10
            if version == 10:
                connection.commit()
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute("PRAGMA legacy_alter_table = ON")
                connection.execute("BEGIN IMMEDIATE")
                try:
                    migrate_v11_web_search_usability(connection)
                    connection.execute("UPDATE schema_info SET version = 11")
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
                finally:
                    connection.execute("PRAGMA legacy_alter_table = OFF")
                    connection.execute("PRAGMA foreign_keys = ON")
                version = 11
            if version == 11:
                migrate_v12_multimodal_images(connection)
                connection.execute("UPDATE schema_info SET version = 12")
                connection.commit()
                version = 12
            if version == 12:
                connection.commit()
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute("PRAGMA legacy_alter_table = ON")
                connection.execute("BEGIN IMMEDIATE")
                try:
                    migrate_v13_execution_routing(connection)
                    connection.execute("UPDATE schema_info SET version = 13")
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
                finally:
                    connection.execute("PRAGMA legacy_alter_table = OFF")
                    connection.execute("PRAGMA foreign_keys = ON")
                version = 13
            if version == 13:
                migrate_v14_invocation_routing(connection)
                connection.execute("UPDATE schema_info SET version = 14")
                connection.commit()
                version = 14
            if version == 14:
                connection.commit()
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute("PRAGMA legacy_alter_table = ON")
                connection.execute("BEGIN IMMEDIATE")
                try:
                    migrate_v15_unified_invocation(connection)
                    connection.execute("UPDATE schema_info SET version = 15")
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
                finally:
                    connection.execute("PRAGMA legacy_alter_table = OFF")
                    connection.execute("PRAGMA foreign_keys = ON")
                version = 15
            if version == 15:
                connection.commit()
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute("PRAGMA legacy_alter_table = ON")
                connection.execute("BEGIN IMMEDIATE")
                try:
                    migrate_v16_unified_invocation_v31(connection)
                    connection.execute("UPDATE schema_info SET version = 16")
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
                finally:
                    connection.execute("PRAGMA legacy_alter_table = OFF")
                    connection.execute("PRAGMA foreign_keys = ON")
                version = 16
            if version == 16:
                connection.commit()
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute("PRAGMA legacy_alter_table = ON")
                connection.execute("BEGIN IMMEDIATE")
                try:
                    migrate_v17_web_intent_evidence(connection)
                    connection.execute("UPDATE schema_info SET version = 17")
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
                finally:
                    connection.execute("PRAGMA legacy_alter_table = OFF")
                    connection.execute("PRAGMA foreign_keys = ON")
                version = 17
            if version == 17:
                migrate_v18_capability_contract(connection)
                connection.execute("UPDATE schema_info SET version = 18")
                connection.commit()
                version = 18
            if version == 18:
                migrate_v19_pending_follow_up_actions(connection)
                connection.execute("UPDATE schema_info SET version = 19")
                connection.commit()
                version = 19
            if version == 19:
                migrate_v20_global_instructions(connection)
                connection.execute("UPDATE schema_info SET version = 20")
                connection.commit()
                version = 20
            if version == 20:
                connection.execute("BEGIN IMMEDIATE")
                migrate_v21_reliability(connection)
                connection.execute("UPDATE schema_info SET version = 21")
                self._validate_database(connection)
                connection.commit()
                version = 21
            if version == 21:
                from agent.migrations.v22_terminal import migrate
                connection.commit()
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute("BEGIN IMMEDIATE")
                try:
                    migrate(connection)
                    connection.execute("UPDATE schema_info SET version = 22")
                    self._validate_database(connection)
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
                finally:
                    connection.execute("PRAGMA foreign_keys = ON")
                version = 22
            if version == 22:
                from agent.migrations.v23_skills import migrate as migrate_skills
                connection.commit()
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute("BEGIN IMMEDIATE")
                try:
                    migrate_skills(connection)
                    connection.execute("UPDATE schema_info SET version = 23")
                    self._validate_database(connection)
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
                finally:
                    connection.execute("PRAGMA foreign_keys = ON")
            self._create_indexes(connection)
            self._reconcile_terminal_web_operations(connection)
            self._validate_database(connection)
        self._remove_legacy_backups()

    @staticmethod
    def _reconcile_terminal_web_operations(connection: sqlite3.Connection) -> None:
        tables = {
            row["name"] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if not {"model_runs", "web_operations"}.issubset(tables):
            return
        rows = connection.execute(
            """SELECT o.id, r.status AS run_status, r.error_code AS run_error
               FROM web_operations o JOIN model_runs r ON r.id = o.run_id
               WHERE o.status IN ('prepared','dispatched','searching','reading_sources')
                 AND r.status IN ('completed','failed','cancelled','interrupted')"""
        ).fetchall()
        now = _utc_now()
        for row in rows:
            if row["run_status"] == "failed":
                status = "failed"
                error_code = row["run_error"] or "search_unknown_outcome"
            elif row["run_status"] == "cancelled":
                status, error_code = "cancelled", "cancelled_by_user"
            else:
                status, error_code = "unknown_outcome", "search_unknown_outcome"
            connection.execute(
                """UPDATE web_operations SET status = ?, error_code = ?, finished_at = ?
                   WHERE id = ?""",
                (status, error_code, now, row["id"]),
            )

    @staticmethod
    def _create_schema_v4(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS chat_groups (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, sort_order INTEGER NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, sort_order INTEGER NOT NULL,
                category TEXT NOT NULL CHECK(category IN
                    ('casual','research','work','writing','coding','gaming','other')),
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, deleted_at TEXT NULL
            );
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL CHECK(kind IN ('standard_chat','project_work')),
                title TEXT NOT NULL,
                category TEXT NULL CHECK(category IS NULL OR category IN
                    ('casual','research','work','writing','coding','gaming','other')),
                chat_group_id TEXT NULL REFERENCES chat_groups(id) ON DELETE SET NULL,
                project_id TEXT NULL REFERENCES projects(id) ON DELETE CASCADE,
                sort_order INTEGER NOT NULL,
                derived_from_conversation_id TEXT NULL REFERENCES conversations(id) ON DELETE SET NULL,
                derived_through_message_id TEXT NULL,
                derived_at TEXT NULL,
                copied_from_conversation_id TEXT NULL REFERENCES conversations(id) ON DELETE SET NULL,
                copied_through_message_id TEXT NULL,
                copied_at TEXT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                deleted_at TEXT NULL,
                CHECK(
                    (kind = 'standard_chat' AND category IS NOT NULL AND project_id IS NULL)
                    OR (kind = 'project_work' AND category IS NULL AND project_id IS NOT NULL
                        AND chat_group_id IS NULL)
                )
            );
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK(role IN ('user','assistant','system_event')),
                content TEXT NOT NULL,
                event_type TEXT NULL,
                source_message_id TEXT NULL REFERENCES messages(id) ON DELETE SET NULL,
                created_at TEXT NOT NULL
            );
            """
        )

    @staticmethod
    def _create_indexes(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_chat_groups_order ON chat_groups(sort_order);
            CREATE INDEX IF NOT EXISTS idx_projects_order ON projects(sort_order);
            CREATE INDEX IF NOT EXISTS idx_conversations_kind_order ON conversations(kind, sort_order);
            CREATE INDEX IF NOT EXISTS idx_conversations_group_order ON conversations(chat_group_id, sort_order);
            CREATE INDEX IF NOT EXISTS idx_conversations_project_order ON conversations(project_id, sort_order);
            CREATE INDEX IF NOT EXISTS idx_messages_conversation_created ON messages(conversation_id, created_at);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_conversation_sequence
                ON messages(conversation_id, sequence_no);
            CREATE INDEX IF NOT EXISTS idx_projects_deleted ON projects(deleted_at);
            CREATE INDEX IF NOT EXISTS idx_conversations_deleted ON conversations(deleted_at);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_model_runs_idempotency
                ON model_runs(idempotency_key) WHERE idempotency_key IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_model_runs_conversation ON model_runs(conversation_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_model_runs_status ON model_runs(status);
            CREATE INDEX IF NOT EXISTS idx_context_items_snapshot ON context_items(snapshot_id, position);
            CREATE INDEX IF NOT EXISTS idx_project_knowledge_project
                ON project_knowledge(project_id, status, pinned, updated_at);
            CREATE INDEX IF NOT EXISTS idx_tool_operations_run
                ON tool_operations(run_id, sequence);
            CREATE INDEX IF NOT EXISTS idx_tool_operations_status
                ON tool_operations(status);
            CREATE INDEX IF NOT EXISTS idx_tool_reviews_operation
                ON tool_reviews(operation_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_message_attachments_message
                ON message_attachments(message_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_message_attachments_storage
                ON message_attachments(storage_key);
            CREATE INDEX IF NOT EXISTS idx_context_snapshots_run_round
                ON context_snapshots(run_id, round_no);
            CREATE INDEX IF NOT EXISTS idx_context_checkpoints_conversation
                ON context_checkpoints(conversation_id, through_sequence, through_offset);
            CREATE INDEX IF NOT EXISTS idx_context_compaction_conversation
                ON context_compaction_attempts(conversation_id, status, created_at);
            CREATE INDEX IF NOT EXISTS idx_web_operations_run
                ON web_operations(run_id, sequence);
            CREATE INDEX IF NOT EXISTS idx_web_operations_status
                ON web_operations(status, created_at);
            CREATE INDEX IF NOT EXISTS idx_web_sources_operation
                ON web_sources(operation_id, position);
            """
        )

    @staticmethod
    def _migrate_v7_to_v8(connection: sqlite3.Connection) -> None:
        """Add immutable user-message attachment metadata without rewriting history."""
        protected = ("projects", "conversations", "messages", "model_runs")
        before = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in protected
        }
        connection.executescript(
            """
            CREATE TABLE message_attachments (
                id TEXT PRIMARY KEY,
                message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                kind TEXT NOT NULL CHECK(kind IN ('image','text')),
                name TEXT NOT NULL,
                media_type TEXT NOT NULL,
                byte_size INTEGER NOT NULL CHECK(byte_size >= 0),
                sha256 TEXT NOT NULL,
                storage_key TEXT NOT NULL,
                width INTEGER NULL,
                height INTEGER NULL,
                source_attachment_id TEXT NULL REFERENCES message_attachments(id) ON DELETE SET NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        after = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in protected
        }
        if before != after:
            raise CoreDataError("migration_failed", "The attachment migration changed existing record counts.")

    @staticmethod
    def _migrate_v4_to_v5(connection: sqlite3.Connection) -> None:
        """Add reliable model runs and explicitly shared project knowledge."""
        before = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("projects", "conversations", "messages")
        }
        connection.executescript(
            """
            ALTER TABLE conversations ADD COLUMN provider_profile_id TEXT NULL;
            ALTER TABLE conversations ADD COLUMN model_id TEXT NULL;
            ALTER TABLE projects ADD COLUMN model_access_policy TEXT NOT NULL DEFAULT 'local_only'
                CHECK(model_access_policy IN ('local_only','allow_online'));

            CREATE TABLE model_runs (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                user_message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                assistant_message_id TEXT NULL REFERENCES messages(id) ON DELETE SET NULL,
                status TEXT NOT NULL CHECK(status IN
                    ('queued','building_context','generating','completed','failed','cancelled','interrupted')),
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
                finished_at TEXT NULL
            );
            CREATE TABLE run_attempts (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES model_runs(id) ON DELETE CASCADE,
                attempt_number INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NULL,
                produced_output INTEGER NOT NULL DEFAULT 0 CHECK(produced_output IN (0,1)),
                error_code TEXT NULL,
                UNIQUE(run_id, attempt_number)
            );
            CREATE TABLE assistant_drafts (
                run_id TEXT PRIMARY KEY REFERENCES model_runs(id) ON DELETE CASCADE,
                attempt_id TEXT NOT NULL REFERENCES run_attempts(id) ON DELETE CASCADE,
                content TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE context_snapshots (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL UNIQUE REFERENCES model_runs(id) ON DELETE CASCADE,
                assembler_version TEXT NOT NULL,
                token_budget INTEGER NOT NULL,
                estimated_tokens INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE context_items (
                id TEXT PRIMARY KEY,
                snapshot_id TEXT NOT NULL REFERENCES context_snapshots(id) ON DELETE CASCADE,
                source_type TEXT NOT NULL,
                source_id TEXT NULL,
                position INTEGER NOT NULL,
                estimated_tokens INTEGER NOT NULL,
                included INTEGER NOT NULL CHECK(included IN (0,1)),
                exclusion_reason TEXT NULL
            );
            CREATE TABLE conversation_summaries (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                through_message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                summary TEXT NOT NULL,
                provider_profile_id TEXT NOT NULL,
                model_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE project_knowledge (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                knowledge_type TEXT NOT NULL CHECK(knowledge_type IN
                    ('fact','decision','constraint','summary','artifact_reference')),
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                pinned INTEGER NOT NULL DEFAULT 0 CHECK(pinned IN (0,1)),
                source_conversation_id TEXT NULL REFERENCES conversations(id) ON DELETE SET NULL,
                source_message_id TEXT NULL REFERENCES messages(id) ON DELETE SET NULL,
                current_revision INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('published','archived')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE project_knowledge_revisions (
                id TEXT PRIMARY KEY,
                knowledge_id TEXT NOT NULL REFERENCES project_knowledge(id) ON DELETE CASCADE,
                revision INTEGER NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(knowledge_id, revision)
            );
            CREATE TABLE knowledge_proposals (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                source_conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                source_message_id TEXT NULL REFERENCES messages(id) ON DELETE SET NULL,
                knowledge_type TEXT NOT NULL CHECK(knowledge_type IN
                    ('fact','decision','constraint','summary','artifact_reference')),
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','published','rejected')),
                created_at TEXT NOT NULL,
                resolved_at TEXT NULL
            );
            """
        )
        after = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("projects", "conversations", "messages")
        }
        if before != after:
            raise CoreDataError("migration_failed", "The model migration changed existing record counts.")

    @staticmethod
    def _migrate_v5_to_v6(connection: sqlite3.Connection) -> None:
        """Add project model scoping and auditable fixed-selection metadata."""
        before = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("projects", "conversations", "messages", "model_runs")
        }
        connection.executescript(
            """
            ALTER TABLE projects ADD COLUMN model_scope_policy TEXT NOT NULL DEFAULT 'inherit_global'
                CHECK(model_scope_policy IN ('inherit_global','restricted'));
            ALTER TABLE model_runs ADD COLUMN selection_source TEXT NOT NULL DEFAULT 'global_default'
                CHECK(selection_source IN ('global_default','conversation_override','user_once','router'));
            ALTER TABLE model_runs ADD COLUMN selection_reason_code TEXT NOT NULL DEFAULT 'fixed_selection';
            ALTER TABLE model_runs ADD COLUMN routing_policy_version TEXT NULL;
            CREATE TABLE project_model_allowlist (
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                provider_profile_id TEXT NOT NULL,
                model_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(project_id, provider_profile_id, model_id)
            );
            CREATE INDEX idx_project_model_allowlist_project
                ON project_model_allowlist(project_id, provider_profile_id, model_id);
            """
        )
        after = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("projects", "conversations", "messages", "model_runs")
        }
        if before != after:
            raise CoreDataError("migration_failed", "The model scope migration changed existing record counts.")

    @staticmethod
    def _migrate_v6_to_v7(connection: sqlite3.Connection) -> None:
        """Add resumable tool operations and per-work purpose overrides."""
        protected = ("projects", "conversations", "messages", "model_runs")
        before = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in protected
        }
        connection.execute(
            """ALTER TABLE conversations ADD COLUMN purpose_override TEXT NULL
               CHECK(purpose_override IS NULL OR purpose_override IN
                   ('casual','research','work','writing','coding','gaming','other'))"""
        )
        connection.execute("ALTER TABLE model_runs RENAME TO model_runs_v6")
        connection.executescript(
            """
            CREATE TABLE model_runs (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                user_message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                assistant_message_id TEXT NULL REFERENCES messages(id) ON DELETE SET NULL,
                status TEXT NOT NULL CHECK(status IN
                    ('queued','building_context','generating','waiting_tool','completed',
                     'failed','cancelled','interrupted')),
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
                routing_policy_version TEXT NULL
            );
            INSERT INTO model_runs(
                id, conversation_id, user_message_id, assistant_message_id, status,
                idempotency_key, provider_profile_id, model_id, initiator_kind,
                parent_run_id, context_snapshot_id, error_code, error_message,
                created_at, started_at, finished_at, selection_source,
                selection_reason_code, routing_policy_version
            ) SELECT
                id, conversation_id, user_message_id, assistant_message_id, status,
                idempotency_key, provider_profile_id, model_id, initiator_kind,
                parent_run_id, context_snapshot_id, error_code, error_message,
                created_at, started_at, finished_at, selection_source,
                selection_reason_code, routing_policy_version
              FROM model_runs_v6;
            DROP TABLE model_runs_v6;

            CREATE TABLE tool_operations (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES model_runs(id) ON DELETE CASCADE,
                attempt_id TEXT NOT NULL REFERENCES run_attempts(id) ON DELETE CASCADE,
                sequence INTEGER NOT NULL,
                provider_call_id TEXT NOT NULL,
                tool_name TEXT NOT NULL CHECK(tool_name IN
                    ('fs.list','fs.search','fs.read','fs.propose_changes')),
                arguments_json TEXT NOT NULL,
                arguments_hash TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN
                    ('requested','validating','reviewing','waiting_user','executing',
                     'completed','rejected','failed','cancelled','interrupted')),
                result_summary TEXT NULL,
                result_hash TEXT NULL,
                error_code TEXT NULL,
                created_at TEXT NOT NULL,
                finished_at TEXT NULL,
                UNIQUE(run_id, attempt_id, sequence),
                UNIQUE(run_id, attempt_id, provider_call_id)
            );
            CREATE TABLE tool_reviews (
                id TEXT PRIMARY KEY,
                operation_id TEXT NOT NULL REFERENCES tool_operations(id) ON DELETE CASCADE,
                provider_profile_id TEXT NOT NULL,
                model_id TEXT NOT NULL,
                decision TEXT NOT NULL CHECK(decision IN ('approve','reject','uncertain')),
                reason_code TEXT NOT NULL,
                input_tokens INTEGER NULL,
                output_tokens INTEGER NULL,
                duration_ms INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        after = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in protected
        }
        if before != after:
            raise CoreDataError("migration_failed", "The tool migration changed existing record counts.")

    @staticmethod
    def _migrate_v2_to_v3(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            ALTER TABLE chat_groups ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE projects ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE conversations ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE conversations ADD COLUMN copied_from_conversation_id TEXT NULL
                REFERENCES conversations(id) ON DELETE SET NULL;
            ALTER TABLE conversations ADD COLUMN copied_through_message_id TEXT NULL;
            ALTER TABLE conversations ADD COLUMN copied_at TEXT NULL;
            """
        )
        CoreRepository._backfill(connection, "chat_groups", "SELECT id FROM chat_groups ORDER BY name COLLATE NOCASE, id")
        CoreRepository._backfill(connection, "projects", "SELECT id FROM projects ORDER BY updated_at DESC, id")
        containers = connection.execute(
            "SELECT kind, chat_group_id, project_id FROM conversations GROUP BY kind, chat_group_id, project_id"
        ).fetchall()
        for container in containers:
            if container["kind"] == "standard_chat":
                rows = connection.execute(
                    """SELECT id FROM conversations WHERE kind = 'standard_chat'
                       AND chat_group_id IS ? ORDER BY updated_at DESC, id""", (container["chat_group_id"],)
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT id FROM conversations WHERE kind = 'project_work'
                       AND project_id = ? ORDER BY updated_at DESC, id""", (container["project_id"],)
                ).fetchall()
            for index, row in enumerate(rows):
                connection.execute("UPDATE conversations SET sort_order = ? WHERE id = ?", (index, row["id"]))

    @staticmethod
    def _migrate_v3_to_v4(connection: sqlite3.Connection) -> None:
        """Repair v3's column-order bug and add recoverable deletion metadata."""
        connection.execute("ALTER TABLE projects ADD COLUMN deleted_at TEXT NULL")
        connection.execute("ALTER TABLE conversations ADD COLUMN deleted_at TEXT NULL")

        # v2 -> v3 appended sort_order after updated_at. Rebuilding makes fresh and
        # migrated databases share the same layout, but all writes still name columns.
        connection.execute("ALTER TABLE chat_groups RENAME TO chat_groups_v3")
        connection.execute(
            """CREATE TABLE chat_groups (
                   id TEXT PRIMARY KEY, name TEXT NOT NULL, sort_order INTEGER NOT NULL,
                   created_at TEXT NOT NULL, updated_at TEXT NOT NULL
               )"""
        )
        rows = connection.execute(
            """SELECT id, name, created_at, updated_at, sort_order
               FROM chat_groups_v3
               ORDER BY CASE WHEN typeof(sort_order) = 'integer' AND sort_order >= 0 THEN 0 ELSE 1 END,
                        CASE WHEN typeof(sort_order) = 'integer' THEN sort_order ELSE 0 END,
                        created_at, id"""
        ).fetchall()
        for index, row in enumerate(rows):
            # The broken INSERT stored 0 in created_at and the timestamp in sort_order.
            created_at = row["created_at"]
            updated_at = row["updated_at"]
            if not isinstance(created_at, str) or created_at == "0":
                created_at = updated_at if isinstance(updated_at, str) else _utc_now()
            connection.execute(
                """INSERT INTO chat_groups(id, name, sort_order, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (row["id"], row["name"], index, created_at, updated_at),
            )
        connection.execute("DROP TABLE chat_groups_v3")
        CoreRepository._normalize_all_orders(connection)

    @staticmethod
    def _normalize_all_orders(connection: sqlite3.Connection) -> None:
        CoreRepository._backfill(
            connection, "projects",
            """SELECT id FROM projects
               ORDER BY CASE WHEN typeof(sort_order) = 'integer' AND sort_order >= 0 THEN 0 ELSE 1 END,
                        CASE WHEN typeof(sort_order) = 'integer' THEN sort_order ELSE 0 END,
                        created_at, id""",
        )
        containers = connection.execute(
            "SELECT kind, chat_group_id, project_id FROM conversations GROUP BY kind, chat_group_id, project_id"
        ).fetchall()
        for container in containers:
            column = "chat_group_id" if container["kind"] == "standard_chat" else "project_id"
            rows = connection.execute(
                f"""SELECT id FROM conversations WHERE kind = ? AND {column} IS ?
                    ORDER BY CASE WHEN typeof(sort_order) = 'integer' AND sort_order >= 0 THEN 0 ELSE 1 END,
                             CASE WHEN typeof(sort_order) = 'integer' THEN sort_order ELSE 0 END,
                             created_at, id""",
                (container["kind"], container[column]),
            ).fetchall()
            for index, row in enumerate(rows):
                connection.execute("UPDATE conversations SET sort_order = ? WHERE id = ?", (index, row["id"]))

    @staticmethod
    def _validate_database(connection: sqlite3.Connection) -> None:
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise CoreDataError("invalid_persisted_data", "The local data contains invalid relationships.")
        for table in ("chat_groups", "projects", "conversations"):
            invalid = connection.execute(
                f"SELECT 1 FROM {table} WHERE typeof(sort_order) != 'integer' OR sort_order < 0 LIMIT 1"
            ).fetchone()
            if invalid is not None:
                raise CoreDataError("invalid_persisted_data", "The local data contains an invalid manual order.")
        invalid_sequence = connection.execute(
            "SELECT 1 FROM messages WHERE sequence_no IS NULL OR sequence_no < 1 LIMIT 1"
        ).fetchone()
        if invalid_sequence is not None:
            raise CoreDataError("invalid_persisted_data", "The local data contains an invalid message sequence.")
        duplicate_sequence = connection.execute(
            """SELECT 1 FROM messages GROUP BY conversation_id, sequence_no
               HAVING COUNT(*) > 1 LIMIT 1"""
        ).fetchone()
        if duplicate_sequence is not None:
            raise CoreDataError("invalid_persisted_data", "The local data contains duplicate message sequences.")

    @staticmethod
    def _validated_rows(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        result = []
        for row in rows:
            value = row["sort_order"]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise CoreDataError("invalid_persisted_data", "The local data contains an invalid manual order.")
            result.append(dict(row))
        return result

    def _remove_legacy_backups(self) -> None:
        if self.database_path.name != "agent.db":
            return
        for backup in self.database_path.parent.glob("agent.db.schema-v1-backup-*"):
            if backup.is_file():
                try:
                    backup.unlink()
                    self.removed_legacy_backups += 1
                except OSError:
                    self.legacy_backup_cleanup_failures += 1

    @staticmethod
    def _backfill(connection: sqlite3.Connection, table: str, query: str) -> None:
        for index, row in enumerate(connection.execute(query).fetchall()):
            connection.execute(f"UPDATE {table} SET sort_order = ? WHERE id = ?", (index, row["id"]))

    @staticmethod
    def _reorder(
        connection: sqlite3.Connection, table: str, query: str,
        parameters: tuple[Any, ...], ordered_ids: list[str],
    ) -> None:
        _validate_ids(ordered_ids)
        existing = [row["id"] for row in connection.execute(query, parameters).fetchall()]
        if len(existing) != len(ordered_ids) or set(existing) != set(ordered_ids):
            raise CoreDataError("invalid_order", "Ordered ids must exactly match the selected container.")
        for index, item_id in enumerate(ordered_ids):
            connection.execute(f"UPDATE {table} SET sort_order = ? WHERE id = ?", (index, item_id))

    def list_chat_groups(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, name, sort_order, created_at, updated_at FROM chat_groups ORDER BY sort_order, id"
            ).fetchall()
        return self._validated_rows(rows)

    def create_chat_group(self, name: str) -> dict[str, Any]:
        group_id, now = _uuid(), _utc_now()
        normalized = _name(name, "invalid_chat_group_name", "Chat group name")
        with self._connect() as connection:
            connection.execute("UPDATE chat_groups SET sort_order = sort_order + 1")
            connection.execute(
                """INSERT INTO chat_groups(id, name, sort_order, created_at, updated_at)
                   VALUES (?, ?, 0, ?, ?)""", (group_id, normalized, now, now)
            )
        return next(group for group in self.list_chat_groups() if group["id"] == group_id)

    def rename_chat_group(self, group_id: str, name: str) -> dict[str, Any]:
        normalized, now = _name(name, "invalid_chat_group_name", "Chat group name"), _utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE chat_groups SET name = ?, updated_at = ? WHERE id = ?", (normalized, now, group_id)
            )
            if cursor.rowcount != 1:
                raise CoreDataError("chat_group_not_found", "The chat group does not exist.")
            row = connection.execute("SELECT * FROM chat_groups WHERE id = ?", (group_id,)).fetchone()
        return dict(row)

    def delete_chat_group(self, group_id: str) -> None:
        with self._connect() as connection:
            group = connection.execute("SELECT sort_order FROM chat_groups WHERE id = ?", (group_id,)).fetchone()
            if group is None:
                raise CoreDataError("chat_group_not_found", "The chat group does not exist.")
            chats = connection.execute(
                """SELECT id FROM conversations WHERE kind = 'standard_chat' AND chat_group_id = ?
                   ORDER BY sort_order, id""", (group_id,)
            ).fetchall()
            connection.execute(
                """UPDATE conversations SET sort_order = sort_order + ?
                   WHERE kind = 'standard_chat' AND chat_group_id IS NULL""", (len(chats),)
            )
            for index, chat in enumerate(chats):
                connection.execute(
                    "UPDATE conversations SET chat_group_id = NULL, sort_order = ? WHERE id = ?",
                    (index, chat["id"]),
                )
            connection.execute("DELETE FROM chat_groups WHERE id = ?", (group_id,))
            connection.execute(
                "UPDATE chat_groups SET sort_order = sort_order - 1 WHERE sort_order > ?", (group["sort_order"],)
            )

    def reorder_chat_groups(self, ordered_ids: list[str]) -> list[dict[str, Any]]:
        with self._connect() as connection:
            self._reorder(connection, "chat_groups", "SELECT id FROM chat_groups", (), ordered_ids)
        return self.list_chat_groups()

    def list_projects(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT id, name, category, sort_order, created_at, updated_at, deleted_at,
                          model_access_policy, model_scope_policy, web_access_policy
                   FROM projects WHERE deleted_at IS NULL ORDER BY sort_order, id"""
            ).fetchall()
        return self._validated_rows(rows)

    def create_project(self, name: str, category: str) -> dict[str, Any]:
        project_id, now = _uuid(), _utc_now()
        normalized, category = _name(name, "invalid_project_name", "Project name"), _category(category)
        with self._connect() as connection:
            connection.execute("UPDATE projects SET sort_order = sort_order + 1 WHERE deleted_at IS NULL")
            connection.execute(
                """INSERT INTO projects(id, name, category, sort_order, created_at, updated_at,
                                        model_access_policy, web_access_policy)
                   VALUES (?, ?, ?, 0, ?, ?, 'allow_online', 'allow_search')""",
                (project_id, normalized, category, now, now),
            )
        return next(project for project in self.list_projects() if project["id"] == project_id)

    def update_project(self, project_id: str, name: str, category: str) -> dict[str, Any]:
        normalized, category, now = _name(name, "invalid_project_name", "Project name"), _category(category), _utc_now()
        with self._connect() as connection:
            previous = connection.execute(
                "SELECT category FROM projects WHERE id = ? AND deleted_at IS NULL", (project_id,)
            ).fetchone()
            cursor = connection.execute(
                """UPDATE projects SET name = ?, category = ?, updated_at = ?
                   WHERE id = ? AND deleted_at IS NULL""",
                (normalized, category, now, project_id),
            )
            if cursor.rowcount != 1:
                raise CoreDataError("project_not_found", "The project does not exist.")
            if previous is not None and previous["category"] != category:
                work_ids = connection.execute(
                    """SELECT id FROM conversations WHERE project_id = ? AND kind = 'project_work'
                           AND purpose_override IS NULL AND deleted_at IS NULL""",
                    (project_id,),
                ).fetchall()
                for work in work_ids:
                    self._insert_message_with_connection(
                        connection, work["id"], "system_event",
                        json.dumps({"purpose": category, "source": "project"}, ensure_ascii=True),
                        "work_purpose_changed",
                    )
            row = connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return dict(row)

    def reorder_projects(self, ordered_ids: list[str]) -> list[dict[str, Any]]:
        with self._connect() as connection:
            self._reorder(connection, "projects", "SELECT id FROM projects WHERE deleted_at IS NULL", (), ordered_ids)
        return self.list_projects()

    def list_conversations(
        self, kind: str | None = None, chat_group_id: str | None = None,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = ["c.deleted_at IS NULL", "(c.project_id IS NULL OR p.deleted_at IS NULL)"]
        parameters: list[str] = []
        if kind is not None:
            if kind not in CONVERSATION_KINDS:
                raise CoreDataError("invalid_conversation_kind", "Conversation kind is not supported.")
            clauses.append("c.kind = ?")
            parameters.append(kind)
        if chat_group_id is not None:
            clauses.append("c.chat_group_id = ?")
            parameters.append(chat_group_id)
        if project_id is not None:
            clauses.append("c.project_id = ?")
            parameters.append(project_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT c.id, c.kind, c.title, c.category, c.purpose_override,
                           c.chat_group_id, c.project_id,
                           c.sort_order, c.derived_from_conversation_id, c.derived_through_message_id,
                           c.derived_at, c.copied_from_conversation_id,
                           c.copied_through_message_id, c.copied_at, c.created_at, c.updated_at,
                           c.deleted_at, c.provider_profile_id, c.model_id, c.web_search_mode,
                           c.model_selection_mode, c.routing_preset,
                           c.coordinator_provider_profile_id, c.coordinator_model_id,
                           p.category AS project_category, p.model_access_policy, p.model_scope_policy,
                           p.web_access_policy
                    FROM conversations c LEFT JOIN projects p ON p.id = c.project_id
                    {where} ORDER BY c.sort_order, c.id""", parameters
            ).fetchall()
        self._validated_rows(rows)
        return [self._conversation_summary(row) for row in rows]

    def get_conversation(self, conversation_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            return self._get_conversation_with_connection(connection, conversation_id)

    def get_run_conversation(self, run_id: str) -> dict[str, Any]:
        """Current authority and selected message only; never a UI history load."""
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            run = connection.execute(
                "SELECT conversation_id, user_message_id FROM model_runs WHERE id = ?", (run_id,),
            ).fetchone()
            if run is None:
                raise CoreDataError("run_not_found", "The model run does not exist.")
            return self._get_conversation_with_connection(
                connection, run["conversation_id"], message_id=run["user_message_id"],
            )

    def get_run_recent_messages(self, run_id: str) -> list[dict[str, Any]]:
        """Bounded advisor excerpts, scoped to the six messages before this request."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT m.role, substr(m.content, 1, 2000) AS content
                   FROM model_runs r JOIN messages current ON current.id = r.user_message_id
                   JOIN messages m ON m.conversation_id = r.conversation_id
                   WHERE r.id = ? AND current.conversation_id = r.conversation_id
                     AND m.sequence_no < current.sequence_no AND m.role IN ('user','assistant')
                   ORDER BY m.sequence_no DESC, m.id DESC LIMIT 6""",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def _get_conversation_header_with_connection(
        self, connection: sqlite3.Connection, conversation_id: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            """SELECT c.id, c.kind, c.title, c.category, c.purpose_override,
                      c.chat_group_id, c.project_id,
                      c.sort_order, c.derived_from_conversation_id, c.derived_through_message_id,
                      c.derived_at, c.copied_from_conversation_id,
                      c.copied_through_message_id, c.copied_at, c.created_at, c.updated_at,
                      c.deleted_at, c.provider_profile_id, c.model_id, c.context_generation,
                       c.web_search_mode, c.model_selection_mode, c.routing_preset,
                       c.coordinator_provider_profile_id, c.coordinator_model_id,
                      p.category AS project_category, p.model_access_policy, p.model_scope_policy,
                      p.web_access_policy
               FROM conversations c LEFT JOIN projects p ON p.id = c.project_id
               WHERE c.id = ? AND c.deleted_at IS NULL
                 AND (c.project_id IS NULL OR p.deleted_at IS NULL)""", (conversation_id,)
        ).fetchone()
        if row is None:
            raise CoreDataError("conversation_not_found", "The conversation does not exist.")
        return self._conversation_summary(row)

    def _get_conversation_with_connection(
        self, connection: sqlite3.Connection, conversation_id: str, *, message_id: str | None = None,
    ) -> dict[str, Any]:
            result = self._get_conversation_header_with_connection(connection, conversation_id)
            scope = "conversation_id = ?" + (" AND id = ?" if message_id is not None else "")
            parameters = (conversation_id, message_id) if message_id is not None else (conversation_id,)
            messages = connection.execute(
                f"""SELECT id, conversation_id, role, content, event_type, source_message_id,
                          created_at, sequence_no
                   FROM messages WHERE {scope} ORDER BY sequence_no, id""",
                parameters,
            ).fetchall()
            attachment_rows = connection.execute(
                f"""SELECT id, message_id, kind, name, media_type, byte_size, sha256,
                          storage_key, width, height, source_attachment_id, created_at
                   FROM message_attachments WHERE message_id IN
                       (SELECT id FROM messages WHERE {scope})
                   ORDER BY created_at, rowid""",
                parameters,
            ).fetchall()
            artifact_rows = connection.execute(
                f"""SELECT id AS artifact_id, attachment_id, kind, processor_id,
                          processor_version, media_type, byte_size, sha256, storage_key,
                          width, height, trust, created_at
                   FROM attachment_artifacts WHERE attachment_id IN
                       (SELECT id FROM message_attachments WHERE message_id IN
                           (SELECT id FROM messages WHERE {scope}))
                   ORDER BY created_at, rowid""",
                parameters,
            ).fetchall()
            rejection_rows = connection.execute(
                f"""SELECT id, message_id, display_name, detected_media_type, error_code,
                          processor_hint, created_at
                   FROM message_attachment_rejections WHERE message_id IN
                       (SELECT id FROM messages WHERE {scope})
                   ORDER BY created_at, rowid""",
                parameters,
            ).fetchall()
            grouped: dict[str, list[dict[str, Any]]] = {}
            artifacts_by_attachment: dict[str, list[dict[str, Any]]] = {}
            for artifact in artifact_rows:
                artifacts_by_attachment.setdefault(artifact["attachment_id"], []).append(
                    dict(artifact)
                )
            for attachment in attachment_rows:
                value = dict(attachment)
                value["artifacts"] = artifacts_by_attachment.get(attachment["id"], [])
                self._add_legacy_model_artifact_view(value)
                grouped.setdefault(attachment["message_id"], []).append(value)
            rejected_grouped: dict[str, list[dict[str, Any]]] = {}
            for rejection in rejection_rows:
                rejected_grouped.setdefault(rejection["message_id"], []).append(dict(rejection))
            result["messages"] = [
                {
                    **dict(message),
                    "attachments": grouped.get(message["id"], []),
                    "attachment_rejections": rejected_grouped.get(message["id"], []),
                }
                for message in messages
            ]
            return result

    def resolve_attachment_references(
        self, conversation_id: str, references: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        from agent.context_engine.attachment_references import validate_references, text_range
        from agent.context_engine.service import ContextEngineService
        references = validate_references(references)
        result = []
        for reference in references:
            attachment = self.get_attachment(reference["attachment_id"])
            if attachment["conversation_id"] != conversation_id:
                raise CoreDataError("attachment_reference_forbidden", "The attachment belongs to another conversation.")
            attachment.update({key: reference[key] for key in ("source_start", "source_end") if key in reference})
            attachment["reference_attachment_id"] = attachment["id"]
            attachment["reference_message_id"] = attachment["message_id"]
            artifact = next((a for a in attachment["artifacts"] if a["kind"] == (
                "text.utf8" if attachment["kind"] == "text" else "image.normalized"
            )), None)
            # Recheck the original as well as the extracted/normalized artifact.
            ContextEngineService._read_attachment(attachment, text=False)
            content = ContextEngineService._read_attachment(attachment,
                text=attachment["kind"] == "text", model_variant=attachment["kind"] == "image", artifact=artifact)
            if attachment["kind"] == "text":
                _, start, end = text_range(attachment, content)
                attachment.update(source_start=start, source_end=end)
            elif "source_start" in reference or "source_end" in reference:
                raise CoreDataError("attachment_reference_range_invalid", "Images do not have text ranges.")
            result.append(attachment)
        return result

    def get_attachment(self, attachment_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT a.id, a.message_id, a.kind, a.name, a.media_type, a.byte_size,
                          a.sha256, a.storage_key, a.width, a.height, a.source_attachment_id,
                          a.created_at, m.conversation_id
                   FROM message_attachments a JOIN messages m ON m.id = a.message_id
                   JOIN conversations c ON c.id = m.conversation_id
                   LEFT JOIN projects p ON p.id = c.project_id
                   WHERE a.id = ? AND c.deleted_at IS NULL
                     AND (c.project_id IS NULL OR p.deleted_at IS NULL)""",
                (attachment_id,),
            ).fetchone()
            if row is None:
                raise CoreDataError("attachment_not_found", "The attachment does not exist.")
            artifacts = connection.execute(
                """SELECT id AS artifact_id, kind, processor_id, processor_version,
                          media_type, byte_size, sha256, storage_key, width, height,
                          trust, created_at
                   FROM attachment_artifacts WHERE attachment_id = ? ORDER BY created_at, rowid""",
                (attachment_id,),
            ).fetchall()
            result = dict(row)
            result["artifacts"] = [dict(item) for item in artifacts]
            self._add_legacy_model_artifact_view(result)
            return result

    @staticmethod
    def _add_legacy_model_artifact_view(attachment: dict[str, Any]) -> None:
        """Expose v12 response fields from the authoritative Artifact without storing them twice."""
        artifact = next((
            item for item in attachment.get("artifacts", [])
            if item.get("kind") == "image.normalized"
        ), None)
        attachment["model_storage_key"] = artifact.get("storage_key") if artifact else None
        attachment["model_media_type"] = artifact.get("media_type") if artifact else None
        attachment["model_byte_size"] = artifact.get("byte_size") if artifact else None
        attachment["model_sha256"] = artifact.get("sha256") if artifact else None
        attachment["model_width"] = artifact.get("width") if artifact else None
        attachment["model_height"] = artifact.get("height") if artifact else None
        attachment["normalization_version"] = (
            artifact.get("processor_version") if artifact else "legacy_passthrough"
            if attachment.get("kind") == "image" else None
        )

    def update_attachment_model_copy(
        self, attachment_id: str, model_storage_key: str, model_media_type: str,
        model_byte_size: int, model_sha256: str, model_width: int,
        model_height: int, normalization_version: str,
    ) -> dict[str, Any]:
        if model_media_type not in {"image/png", "image/jpeg"}:
            raise CoreDataError("image_format_unsupported", "The normalized image format is invalid.")
        if not isinstance(model_byte_size, int) or not 0 < model_byte_size <= 10 * 1024 * 1024:
            raise CoreDataError("image_payload_too_large", "The normalized image size is invalid.")
        if (not isinstance(model_width, int) or not isinstance(model_height, int)
                or model_width < 1 or model_height < 1 or model_width > 4096
                or model_height > 4096 or model_width * model_height > 16_000_000):
            raise CoreDataError("image_normalization_failed", "The normalized image dimensions are invalid.")
        if (len(model_sha256) != 64
                or any(character not in "0123456789abcdef" for character in model_sha256)):
            raise CoreDataError("image_integrity_failed", "The normalized image hash is invalid.")
        if (normalization_version != "image-normalization-v1"
                or not model_storage_key.startswith("message-attachments/")
                or ".." in model_storage_key.replace("\\", "/").split("/")
                or "\\" in model_storage_key):
            raise CoreDataError("invalid_attachment_path", "The normalized image path is invalid.")
        with self._connect() as connection:
            row = connection.execute(
                """SELECT a.*, m.conversation_id FROM message_attachments a
                   JOIN messages m ON m.id = a.message_id WHERE a.id = ?""",
                (attachment_id,),
            ).fetchone()
            if row is None:
                raise CoreDataError("attachment_not_found", "The attachment does not exist.")
            if row["kind"] != "image":
                raise CoreDataError("image_format_unsupported", "The attachment is not an image.")
            artifact = connection.execute(
                """SELECT id, processor_version, media_type, byte_size, storage_key, sha256,
                          width, height FROM attachment_artifacts
                   WHERE attachment_id = ? AND kind = 'image.normalized'""",
                (attachment_id,),
            ).fetchone()
            if artifact is None:
                connection.execute(
                    """INSERT INTO attachment_artifacts(
                           id, attachment_id, kind, processor_id, processor_version,
                           media_type, byte_size, sha256, storage_key, width, height,
                           trust, created_at
                       ) VALUES (?, ?, 'image.normalized', 'builtin.image', ?, ?, ?, ?, ?, ?, ?,
                                 'external_untrusted', ?)""",
                    (_uuid(), attachment_id, normalization_version, model_media_type,
                     model_byte_size, model_sha256, model_storage_key, model_width,
                     model_height, _utc_now()),
                )
                connection.execute(
                    "UPDATE conversations SET context_generation = context_generation + 1 WHERE id = ?",
                    (row["conversation_id"],),
                )
            elif (
                artifact["processor_version"], artifact["media_type"], artifact["byte_size"],
                artifact["sha256"], artifact["storage_key"], artifact["width"], artifact["height"],
            ) != (
                normalization_version, model_media_type, model_byte_size,
                model_sha256, model_storage_key, model_width, model_height,
            ):
                raise CoreDataError(
                    "image_normalization_conflict",
                    "A different normalized image Artifact is already registered.",
                )
        return self.get_attachment(attachment_id)

    def create_conversation(
        self, kind: str, title: str, category: str | None = None,
        chat_group_id: str | None = None, project_id: str | None = None,
    ) -> dict[str, Any]:
        if kind not in CONVERSATION_KINDS:
            raise CoreDataError("invalid_conversation_kind", "Conversation kind is not supported.")
        title = _name(title, "invalid_conversation_title", "Conversation title")
        if kind == "standard_chat":
            if project_id is not None or category is None:
                raise CoreDataError("invalid_conversation_container", "A standard chat cannot belong to a project.")
            category = _category(category)
            predicate, values = "kind = 'standard_chat' AND chat_group_id IS ?", (chat_group_id,)
        elif project_id is None or chat_group_id is not None or category is not None:
            raise CoreDataError("invalid_conversation_container", "Project work must belong to one project.")
        else:
            predicate, values = "kind = 'project_work' AND project_id = ?", (project_id,)
        conversation_id, now = _uuid(), _utc_now()
        try:
            with self._connect() as connection:
                if chat_group_id is not None and connection.execute(
                    "SELECT 1 FROM chat_groups WHERE id = ?", (chat_group_id,)
                ).fetchone() is None:
                    raise CoreDataError("chat_group_not_found", "The selected chat group does not exist.")
                if project_id is not None and connection.execute(
                    "SELECT 1 FROM projects WHERE id = ? AND deleted_at IS NULL", (project_id,)
                ).fetchone() is None:
                    raise CoreDataError("project_not_found", "The selected project does not exist.")
                connection.execute(
                    f"""UPDATE conversations SET sort_order = sort_order + 1
                        WHERE deleted_at IS NULL AND {predicate}""", values,
                )
                connection.execute(
                    """INSERT INTO conversations(
                           id, kind, title, category, chat_group_id, project_id, sort_order,
                           created_at, updated_at, model_selection_mode, routing_preset
                       ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, 'automatic', 'balanced')""",
                    (conversation_id, kind, title, category, chat_group_id, project_id, now, now),
                )
        except sqlite3.IntegrityError as error:
            raise CoreDataError("container_not_found", "The selected container does not exist.") from error
        return self.get_conversation(conversation_id)

    def update_conversation(
        self, conversation_id: str, title: str, category: str | None,
    ) -> dict[str, Any]:
        current = self.get_conversation(conversation_id)
        title = _name(title, "invalid_conversation_title", "Conversation title")
        if current["kind"] == "project_work":
            if category is not None:
                raise CoreDataError("invalid_category", "Project work inherits its project category.")
        else:
            if category is None:
                raise CoreDataError("invalid_category", "A standard chat requires a category.")
            category = _category(category)
        with self._connect() as connection:
            connection.execute(
                "UPDATE conversations SET title = ?, category = ?, updated_at = ? WHERE id = ?",
                (title, category, _utc_now(), conversation_id),
            )
        return self.get_conversation(conversation_id)

    def update_work_purpose(
        self, conversation_id: str, purpose_override: str | None,
    ) -> dict[str, Any]:
        conversation = self.get_conversation(conversation_id)
        if conversation["kind"] != "project_work":
            raise CoreDataError("purpose_not_allowed", "Only project work can override its purpose.")
        if purpose_override is not None:
            purpose_override = _category(purpose_override)
        with self._connect() as connection:
            connection.execute(
                "UPDATE conversations SET purpose_override = ?, updated_at = ? WHERE id = ?",
                (purpose_override, _utc_now(), conversation_id),
            )
            self._insert_message_with_connection(
                connection, conversation_id, "system_event",
                json.dumps({
                    "purpose": purpose_override,
                    "source": "work_override" if purpose_override is not None else "project",
                }, ensure_ascii=True),
                "work_purpose_changed",
            )
        return self.get_conversation(conversation_id)

    def reorder_conversations(
        self, kind: str, container_id: str | None, ordered_ids: list[str],
    ) -> list[dict[str, Any]]:
        if kind == "standard_chat":
            query = """SELECT id FROM conversations WHERE deleted_at IS NULL
                       AND kind = 'standard_chat' AND chat_group_id IS ?"""
            parameters = (container_id,)
        elif kind == "project_work" and container_id is not None:
            query = """SELECT id FROM conversations WHERE deleted_at IS NULL
                       AND kind = 'project_work' AND project_id = ?"""
            parameters = (container_id,)
        else:
            raise CoreDataError("invalid_conversation_container", "The conversation container is invalid.")
        with self._connect() as connection:
            self._reorder(connection, "conversations", query, parameters, ordered_ids)
        return self.list_conversations(
            kind, container_id if kind == "standard_chat" else None,
            container_id if kind == "project_work" else None,
        )

    @staticmethod
    def _target_index(value: int, count: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > count:
            raise CoreDataError("invalid_target_index", "The target index is outside the destination container.")
        return value

    def move_chat(self, conversation_id: str, chat_group_id: str | None, target_index: int) -> dict[str, Any]:
        current = self.get_conversation(conversation_id)
        if current["kind"] != "standard_chat":
            raise CoreDataError("invalid_conversation_kind", "Only a standard chat can join a chat group.")
        with self._connect() as connection:
            if chat_group_id is not None and connection.execute(
                "SELECT 1 FROM chat_groups WHERE id = ?", (chat_group_id,)
            ).fetchone() is None:
                raise CoreDataError("chat_group_not_found", "The selected chat group does not exist.")
            count = connection.execute(
                """SELECT COUNT(*) FROM conversations WHERE deleted_at IS NULL AND kind = 'standard_chat'
                   AND chat_group_id IS ? AND id != ?""", (chat_group_id, conversation_id)
            ).fetchone()[0]
            target_index = self._target_index(target_index, count)
            connection.execute(
                """UPDATE conversations SET sort_order = sort_order - 1
                   WHERE deleted_at IS NULL AND kind = 'standard_chat'
                     AND chat_group_id IS ? AND sort_order > ?""",
                (current["chat_group_id"], current["sort_order"]),
            )
            connection.execute(
                """UPDATE conversations SET sort_order = sort_order + 1
                   WHERE deleted_at IS NULL AND kind = 'standard_chat'
                     AND chat_group_id IS ? AND sort_order >= ?""",
                (chat_group_id, target_index),
            )
            connection.execute(
                "UPDATE conversations SET chat_group_id = ?, sort_order = ?, updated_at = ? WHERE id = ?",
                (chat_group_id, target_index, _utc_now(), conversation_id),
            )
        return self.get_conversation(conversation_id)

    def move_work(self, conversation_id: str, project_id: str, target_index: int) -> dict[str, Any]:
        current = self.get_conversation(conversation_id)
        if current["kind"] != "project_work":
            raise CoreDataError("invalid_conversation_kind", "Only project work can move between projects.")
        with self._connect() as connection:
            if connection.execute(
                "SELECT 1 FROM projects WHERE id = ? AND deleted_at IS NULL", (project_id,)
            ).fetchone() is None:
                raise CoreDataError("project_not_found", "The selected project does not exist.")
            count = connection.execute(
                """SELECT COUNT(*) FROM conversations WHERE deleted_at IS NULL AND kind = 'project_work'
                   AND project_id = ? AND id != ?""", (project_id, conversation_id)
            ).fetchone()[0]
            target_index = self._target_index(target_index, count)
            connection.execute(
                """UPDATE conversations SET sort_order = sort_order - 1
                   WHERE deleted_at IS NULL AND kind = 'project_work'
                     AND project_id = ? AND sort_order > ?""",
                (current["project_id"], current["sort_order"]),
            )
            connection.execute(
                """UPDATE conversations SET sort_order = sort_order + 1
                   WHERE deleted_at IS NULL AND kind = 'project_work'
                     AND project_id = ? AND sort_order >= ?""",
                (project_id, target_index),
            )
            connection.execute(
                """UPDATE conversations SET project_id = ?, sort_order = ?, updated_at = ?,
                       web_search_mode = CASE WHEN project_id = ? THEN web_search_mode ELSE 'inherit' END
                   WHERE id = ?""",
                (project_id, target_index, _utc_now(), project_id, conversation_id),
            )
        return self.get_conversation(conversation_id)

    def derive_work(
        self, source_conversation_id: str, project_id: str, title: str | None = None,
    ) -> dict[str, Any]:
        source = self.get_conversation(source_conversation_id)
        if source["kind"] != "standard_chat":
            raise CoreDataError("invalid_derivation_source", "Only a standard chat can be derived into work.")
        return self._copy_to_work(source, project_id, title, derived=True)

    def copy_work(
        self, source_conversation_id: str, project_id: str, title: str | None = None,
    ) -> dict[str, Any]:
        source = self.get_conversation(source_conversation_id)
        if source["kind"] != "project_work":
            raise CoreDataError("invalid_copy_source", "Only project work can be copied to a project.")
        return self._copy_to_work(source, project_id, title, derived=False)

    def _copy_to_work(
        self, source: dict[str, Any], project_id: str, title: str | None, derived: bool,
    ) -> dict[str, Any]:
        title = _name(title or source["title"], "invalid_conversation_title", "Conversation title")
        work_id, now = _uuid(), _utc_now()
        with self._connect() as connection:
            if connection.execute(
                "SELECT 1 FROM projects WHERE id = ? AND deleted_at IS NULL", (project_id,)
            ).fetchone() is None:
                raise CoreDataError("project_not_found", "The selected project does not exist.")
            messages = connection.execute(
                """SELECT id, role, content, event_type, created_at FROM messages
                   WHERE conversation_id = ? ORDER BY created_at, rowid""", (source["id"],)
            ).fetchall()
            cutoff = messages[-1]["id"] if messages else None
            connection.execute(
                """UPDATE conversations SET sort_order = sort_order + 1
                   WHERE deleted_at IS NULL AND kind = 'project_work' AND project_id = ?""", (project_id,)
            )
            connection.execute(
                """INSERT INTO conversations(
                       id, kind, title, category, chat_group_id, project_id, sort_order,
                       derived_from_conversation_id, derived_through_message_id, derived_at,
                       copied_from_conversation_id, copied_through_message_id, copied_at,
                       created_at, updated_at, model_selection_mode, routing_preset,
                       coordinator_provider_profile_id, coordinator_model_id
                   ) VALUES (?, 'project_work', ?, NULL, NULL, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?,
                             'automatic', 'balanced', NULL, NULL)""",
                (
                    work_id, title, project_id,
                    source["id"] if derived else source.get("derived_from_conversation_id"),
                    cutoff if derived else source.get("derived_through_message_id"),
                    now if derived else source.get("derived_at"),
                    None if derived else source["id"], None if derived else cutoff,
                    None if derived else now, now, now,
                ),
            )
            for sequence_no, message in enumerate(messages, start=1):
                copied_message_id = _uuid()
                connection.execute(
                    """INSERT INTO messages(
                           id, conversation_id, role, content, event_type, source_message_id,
                           created_at, sequence_no
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (copied_message_id, work_id, message["role"], message["content"], message["event_type"],
                     message["id"], message["created_at"], sequence_no),
                )
                attachments = connection.execute(
                    "SELECT * FROM message_attachments WHERE message_id = ? ORDER BY created_at, rowid",
                    (message["id"],),
                ).fetchall()
                for attachment in attachments:
                    copied_attachment_id = _uuid()
                    connection.execute(
                        """INSERT INTO message_attachments(
                               id, message_id, kind, name, media_type, byte_size, sha256,
                               storage_key, width, height, source_attachment_id, created_at
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (copied_attachment_id, copied_message_id, attachment["kind"], attachment["name"],
                         attachment["media_type"], attachment["byte_size"], attachment["sha256"],
                         attachment["storage_key"], attachment["width"], attachment["height"],
                         attachment["id"], attachment["created_at"]),
                    )
                    artifacts = connection.execute(
                        "SELECT * FROM attachment_artifacts WHERE attachment_id = ? ORDER BY created_at, rowid",
                        (attachment["id"],),
                    ).fetchall()
                    for artifact in artifacts:
                        connection.execute(
                            """INSERT INTO attachment_artifacts(
                                   id, attachment_id, kind, processor_id, processor_version,
                                   media_type, byte_size, sha256, storage_key, width, height,
                                   trust, created_at
                               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (_uuid(), copied_attachment_id, artifact["kind"], artifact["processor_id"],
                             artifact["processor_version"], artifact["media_type"],
                             artifact["byte_size"], artifact["sha256"], artifact["storage_key"],
                             artifact["width"], artifact["height"], artifact["trust"],
                             artifact["created_at"]),
                        )
                rejections = connection.execute(
                    "SELECT * FROM message_attachment_rejections WHERE message_id = ? ORDER BY created_at, rowid",
                    (message["id"],),
                ).fetchall()
                for rejection in rejections:
                    connection.execute(
                        """INSERT INTO message_attachment_rejections(
                               id, message_id, display_name, detected_media_type, error_code,
                               processor_hint, created_at
                           ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (_uuid(), copied_message_id, rejection["display_name"],
                         rejection["detected_media_type"], rejection["error_code"],
                         rejection["processor_hint"], rejection["created_at"]),
                    )
            connection.execute(
                "UPDATE conversations SET context_generation = ? WHERE id = ?",
                (len(messages), work_id),
            )
        return self.get_conversation(work_id)

    def delete_conversation(self, conversation_id: str) -> dict[str, Any]:
        now = _utc_now()
        with self._connect() as connection:
            row = connection.execute(
                """SELECT id, kind, title, chat_group_id, project_id, sort_order FROM conversations
                   WHERE id = ? AND deleted_at IS NULL""", (conversation_id,)
            ).fetchone()
            if row is None:
                raise CoreDataError("conversation_not_found", "The conversation does not exist.")
            message_count = connection.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id = ?", (conversation_id,)
            ).fetchone()[0]
            connection.execute("UPDATE conversations SET deleted_at = ? WHERE id = ?", (now, conversation_id))
            container_column = "chat_group_id" if row["kind"] == "standard_chat" else "project_id"
            connection.execute(
                f"""UPDATE conversations SET sort_order = sort_order - 1
                    WHERE deleted_at IS NULL AND kind = ? AND {container_column} IS ? AND sort_order > ?""",
                (row["kind"], row[container_column], row["sort_order"]),
            )
        return {"entity_type": "conversation", "id": row["id"], "title": row["title"],
                "conversation_kind": row["kind"], "project_id": row["project_id"],
                "deleted_at": now, "expires_at": self._trash_expiry(now),
                "work_count": 0, "message_count": message_count}

    def delete_project(self, project_id: str) -> dict[str, Any]:
        now = _utc_now()
        with self._connect() as connection:
            project = connection.execute(
                "SELECT id, name, sort_order FROM projects WHERE id = ? AND deleted_at IS NULL", (project_id,)
            ).fetchone()
            if project is None:
                raise CoreDataError("project_not_found", "The project does not exist.")
            counts = self._project_counts(connection, project_id)
            connection.execute("UPDATE projects SET deleted_at = ? WHERE id = ?", (now, project_id))
            connection.execute(
                "UPDATE projects SET sort_order = sort_order - 1 WHERE deleted_at IS NULL AND sort_order > ?",
                (project["sort_order"],),
            )
        return {"entity_type": "project", "id": project["id"], "title": project["name"],
                "deleted_at": now, "expires_at": self._trash_expiry(now),
                "work_count": counts[0], "message_count": counts[1],
                "conversation_kind": None, "project_id": None}

    def project_delete_impact(self, project_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            project = connection.execute(
                "SELECT name FROM projects WHERE id = ? AND deleted_at IS NULL", (project_id,)
            ).fetchone()
            if project is None:
                raise CoreDataError("project_not_found", "The project does not exist.")
            counts = self._project_counts(connection, project_id)
        return {"project_id": project_id, "title": project["name"],
                "work_count": counts[0], "message_count": counts[1]}

    @staticmethod
    def _project_counts(connection: sqlite3.Connection, project_id: str) -> sqlite3.Row:
        return connection.execute(
            """SELECT COUNT(DISTINCT c.id), COUNT(m.id) FROM conversations c
               LEFT JOIN messages m ON m.conversation_id = c.id WHERE c.project_id = ?""",
            (project_id,),
        ).fetchone()

    def list_trash(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            projects = connection.execute(
                """SELECT p.id, p.name AS title, p.deleted_at,
                          COUNT(DISTINCT c.id) AS work_count, COUNT(m.id) AS message_count
                   FROM projects p LEFT JOIN conversations c ON c.project_id = p.id
                   LEFT JOIN messages m ON m.conversation_id = c.id
                   WHERE p.deleted_at IS NOT NULL GROUP BY p.id ORDER BY p.deleted_at DESC"""
            ).fetchall()
            conversations = connection.execute(
                """SELECT c.id, c.title, c.kind, c.project_id, c.deleted_at, COUNT(m.id) AS message_count
                   FROM conversations c LEFT JOIN messages m ON m.conversation_id = c.id
                   WHERE c.deleted_at IS NOT NULL GROUP BY c.id ORDER BY c.deleted_at DESC"""
            ).fetchall()
        result = [{"entity_type": "project", "id": row["id"], "title": row["title"],
                   "deleted_at": row["deleted_at"], "expires_at": self._trash_expiry(row["deleted_at"]),
                   "work_count": row["work_count"], "message_count": row["message_count"],
                   "conversation_kind": None, "project_id": None} for row in projects]
        result.extend({"entity_type": "conversation", "id": row["id"], "title": row["title"],
                       "deleted_at": row["deleted_at"], "expires_at": self._trash_expiry(row["deleted_at"]),
                       "work_count": 0, "message_count": row["message_count"],
                       "conversation_kind": row["kind"], "project_id": row["project_id"]}
                      for row in conversations)
        return sorted(result, key=lambda item: item["deleted_at"], reverse=True)

    def restore_trash(self, entity_type: str, entity_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            if entity_type == "project":
                connection.execute("UPDATE projects SET sort_order = sort_order + 1 WHERE deleted_at IS NULL")
                cursor = connection.execute(
                    """UPDATE projects SET deleted_at = NULL, sort_order = 0,
                           web_access_policy = 'disabled'
                       WHERE id = ? AND deleted_at IS NOT NULL""", (entity_id,)
                )
            elif entity_type == "conversation":
                row = connection.execute(
                    "SELECT kind, project_id FROM conversations WHERE id = ? AND deleted_at IS NOT NULL", (entity_id,)
                ).fetchone()
                if row is None:
                    raise CoreDataError("trash_item_not_found", "The trash item does not exist.")
                if row["kind"] == "project_work" and connection.execute(
                    "SELECT 1 FROM projects WHERE id = ? AND deleted_at IS NULL", (row["project_id"],)
                ).fetchone() is None:
                    raise CoreDataError("restore_parent_required", "Restore the parent project first.")
                container_column = "chat_group_id" if row["kind"] == "standard_chat" else "project_id"
                full = connection.execute(
                    f"SELECT {container_column} AS container_id FROM conversations WHERE id = ?", (entity_id,)
                ).fetchone()
                connection.execute(
                    f"""UPDATE conversations SET sort_order = sort_order + 1
                        WHERE deleted_at IS NULL AND kind = ? AND {container_column} IS ?""",
                    (row["kind"], full["container_id"]),
                )
                # A deleted group leaves its conversations ungrouped through the FK.
                cursor = connection.execute(
                    """UPDATE conversations SET deleted_at = NULL, sort_order = 0,
                           web_search_mode = 'inherit', model_selection_mode = 'automatic',
                           provider_profile_id = NULL, model_id = NULL,
                           coordinator_provider_profile_id = NULL, coordinator_model_id = NULL
                       WHERE id = ?""", (entity_id,)
                )
            else:
                raise CoreDataError("invalid_trash_entity", "Trash entity type is not supported.")
            if cursor.rowcount != 1:
                raise CoreDataError("trash_item_not_found", "The trash item does not exist.")
        return {"entity_type": entity_type, "id": entity_id, "restored": True}

    def permanently_delete_trash(self, entity_type: str, entity_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            table = "projects" if entity_type == "project" else "conversations" if entity_type == "conversation" else None
            if table is None:
                raise CoreDataError("invalid_trash_entity", "Trash entity type is not supported.")
            if entity_type == "project":
                candidates = [row[0] for row in connection.execute(
                    """SELECT storage_key FROM (
                         SELECT a.storage_key AS storage_key FROM message_attachments a
                        JOIN messages m ON m.id = a.message_id
                        JOIN conversations c ON c.id = m.conversation_id
                        WHERE c.project_id = ?
                         UNION SELECT ar.storage_key AS storage_key FROM attachment_artifacts ar
                         JOIN message_attachments a ON a.id = ar.attachment_id
                         JOIN messages m ON m.id = a.message_id
                         JOIN conversations c ON c.id = m.conversation_id
                         WHERE c.project_id = ?)""", (entity_id, entity_id),
                ).fetchall()]
            else:
                candidates = [row[0] for row in connection.execute(
                    """SELECT storage_key FROM (
                         SELECT a.storage_key AS storage_key FROM message_attachments a
                         JOIN messages m ON m.id = a.message_id WHERE m.conversation_id = ?
                         UNION SELECT ar.storage_key AS storage_key FROM attachment_artifacts ar
                         JOIN message_attachments a ON a.id = ar.attachment_id
                         JOIN messages m ON m.id = a.message_id
                         WHERE m.conversation_id = ?)""",
                    (entity_id, entity_id),
                ).fetchall()]
            cursor = connection.execute(
                f"DELETE FROM {table} WHERE id = ? AND deleted_at IS NOT NULL", (entity_id,)
            )
            if cursor.rowcount != 1:
                raise CoreDataError("trash_item_not_found", "The trash item does not exist.")
            orphaned = [key for key in candidates if connection.execute(
                """SELECT 1 FROM (
                       SELECT storage_key FROM message_attachments
                       UNION ALL SELECT storage_key FROM attachment_artifacts
                   ) WHERE storage_key = ? LIMIT 1""", (key,)
            ).fetchone() is None]
        return {"entity_type": entity_type, "id": entity_id, "deleted": True,
                "storage_keys": orphaned}

    def empty_trash(self, expired_only: bool = False) -> dict[str, Any]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=TRASH_RETENTION_DAYS)).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")
        predicate = " AND deleted_at <= ?" if expired_only else ""
        parameters = (cutoff,) if expired_only else ()
        with self._connect() as connection:
            project_ids = [row[0] for row in connection.execute(
                f"SELECT id FROM projects WHERE deleted_at IS NOT NULL{predicate}", parameters
            ).fetchall()]
            conversation_ids = [row[0] for row in connection.execute(
                f"SELECT id FROM conversations WHERE deleted_at IS NOT NULL{predicate}", parameters
            ).fetchall()]
            project_placeholders = ",".join("?" for _ in project_ids)
            conversation_placeholders = ",".join("?" for _ in conversation_ids)
            attachment_clauses: list[str] = []
            attachment_parameters: list[str] = []
            if project_ids:
                attachment_clauses.append(f"c.project_id IN ({project_placeholders})")
                attachment_parameters.extend(project_ids)
            if conversation_ids:
                attachment_clauses.append(f"c.id IN ({conversation_placeholders})")
                attachment_parameters.extend(conversation_ids)
            candidates = [] if not attachment_clauses else [row[0] for row in connection.execute(
                f"""SELECT storage_key FROM (
                    SELECT a.storage_key AS storage_key FROM message_attachments a
                    JOIN messages m ON m.id = a.message_id
                    JOIN conversations c ON c.id = m.conversation_id
                    WHERE {' OR '.join(attachment_clauses)}
                    UNION SELECT ar.storage_key AS storage_key FROM attachment_artifacts ar
                    JOIN message_attachments a ON a.id = ar.attachment_id
                    JOIN messages m ON m.id = a.message_id
                    JOIN conversations c ON c.id = m.conversation_id
                    WHERE {' OR '.join(attachment_clauses)})""",
                [*attachment_parameters, *attachment_parameters],
            ).fetchall()]
            connection.execute(f"DELETE FROM conversations WHERE deleted_at IS NOT NULL{predicate}", parameters)
            connection.execute(f"DELETE FROM projects WHERE deleted_at IS NOT NULL{predicate}", parameters)
            orphaned = [key for key in candidates if connection.execute(
                """SELECT 1 FROM (
                       SELECT storage_key FROM message_attachments
                       UNION ALL SELECT storage_key FROM attachment_artifacts
                   ) WHERE storage_key = ? LIMIT 1""", (key,)
            ).fetchone() is None]
        return {"project_ids": project_ids, "conversation_ids": conversation_ids,
                "storage_keys": orphaned}

    @staticmethod
    def _trash_expiry(deleted_at: str) -> str:
        deleted = datetime.fromisoformat(deleted_at.replace("Z", "+00:00"))
        return (deleted + timedelta(days=TRASH_RETENTION_DAYS)).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")

    def behavior_policy(self, conversation_id: str) -> dict[str, Any]:
        category = self.get_conversation(conversation_id)["resolved_category"]
        return {"category": category, **BEHAVIOR_POLICIES[category]}

    def update_conversation_routing(
        self, conversation_id: str, mode: str, preset: str | None = None,
        provider_profile_id: str | None = None, model_id: str | None = None,
    ) -> dict[str, Any]:
        if mode not in {"fixed", "automatic"}:
            raise CoreDataError("invalid_model_selection", "Model selection mode is not supported.")
        if preset is not None and preset not in {"quality", "balanced", "economy", "local_first"}:
            raise CoreDataError("invalid_routing_preset", "Routing preset is not supported.")
        if (provider_profile_id is None) != (model_id is None):
            raise CoreDataError("invalid_model_selection", "Provider and model must be changed together.")
        if mode == "fixed" and (provider_profile_id is None or model_id is None):
            raise CoreDataError(
                "invalid_model_selection", "A fixed route requires a provider and model.",
            )
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id FROM conversations WHERE id = ? AND deleted_at IS NULL", (conversation_id,),
            ).fetchone()
            if row is None:
                raise CoreDataError("conversation_not_found", "The conversation does not exist.")
            active = connection.execute(
                """SELECT 1 FROM model_runs WHERE conversation_id = ? AND status IN
                       ('queued','building_context','waiting_context','waiting_permission',
                        'waiting_budget','generating','waiting_tool') LIMIT 1""",
                (conversation_id,),
            ).fetchone()
            if active:
                raise CoreDataError("run_already_active", "Routing cannot change during an active run.")
            coordinator_provider = provider_profile_id
            coordinator_model = model_id
            changed_at = _utc_now()
            connection.execute(
                """UPDATE conversations SET model_selection_mode = ?,
                       routing_preset = COALESCE(?, routing_preset),
                       coordinator_provider_profile_id = ?, coordinator_model_id = ?,
                       provider_profile_id = CASE WHEN ? = 'fixed' THEN ? ELSE NULL END,
                       model_id = CASE WHEN ? = 'fixed' THEN ? ELSE NULL END,
                       context_generation = context_generation + 1,
                       updated_at = ? WHERE id = ?""",
                (mode, preset, coordinator_provider, coordinator_model,
                 mode, provider_profile_id, mode, model_id, changed_at, conversation_id),
            )
            if mode == "automatic" and provider_profile_id is None:
                label = "Coordinator model reset to the global default."
            elif mode == "automatic":
                label = f"Coordinator model changed to {provider_profile_id}/{model_id}."
            else:
                label = f"Model selection fixed to {provider_profile_id}/{model_id}."
            self._insert_message_with_connection(
                connection, conversation_id, "system_event", label, "coordinator_changed",
            )
        return self.get_conversation(conversation_id)

    def start_model_run(
        self, *, content: str, idempotency_key: str, provider_profile_id: str,
        model_id: str, conversation_id: str | None = None,
        kind: str | None = None, category: str | None = None,
        chat_group_id: str | None = None, project_id: str | None = None,
        initiator_kind: str = "user", parent_run_id: str | None = None,
        selection_source: str = "global_default", selection_reason_code: str = "fixed_selection",
        routing_policy_version: str | None = None,
        web_search_request_mode: str = "off", web_search_route: str = "none",
        web_search_reason_code: str = "web_search_off",
        web_search_policy_version: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        attachment_rejections: list[dict[str, Any]] | None = None,
        model_selection_mode: str = "fixed", routing_preset: str = "balanced",
        edit_message_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist the user action before any slow context or model operation."""
        if edit_message_id is not None and (not isinstance(edit_message_id, str) or not conversation_id):
            raise CoreDataError("invalid_message_edit", "An edit requires a user message and conversation.")
        attachments = self._validate_attachments(attachments or [])
        attachment_rejections = self._validate_attachment_rejections(attachment_rejections or [])
        if content.strip():
            content = self._message_content(content)
        elif attachments or attachment_rejections:
            content = ""
        else:
            raise CoreDataError("invalid_message", "A message or attachment is required.")
        if not idempotency_key or len(idempotency_key) > 200:
            raise CoreDataError("invalid_idempotency_key", "A valid idempotency key is required.")
        if not provider_profile_id or not model_id:
            raise CoreDataError("invalid_model_selection", "A provider profile and model are required.")
        if initiator_kind != "user" or parent_run_id is not None:
            raise CoreDataError("unsupported_initiator", "Stage 3 only allows user-initiated top-level runs.")
        if selection_source not in {"global_default", "conversation_override", "user_once", "router"}:
            raise CoreDataError("invalid_model_selection", "Model selection source is not supported.")
        if model_selection_mode not in {"fixed", "automatic"}:
            raise CoreDataError("invalid_model_selection", "Model selection mode is not supported.")
        if routing_preset not in {"quality", "balanced", "economy", "local_first"}:
            raise CoreDataError("invalid_routing_preset", "Routing preset is not supported.")
        if web_search_request_mode not in {"off", "auto", "force_once"}:
            raise CoreDataError("invalid_web_search_mode", "Web search request mode is not supported.")
        if web_search_route not in {"none", "native", "host_tool", "host_presearch", "specialist"}:
            raise CoreDataError("invalid_web_search_route", "Web search route is not supported.")

        now = _utc_now()
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT id FROM model_runs WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing is not None:
                return self.get_model_run(existing["id"])

            if conversation_id is None:
                if kind not in CONVERSATION_KINDS:
                    raise CoreDataError("invalid_conversation_kind", "Conversation kind is not supported.")
                fallback_name = (
                    attachments[0]["name"] if attachments
                    else attachment_rejections[0]["display_name"]
                    if attachment_rejections else ""
                )
                title = content.splitlines()[0][:80].strip() or fallback_name[:80].strip() or (
                    "New chat" if kind == "standard_chat" else "New work"
                )
                conversation_id = _uuid()
                if kind == "standard_chat":
                    category = _category(category or "")
                    if project_id is not None:
                        raise CoreDataError("invalid_conversation_container", "A standard chat cannot belong to a project.")
                    if chat_group_id is not None and connection.execute(
                        "SELECT 1 FROM chat_groups WHERE id = ?", (chat_group_id,)
                    ).fetchone() is None:
                        raise CoreDataError("chat_group_not_found", "The selected chat group does not exist.")
                    predicate, values = "kind = 'standard_chat' AND chat_group_id IS ?", (chat_group_id,)
                else:
                    category = None
                    if project_id is None or chat_group_id is not None:
                        raise CoreDataError("invalid_conversation_container", "Project work must belong to one project.")
                    if connection.execute(
                        "SELECT 1 FROM projects WHERE id = ? AND deleted_at IS NULL", (project_id,)
                    ).fetchone() is None:
                        raise CoreDataError("project_not_found", "The selected project does not exist.")
                    predicate, values = "kind = 'project_work' AND project_id = ?", (project_id,)
                connection.execute(
                    f"UPDATE conversations SET sort_order = sort_order + 1 WHERE deleted_at IS NULL AND {predicate}",
                    values,
                )
                connection.execute(
                    """INSERT INTO conversations(
                           id, kind, title, category, chat_group_id, project_id, sort_order,
                           created_at, updated_at, deleted_at, provider_profile_id, model_id,
                           model_selection_mode, routing_preset,
                           coordinator_provider_profile_id, coordinator_model_id
                       ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, NULL, ?, ?, ?, ?, ?, ?)""",
                    (conversation_id, kind, title, category, chat_group_id, project_id,
                     now, now,
                     provider_profile_id if model_selection_mode == "fixed" else None,
                     model_id if model_selection_mode == "fixed" else None,
                     model_selection_mode, routing_preset,
                     provider_profile_id, model_id),
                )
            else:
                conversation = connection.execute(
                    """SELECT c.id, c.kind, c.project_id, p.model_access_policy
                       FROM conversations c LEFT JOIN projects p ON p.id = c.project_id
                       WHERE c.id = ? AND c.deleted_at IS NULL
                         AND (c.project_id IS NULL OR p.deleted_at IS NULL)""",
                    (conversation_id,),
                ).fetchone()
                if conversation is None:
                    raise CoreDataError("conversation_not_found", "The conversation does not exist.")
                active = connection.execute(
                    """SELECT 1 FROM model_runs WHERE conversation_id = ?
                       AND status IN ('queued','building_context','waiting_context','waiting_permission',
                                      'waiting_budget','generating','waiting_tool') LIMIT 1""",
                    (conversation_id,),
                ).fetchone()
                if active is not None:
                    raise CoreDataError("run_already_active", "This conversation already has an active generation.")
                if edit_message_id is not None:
                    conversation_id = self._branch_before_message(connection, conversation_id, edit_message_id)
                connection.execute(
                    """UPDATE conversations SET
                           provider_profile_id = CASE WHEN model_selection_mode = 'fixed'
                               THEN COALESCE(provider_profile_id, ?) ELSE NULL END,
                           model_id = CASE WHEN model_selection_mode = 'fixed'
                               THEN COALESCE(model_id, ?) ELSE NULL END,
                           coordinator_provider_profile_id = CASE
                               WHEN model_selection_mode = 'automatic'
                               THEN COALESCE(coordinator_provider_profile_id, ?)
                               ELSE coordinator_provider_profile_id END,
                           coordinator_model_id = CASE
                               WHEN model_selection_mode = 'automatic'
                               THEN COALESCE(coordinator_model_id, ?)
                               ELSE coordinator_model_id END,
                           updated_at = ? WHERE id = ?""",
                    (provider_profile_id, model_id, provider_profile_id, model_id, now, conversation_id),
                )

            user = self._insert_message_with_connection(connection, conversation_id, "user", content)
            for attachment in attachments:
                self._insert_or_clone_message_attachment(
                    connection, user["id"], attachment, now,
                )
            for rejection in attachment_rejections:
                self._insert_or_clone_attachment_rejection(
                    connection, user["id"], rejection, now,
                )
            run_id = _uuid()
            connection.execute(
                """INSERT INTO model_runs(
                       id, conversation_id, user_message_id, status, idempotency_key,
                       provider_profile_id, model_id, initiator_kind, parent_run_id, created_at,
                       selection_source, selection_reason_code, routing_policy_version,
                       web_search_request_mode, web_search_route, web_search_reason_code,
                       web_search_policy_version
                   ) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, conversation_id, user["id"], idempotency_key,
                 provider_profile_id, model_id, initiator_kind, now, selection_source,
                 selection_reason_code, routing_policy_version, web_search_request_mode,
                 web_search_route, web_search_reason_code, web_search_policy_version),
            )
        return self.get_model_run(run_id)

    def get_model_run(self, run_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM model_runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise CoreDataError("run_not_found", "The model run does not exist.")
            return self._model_run_from_row(connection, row)

    def _model_run_from_row(
        self, connection: sqlite3.Connection, row: sqlite3.Row,
    ) -> dict[str, Any]:
            run_id = row["id"]
            attempt = connection.execute(
                "SELECT * FROM run_attempts WHERE run_id = ? ORDER BY attempt_number DESC LIMIT 1", (run_id,)
            ).fetchone()
            draft = connection.execute(
                "SELECT content, updated_at FROM assistant_drafts WHERE run_id = ?", (run_id,)
            ).fetchone()
            message = connection.execute(
                """SELECT m.* FROM messages m JOIN model_runs r ON r.assistant_message_id = m.id
                   WHERE r.id = ?""", (run_id,)
            ).fetchone()
            result = dict(row)
            result["attempt"] = dict(attempt) if attempt else None
            result["draft"] = dict(draft) if draft else None
            result["assistant_message"] = dict(message) if message else None
            if result["assistant_message"] is not None:
                result["assistant_message"]["attachments"] = []
            return result

    def _branch_before_message(
        self, connection: sqlite3.Connection, source_id: str, message_id: str,
    ) -> str:
        """Copy a history prefix atomically; past messages/effects stay intact."""
        source = connection.execute("SELECT * FROM conversations WHERE id = ?", (source_id,)).fetchone()
        messages = self._get_conversation_with_connection(connection, source_id)["messages"]
        target = next((i for i, message in enumerate(messages)
                       if message["id"] == message_id and message["role"] == "user"), None)
        if target is None:
            raise CoreDataError("invalid_message_edit", "Only a user message in this conversation can be edited.")
        branch_id, now = _uuid(), _utc_now()
        container = "chat_group_id" if source["kind"] == "standard_chat" else "project_id"
        connection.execute(
            f"UPDATE conversations SET sort_order = sort_order + 1 WHERE deleted_at IS NULL AND kind = ? AND {container} IS ?",
            (source["kind"], source[container]),
        )
        copied_columns = (
            "kind", "title", "category", "chat_group_id", "project_id", "purpose_override",
            "provider_profile_id", "model_id", "model_selection_mode", "routing_preset",
            "coordinator_provider_profile_id", "coordinator_model_id", "web_search_mode",
        )
        connection.execute(
            f"""INSERT INTO conversations(id, {', '.join(copied_columns)}, sort_order,
                copied_from_conversation_id, copied_through_message_id, copied_at, created_at, updated_at)
                VALUES ({', '.join('?' for _ in range(len(copied_columns) + 7))})""",
            (branch_id, *(source[column] for column in copied_columns), 0, source_id,
             messages[target - 1]["id"] if target else None, now, now, now),
        )
        for original in messages[:target]:
            copied = self._insert_message_with_connection(connection, branch_id, original["role"],
                original["content"], original["event_type"])
            connection.execute("UPDATE messages SET source_message_id = ? WHERE id = ?", (original["id"], copied["id"]))
            for attachment in original["attachments"]:
                self._insert_or_clone_message_attachment(connection, copied["id"], attachment, now)
            for rejection in original["attachment_rejections"]:
                self._insert_or_clone_attachment_rejection(connection, copied["id"],
                    {**rejection, "receipt_id": rejection["id"]}, now)
        return branch_id

    @staticmethod
    def _validate_attachments(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not isinstance(values, list) or len(values) > 8:
            raise CoreDataError("invalid_attachments", "A message can contain at most 8 attachments.")
        total = 0
        result: list[dict[str, Any]] = []
        for value in values:
            if not isinstance(value, dict):
                raise CoreDataError("invalid_attachments", "Attachment metadata is invalid.")
            kind = value.get("kind")
            name = value.get("name")
            media_type = value.get("media_type")
            byte_size = value.get("byte_size")
            sha256 = value.get("sha256")
            storage_key = value.get("storage_key")
            attachment_id = value.get("id")
            if kind not in {"image", "text"} or not all(
                isinstance(item, str) and item for item in
                (name, media_type, sha256, storage_key, attachment_id)
            ) or isinstance(byte_size, bool) or not isinstance(byte_size, int) or byte_size < 0:
                raise CoreDataError("invalid_attachments", "Attachment metadata is invalid.")
            if len(name) > 255 or len(storage_key) > 500 or ".." in storage_key.replace("\\", "/").split("/"):
                raise CoreDataError("invalid_attachments", "Attachment metadata is invalid.")
            limit = 10 * 1024 * 1024 if kind == "image" else 1024 * 1024
            if byte_size > limit:
                raise CoreDataError("attachment_too_large", "An attachment exceeds its size limit.")
            if kind == "image":
                model_key = value.get("model_storage_key")
                model_media = value.get("model_media_type")
                model_size = value.get("model_byte_size")
                model_hash = value.get("model_sha256")
                model_width = value.get("model_width")
                model_height = value.get("model_height")
                normalized = value.get("normalization_version")
                has_model_copy = all(item is not None for item in (
                    model_key, model_media, model_size, model_hash, model_width, model_height, normalized,
                ))
                legacy = normalized == "legacy_passthrough" or (
                    model_key is None and media_type in {"image/png", "image/jpeg"}
                )
                if not has_model_copy and not legacy:
                    raise CoreDataError(
                        "image_normalization_failed", "The image has no safe model-ready copy.",
                    )
                if has_model_copy:
                    if model_media not in {"image/png", "image/jpeg"} \
                            or not isinstance(model_key, str) or not model_key \
                            or not isinstance(model_hash, str) or not model_hash \
                            or isinstance(model_size, bool) or not isinstance(model_size, int) \
                            or not 0 < model_size <= 10 * 1024 * 1024 \
                            or isinstance(model_width, bool) or not isinstance(model_width, int) \
                            or isinstance(model_height, bool) or not isinstance(model_height, int) \
                            or not 0 < model_width <= 4096 or not 0 < model_height <= 4096 \
                            or ".." in model_key.replace("\\", "/").split("/"):
                        raise CoreDataError(
                            "image_normalization_failed", "Normalized image metadata is invalid.",
                        )
            artifacts = value.get("artifacts", [])
            if not isinstance(artifacts, list) or len(artifacts) > 4:
                raise CoreDataError("attachment_artifact_invalid", "Attachment artifacts are invalid.")
            artifact_ids: set[str] = set()
            for artifact in artifacts:
                if not isinstance(artifact, dict):
                    raise CoreDataError("attachment_artifact_invalid", "Attachment artifacts are invalid.")
                artifact_id = artifact.get("artifact_id")
                artifact_kind = artifact.get("kind")
                artifact_media = artifact.get("media_type")
                artifact_size = artifact.get("byte_size")
                artifact_hash = artifact.get("sha256")
                artifact_key = artifact.get("storage_key")
                processor_id = artifact.get("processor_id")
                processor_version = artifact.get("processor_version")
                if artifact_kind not in {"text.utf8", "image.normalized"} or not all(
                    isinstance(item, str) and item for item in (
                        artifact_id, artifact_media, artifact_hash, artifact_key,
                        processor_id, processor_version,
                    )
                ) or isinstance(artifact_size, bool) or not isinstance(artifact_size, int) \
                        or not 0 <= artifact_size <= 10 * 1024 * 1024:
                    raise CoreDataError("attachment_artifact_invalid", "Attachment artifacts are invalid.")
                if artifact_id in artifact_ids or len(artifact_key) > 500 \
                        or ".." in artifact_key.replace("\\", "/").split("/"):
                    raise CoreDataError("attachment_artifact_invalid", "Attachment artifacts are invalid.")
                if artifact_kind == "image.normalized" and artifact_media not in {"image/png", "image/jpeg"}:
                    raise CoreDataError("attachment_artifact_invalid", "Image artifact format is invalid.")
                artifact_ids.add(artifact_id)
            total += byte_size
            result.append(dict(value))
        if total > 25 * 1024 * 1024:
            raise CoreDataError("attachments_too_large", "Attachments exceed the per-message size limit.")
        return result

    @staticmethod
    def _insert_or_clone_message_attachment(
        connection: sqlite3.Connection, message_id: str,
        attachment: dict[str, Any], created_at: str,
    ) -> str:
        """Attach an immutable staged source without assuming it is single-use.

        A retry may legitimately submit the same Host-owned staging descriptor again.
        Message attachment rows are message-local, so a previously committed source is
        represented by a new row that references the original and reuses its immutable
        storage objects. This mirrors conversation snapshot copying and keeps cleanup
        reference-aware.
        """
        existing = connection.execute(
            "SELECT * FROM message_attachments WHERE id = ?", (attachment["id"],),
        ).fetchone()
        if attachment.get("reference_attachment_id"):
            scope = connection.execute(
                """SELECT 1 FROM message_attachments a JOIN messages source ON source.id = a.message_id
                   JOIN messages current ON current.conversation_id = source.conversation_id
                   WHERE a.id = ? AND current.id = ?""", (attachment["id"], message_id),
            ).fetchone()
            if scope is None:
                raise CoreDataError("attachment_reference_forbidden", "The attachment reference is no longer authorized.")
        attachment_id = attachment["id"]
        source_attachment_id: str | None = None
        artifacts = attachment.get("artifacts", [])
        if existing is not None:
            immutable_fields = (
                "kind", "name", "media_type", "byte_size", "sha256",
                "storage_key", "width", "height",
            )
            if any(existing[field] != attachment.get(field) for field in immutable_fields):
                raise CoreDataError(
                    "attachment_integrity_failed",
                    "The reused attachment no longer matches its stored source.",
                )
            stored_artifacts = connection.execute(
                """SELECT * FROM attachment_artifacts
                   WHERE attachment_id = ? ORDER BY created_at, rowid""",
                (attachment_id,),
            ).fetchall()
            if artifacts and len(artifacts) != len(stored_artifacts):
                raise CoreDataError(
                    "attachment_integrity_failed",
                    "The reused attachment artifacts no longer match their stored source.",
                )
            artifact_identity_fields = (
                "kind", "processor_id", "processor_version", "media_type",
                "byte_size", "sha256", "storage_key", "width", "height",
            )
            if artifacts and any(
                any(stored[field] != supplied.get(field) for field in artifact_identity_fields)
                for stored, supplied in zip(stored_artifacts, artifacts, strict=True)
            ):
                raise CoreDataError(
                    "attachment_integrity_failed",
                    "The reused attachment artifacts failed integrity validation.",
                )
            attachment_id = _uuid()
            source_attachment_id = existing["source_attachment_id"] or existing["id"]
            artifacts = [dict(item) for item in stored_artifacts]

        connection.execute(
            """INSERT INTO message_attachments(
                   id, message_id, kind, name, media_type, byte_size, sha256,
                   storage_key, width, height, source_attachment_id, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (attachment_id, message_id, attachment["kind"], attachment["name"],
             attachment["media_type"], attachment["byte_size"], attachment["sha256"],
             attachment["storage_key"], attachment.get("width"), attachment.get("height"),
             source_attachment_id, created_at),
        )
        for artifact in artifacts:
            artifact_id = _uuid() if existing is not None else artifact["artifact_id"]
            if connection.execute(
                "SELECT 1 FROM attachment_artifacts WHERE id = ?", (artifact_id,),
            ).fetchone() is not None:
                raise CoreDataError(
                    "attachment_integrity_failed",
                    "The attachment artifact identifier is already in use.",
                )
            connection.execute(
                """INSERT INTO attachment_artifacts(
                       id, attachment_id, kind, processor_id, processor_version,
                       media_type, byte_size, sha256, storage_key, width, height,
                       trust, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (artifact_id, attachment_id,
                 artifact["kind"], artifact["processor_id"], artifact["processor_version"],
                 artifact["media_type"], artifact["byte_size"], artifact["sha256"],
                 artifact["storage_key"], artifact.get("width"), artifact.get("height"),
                 artifact.get("trust", "external_untrusted"), created_at),
            )
        return attachment_id

    @staticmethod
    def _insert_or_clone_attachment_rejection(
        connection: sqlite3.Connection, message_id: str,
        rejection: dict[str, Any], created_at: str,
    ) -> str:
        existing = connection.execute(
            "SELECT * FROM message_attachment_rejections WHERE id = ?",
            (rejection["receipt_id"],),
        ).fetchone()
        receipt_id = rejection["receipt_id"]
        if existing is not None:
            fields = ("display_name", "detected_media_type", "error_code", "processor_hint")
            if any(existing[field] != rejection.get(field) for field in fields):
                raise CoreDataError(
                    "attachment_integrity_failed",
                    "The reused attachment rejection no longer matches its receipt.",
                )
            receipt_id = _uuid()
        connection.execute(
            """INSERT INTO message_attachment_rejections(
                   id, message_id, display_name, detected_media_type, error_code,
                   processor_hint, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (receipt_id, message_id, rejection["display_name"],
             rejection.get("detected_media_type"), rejection["error_code"],
             rejection.get("processor_hint"), created_at),
        )
        return receipt_id

    @staticmethod
    def _validate_attachment_rejections(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not isinstance(values, list) or len(values) > 8:
            raise CoreDataError(
                "invalid_attachments", "A message can contain at most 8 rejected attachments."
            )
        result: list[dict[str, Any]] = []
        for value in values:
            if not isinstance(value, dict):
                raise CoreDataError("invalid_attachments", "Attachment rejection metadata is invalid.")
            receipt_id = value.get("receipt_id")
            name = value.get("display_name")
            error_code = value.get("error_code")
            media_type = value.get("detected_media_type")
            hint = value.get("processor_hint")
            if not all(isinstance(item, str) and item for item in (receipt_id, name, error_code)):
                raise CoreDataError("invalid_attachments", "Attachment rejection metadata is invalid.")
            if len(name) > 255 or len(error_code) > 80 or any(ord(character) < 32 for character in name):
                raise CoreDataError("invalid_attachments", "Attachment rejection metadata is invalid.")
            if media_type is not None and (not isinstance(media_type, str) or len(media_type) > 120):
                raise CoreDataError("invalid_attachments", "Attachment rejection metadata is invalid.")
            if hint is not None and (not isinstance(hint, str) or len(hint) > 120):
                raise CoreDataError("invalid_attachments", "Attachment rejection metadata is invalid.")
            result.append(dict(value))
        return result

    def get_model_run_by_idempotency_key(self, idempotency_key: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id FROM model_runs WHERE idempotency_key = ?", (idempotency_key,),
            ).fetchone()
        return self.get_model_run(row["id"]) if row is not None else None

    def active_model_runs(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT id FROM model_runs
                   WHERE status IN ('queued','building_context','waiting_context','waiting_permission',
                                    'waiting_budget','generating','waiting_tool')"""
            ).fetchall()
        return [self.get_model_run(row["id"]) for row in rows]

    def app_update_readiness(self) -> dict[str, int]:
        with self._connect() as connection:
            runs = connection.execute("""SELECT COUNT(*) FROM model_runs WHERE status IN
                ('queued','building_context','waiting_context','waiting_permission',
                 'waiting_budget','generating','waiting_tool')""").fetchone()[0]
            tools = connection.execute("""SELECT COUNT(*) FROM tool_operations WHERE status IN
                ('requested','validating','reviewing','waiting_user','executing')""").fetchone()[0]
        return {"active_runs": runs, "active_tools": tools}

    def flush_for_app_update(self) -> None:
        with self._connect() as connection:
            result = connection.execute("PRAGMA wal_checkpoint(FULL)").fetchone()
            if result and result[0]:
                raise CoreDataError("app_update_busy", "Local data is still being saved.")

    def latest_model_run(self, conversation_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id FROM model_runs WHERE conversation_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
        return self.get_model_run(row["id"]) if row else None

    def transition_model_run(
        self, run_id: str, expected: set[str], status: str,
        error_code: str | None = None, error_message: str | None = None,
    ) -> dict[str, Any]:
        now = _utc_now()
        placeholders = ",".join("?" for _ in expected)
        finished = now if status in {"completed", "failed", "cancelled", "interrupted"} else None
        started = now if status in {"building_context", "generating"} else None
        with self._connect() as connection:
            cursor = connection.execute(
                f"""UPDATE model_runs SET status = ?, error_code = ?, error_message = ?,
                       started_at = COALESCE(started_at, ?), finished_at = ?
                   WHERE id = ? AND status IN ({placeholders})""",
                (status, error_code, error_message, started, finished, run_id, *sorted(expected)),
            )
            if cursor.rowcount != 1:
                exists = connection.execute("SELECT 1 FROM model_runs WHERE id = ?", (run_id,)).fetchone()
                code = "invalid_run_transition" if exists else "run_not_found"
                raise CoreDataError(code, "The model run cannot enter the requested state.")
        return self.get_model_run(run_id)

    def continue_context_run(self, run_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE model_runs SET status = 'queued', automatic_compaction_used = 0,
                       error_code = NULL, error_message = NULL, finished_at = NULL
                   WHERE id = ? AND status = 'waiting_context'""", (run_id,),
            )
            if cursor.rowcount != 1:
                exists = connection.execute("SELECT 1 FROM model_runs WHERE id = ?", (run_id,)).fetchone()
                raise CoreDataError(
                    "invalid_run_transition" if exists else "run_not_found",
                    "Only a run waiting for context can continue.",
                )
        return self.get_model_run(run_id)

    def create_expanded_retry(self, parent_run_id: str) -> dict[str, Any]:
        return self._create_linked_retry(
            parent_run_id,
            idempotency_prefix="expanded-retry",
            context_strategy="expanded_retry",
            selection_reason_code="expanded_history_retry",
        )

    def create_web_disabled_retry(self, parent_run_id: str) -> dict[str, Any]:
        return self._create_linked_retry(
            parent_run_id,
            idempotency_prefix="web-disabled-retry",
            context_strategy="default",
            selection_reason_code="retry_without_web_search",
            disable_web_search=True,
        )

    def _create_linked_retry(
        self, parent_run_id: str, *, idempotency_prefix: str,
        context_strategy: str, selection_reason_code: str,
        disable_web_search: bool = False,
    ) -> dict[str, Any]:
        now = _utc_now()
        with self._connect() as connection:
            parent = connection.execute(
                "SELECT * FROM model_runs WHERE id = ?", (parent_run_id,),
            ).fetchone()
            if parent is None:
                raise CoreDataError("run_not_found", "The model run does not exist.")
            if parent["status"] not in {"completed", "failed", "cancelled", "interrupted"}:
                raise CoreDataError("run_not_regenerable", "Only a finished run can use more history.")
            if disable_web_search and (
                parent["status"] != "failed" or parent["web_search_route"] == "none"
            ):
                raise CoreDataError(
                    "run_not_regenerable",
                    "Only a failed run that used web search can retry without web search.",
                )
            active = connection.execute(
                """SELECT 1 FROM model_runs WHERE conversation_id = ? AND status IN
                   ('queued','building_context','waiting_context','waiting_permission',
                    'waiting_budget','generating','waiting_tool') LIMIT 1""",
                (parent["conversation_id"],),
            ).fetchone()
            if active is not None:
                raise CoreDataError("run_already_active", "This conversation already has an active generation.")
            latest_user = connection.execute(
                """SELECT id FROM messages WHERE conversation_id = ? AND role = 'user'
                   ORDER BY sequence_no DESC LIMIT 1""", (parent["conversation_id"],),
            ).fetchone()
            if latest_user is None or latest_user["id"] != parent["user_message_id"]:
                raise CoreDataError("run_not_regenerable", "Only the latest user request can use more history.")
            idempotency_key = f"{idempotency_prefix}:{parent_run_id}"
            existing = connection.execute(
                "SELECT id FROM model_runs WHERE idempotency_key = ?", (idempotency_key,),
            ).fetchone()
            if existing is not None:
                return self.get_model_run(existing["id"])
            run_id = _uuid()
            generation = connection.execute(
                "SELECT context_generation FROM conversations WHERE id = ?", (parent["conversation_id"],),
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO model_runs(
                       id, conversation_id, user_message_id, status, idempotency_key,
                       provider_profile_id, model_id, initiator_kind, parent_run_id, created_at,
                       selection_source, selection_reason_code, routing_policy_version,
                       context_strategy, context_generation, automatic_compaction_used,
                       web_search_request_mode, web_search_route, web_search_reason_code,
                       web_search_policy_version
                   ) VALUES (?, ?, ?, 'queued', ?, ?, ?, 'user', ?, ?, ?, ?, ?,
                             ?, ?, 0, ?, ?, ?, ?)""",
                (run_id, parent["conversation_id"], parent["user_message_id"], idempotency_key,
                 parent["provider_profile_id"], parent["model_id"], parent_run_id, now,
                 parent["selection_source"], selection_reason_code,
                 parent["routing_policy_version"], context_strategy, generation,
                 "off" if disable_web_search else parent["web_search_request_mode"],
                 "none" if disable_web_search else parent["web_search_route"],
                 "user_retry_without_web" if disable_web_search else parent["web_search_reason_code"],
                 parent["web_search_policy_version"]),
            )
        return self.get_model_run(run_id)

    def begin_run_attempt(self, run_id: str, execution_step_id: str | None = None) -> dict[str, Any]:
        with self._connect() as connection:
            number = connection.execute(
                "SELECT COALESCE(MAX(attempt_number), 0) + 1 FROM run_attempts WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
            attempt = {"id": _uuid(), "run_id": run_id, "attempt_number": number,
                       "started_at": _utc_now(), "finished_at": None,
                       "produced_output": 0, "error_code": None}
            connection.execute(
                """INSERT INTO run_attempts(id, run_id, attempt_number, started_at,
                       produced_output, execution_step_id) VALUES (?, ?, ?, ?, 0, ?)""",
                (attempt["id"], run_id, number, attempt["started_at"], execution_step_id),
            )
            connection.execute("DELETE FROM assistant_drafts WHERE run_id = ?", (run_id,))
        return attempt

    def bind_attempt_invocation_spec(self, attempt_id: str, invocation_spec_id: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE run_attempts SET invocation_spec_id = ?
                   WHERE id = ? AND invocation_spec_id IS NULL""",
                (invocation_spec_id, attempt_id),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    "SELECT invocation_spec_id FROM run_attempts WHERE id = ?", (attempt_id,),
                ).fetchone()
                if row is None or row["invocation_spec_id"] != invocation_spec_id:
                    raise CoreDataError(
                        "invocation_spec_invalid",
                        "The run attempt is already bound to another invocation specification.",
                    )

    def checkpoint_draft(self, run_id: str, attempt_id: str, content: str) -> None:
        now = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO assistant_drafts(run_id, attempt_id, content, updated_at)
                   VALUES (?, ?, ?, ?) ON CONFLICT(run_id) DO UPDATE SET
                   attempt_id = excluded.attempt_id, content = excluded.content,
                   updated_at = excluded.updated_at""", (run_id, attempt_id, content, now),
            )
            connection.execute(
                "UPDATE run_attempts SET produced_output = ? WHERE id = ?",
                (1 if content else 0, attempt_id),
            )

    def finish_attempt(self, attempt_id: str, error_code: str | None = None) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE run_attempts SET finished_at = ?, error_code = ? WHERE id = ?",
                (_utc_now(), error_code, attempt_id),
            )

    def update_attempt_usage(self, attempt_id: str, usage: dict[str, Any]) -> None:
        def optional_nonnegative(name: str) -> int | None:
            value = usage.get(name)
            return value if isinstance(value, int) and value >= 0 else None

        cost = usage.get("estimated_cost")
        estimated_cost = cost if isinstance(cost, (int, float)) and math.isfinite(cost) and cost >= 0 else None

        with self._connect() as connection:
            connection.execute(
                """UPDATE run_attempts SET
                       input_tokens = COALESCE(?, input_tokens),
                       output_tokens = COALESCE(?, output_tokens),
                       cached_input_tokens = COALESCE(?, cached_input_tokens),
                       estimated_cost = COALESCE(?, estimated_cost)
                   WHERE id = ?""",
                (optional_nonnegative("input_tokens"), optional_nonnegative("output_tokens"),
                 optional_nonnegative("cached_input_tokens"), estimated_cost, attempt_id),
            )

    def complete_model_run(self, run_id: str, attempt_id: str, content: str) -> dict[str, Any]:
        # The input-editor limit must not reject an already generated answer.
        content = content.strip()
        if not content:
            raise CoreDataError("invalid_message", "The model response must contain text.")
        try:
            content.encode("utf-8")
        except UnicodeEncodeError as error:
            raise CoreDataError("invalid_text_encoding", "Text contains an invalid Unicode character.") from error
        now = _utc_now()
        with self._connect() as connection:
            run = connection.execute(
                "SELECT conversation_id, status FROM model_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if run is None or run["status"] != "generating":
                raise CoreDataError("invalid_run_transition", "Only a generating run can complete.")
            assistant = self._insert_message_with_connection(
                connection, run["conversation_id"], "assistant", content
            )
            connection.execute(
                "UPDATE run_attempts SET finished_at = ? WHERE id = ?", (now, attempt_id)
            )
            connection.execute("DELETE FROM assistant_drafts WHERE run_id = ?", (run_id,))
            connection.execute(
                """UPDATE model_runs SET status = 'completed', finished_at = ?, assistant_message_id = ?,
                       error_code = NULL, error_message = NULL WHERE id = ?""", (now, assistant["id"], run_id)
            )
        result = self.get_model_run(run_id)
        result["assistant_message"] = assistant
        return result

    def interrupt_active_runs(self) -> list[str]:
        now = _utc_now()
        with self._connect() as connection:
            ids = [row["id"] for row in connection.execute(
                """SELECT id FROM model_runs
                   WHERE status IN ('queued','building_context','waiting_permission',
                                    'waiting_budget','generating','waiting_tool')"""
            ).fetchall()]
            connection.execute(
                """UPDATE model_runs SET status = 'interrupted', error_code = 'core_interrupted',
                       error_message = 'The Core stopped before generation completed.', finished_at = ?
                   WHERE status IN ('queued','building_context','waiting_permission',
                                    'waiting_budget','generating','waiting_tool')""", (now,)
            )
            connection.execute(
                """UPDATE web_operations SET status = 'unknown_outcome',
                       error_code = 'search_unknown_outcome', finished_at = ?
                   WHERE status IN ('dispatched','searching')""", (now,),
            )
            connection.execute(
                """UPDATE web_operations SET status = 'interrupted',
                       error_code = 'core_interrupted', finished_at = ?
                   WHERE status = 'reading_sources'""", (now,),
            )
        return ids

    def save_context_snapshot(
        self, run_id: str, items: list[dict[str, Any]], token_budget: int,
        estimated_tokens: int, content_hash: str,
    ) -> str:
        snapshot_id, now = _uuid(), _utc_now()
        with self._connect() as connection:
            run = connection.execute(
                """SELECT r.context_generation, c.context_generation AS current_generation
                   FROM model_runs r JOIN conversations c ON c.id = r.conversation_id
                   WHERE r.id = ?""", (run_id,),
            ).fetchone()
            if run is None:
                raise CoreDataError("run_not_found", "The model run does not exist.")
            connection.execute(
                """INSERT INTO context_snapshots(
                       id, run_id, round_no, assembler_version, policy_version, policy_json,
                       context_generation, context_window, input_target, output_reserve,
                       token_method, estimated_tokens, stable_prefix_hash, dynamic_suffix_hash,
                       content_hash, retrieval_confidence, compression_applied, omitted_items, created_at
                   ) VALUES (?, ?, 0, 'context-engine-v2-compat', 'context-default-v1', '{}',
                             ?, ?, ?, 0, 'conservative-char-v2', ?, '', '', ?, 'none', 0, 0, ?)""",
                (snapshot_id, run_id, run["current_generation"], token_budget, token_budget,
                 estimated_tokens, content_hash, now),
            )
            for position, item in enumerate(items):
                connection.execute(
                    """INSERT INTO context_items(
                           id, snapshot_id, source_type, source_id, position, estimated_tokens,
                           included, exclusion_reason, section, representation, trust,
                           derived_from_untrusted, metadata_json
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'dynamic', 'original', 'user_authored', 0, '{}')""",
                    (_uuid(), snapshot_id, item["source_type"], item.get("source_id"), position,
                     item["estimated_tokens"], 1 if item.get("included", True) else 0,
                     item.get("exclusion_reason")),
                )
            connection.execute(
                "UPDATE model_runs SET context_snapshot_id = ? WHERE id = ?", (snapshot_id, run_id)
            )
        return snapshot_id

    def context_snapshot(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            snapshot = connection.execute(
                "SELECT * FROM context_snapshots WHERE run_id = ? ORDER BY round_no DESC LIMIT 1", (run_id,)
            ).fetchone()
            if snapshot is None:
                return None
            items = connection.execute(
                "SELECT * FROM context_items WHERE snapshot_id = ? ORDER BY position", (snapshot["id"],)
            ).fetchall()
        result = dict(snapshot)
        result["items"] = [dict(item) for item in items]
        return result

    def latest_conversation_summary(self, conversation_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM conversation_summaries_legacy_v8 WHERE conversation_id = ?
                   ORDER BY version DESC, created_at DESC LIMIT 1""", (conversation_id,)
            ).fetchone()
        return dict(row) if row else None

    def save_conversation_summary(
        self, conversation_id: str, through_message_id: str, summary: str,
        provider_profile_id: str, model_id: str,
    ) -> dict[str, Any]:
        summary = self._message_content(summary)
        with self._connect() as connection:
            existing = connection.execute(
                """SELECT * FROM conversation_summaries_legacy_v8 WHERE conversation_id = ?
                   AND through_message_id = ? ORDER BY version DESC LIMIT 1""",
                (conversation_id, through_message_id),
            ).fetchone()
            if existing:
                return dict(existing)
            version = connection.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM conversation_summaries_legacy_v8 WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()[0]
            item = {"id": _uuid(), "conversation_id": conversation_id,
                    "through_message_id": through_message_id, "summary": summary,
                    "provider_profile_id": provider_profile_id, "model_id": model_id,
                    "version": version, "created_at": _utc_now()}
            connection.execute(
                """INSERT INTO conversation_summaries_legacy_v8(id, conversation_id, through_message_id,
                       summary, provider_profile_id, model_id, version, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", tuple(item.values())
            )
        return item

    def update_conversation_model(
        self, conversation_id: str, provider_profile_id: str | None, model_id: str | None,
    ) -> dict[str, Any]:
        if (provider_profile_id is None) != (model_id is None):
            raise CoreDataError("invalid_model_selection", "Provider and model overrides must be set together.")
        return self.update_conversation_routing(
            conversation_id,
            "automatic" if provider_profile_id is None else "fixed",
            provider_profile_id=provider_profile_id,
            model_id=model_id,
        )

    def update_project_model_policy(self, project_id: str, policy: str) -> dict[str, Any]:
        if policy not in {"local_only", "allow_online"}:
            raise CoreDataError("invalid_model_policy", "Project model policy is not supported.")
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE projects SET model_access_policy = ?, updated_at = ?
                   WHERE id = ? AND deleted_at IS NULL""", (policy, _utc_now(), project_id)
            )
            if cursor.rowcount != 1:
                raise CoreDataError("project_not_found", "The project does not exist.")
            connection.execute(
                """UPDATE conversations SET context_generation = context_generation + 1
                   WHERE project_id = ? AND kind = 'project_work' AND deleted_at IS NULL""",
                (project_id,),
            )
        return next(project for project in self.list_projects() if project["id"] == project_id)

    def update_project_web_access(self, project_id: str, policy: str) -> dict[str, Any]:
        if policy not in {"disabled", "allow_search"}:
            raise CoreDataError("invalid_web_access_policy", "Project web access policy is not supported.")
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE projects SET web_access_policy = ?, updated_at = ?
                   WHERE id = ? AND deleted_at IS NULL""",
                (policy, _utc_now(), project_id),
            )
            if cursor.rowcount != 1:
                raise CoreDataError("project_not_found", "The project does not exist.")
            connection.execute(
                """UPDATE conversations SET context_generation = context_generation + 1,
                       web_search_mode = CASE WHEN ? = 'disabled' THEN 'inherit' ELSE web_search_mode END
                   WHERE project_id = ? AND kind = 'project_work' AND deleted_at IS NULL""",
                (policy, project_id),
            )
        return next(project for project in self.list_projects() if project["id"] == project_id)

    def update_conversation_web_search(self, conversation_id: str, mode: str) -> dict[str, Any]:
        if mode not in {"inherit", "off", "auto"}:
            raise CoreDataError("invalid_web_search_mode", "Conversation web search mode is not supported.")
        conversation = self.get_conversation(conversation_id)
        if conversation["kind"] == "project_work" \
                and mode == "auto" and conversation.get("web_access_policy") != "allow_search":
            raise CoreDataError("project_web_access_denied", "This project has not allowed web search.")
        with self._connect() as connection:
            connection.execute(
                """UPDATE conversations SET web_search_mode = ?, updated_at = ?,
                       context_generation = context_generation + 1 WHERE id = ?""",
                (mode, _utc_now(), conversation_id),
            )
        return self.get_conversation(conversation_id)

    def create_web_operation(
        self, run_id: str, sequence: int, idempotency_key: str, route: str,
        backend_id: str | None, query_text: str, operation_id: str | None = None,
    ) -> dict[str, Any]:
        if route not in {"native", "host_tool", "host_presearch"}:
            raise CoreDataError("invalid_web_search_route", "Web search route is not supported.")
        query = query_text.strip()
        if not query or len(query) > 500:
            raise CoreDataError("search_result_invalid", "Web search query must contain 1 to 500 characters.")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            raise CoreDataError("invalid_web_operation", "Web operation sequence is invalid.")
        now, operation_id = _utc_now(), operation_id or _uuid()
        try:
            with self._connect() as connection:
                run = connection.execute(
                    "SELECT web_search_route FROM model_runs WHERE id = ?", (run_id,),
                ).fetchone()
                if run is None:
                    raise CoreDataError("run_not_found", "The model run does not exist.")
                if run["web_search_route"] != route \
                        and not (run["web_search_route"] == "specialist" and route == "native"):
                    raise CoreDataError("web_search_route_mismatch", "The web operation does not match the run route.")
                connection.execute(
                    """INSERT INTO web_operations(
                           id, run_id, sequence, idempotency_key, route, backend_id,
                           query_text, status, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, 'prepared', ?)""",
                    (operation_id, run_id, sequence, idempotency_key, route, backend_id, query, now),
                )
        except sqlite3.IntegrityError:
            with self._connect() as connection:
                existing = connection.execute(
                    "SELECT * FROM web_operations WHERE idempotency_key = ?", (idempotency_key,),
                ).fetchone()
            if existing is None:
                raise CoreDataError("invalid_web_operation", "Web operation conflicts with existing state.")
            return dict(existing)
        return self.get_web_operation(operation_id)

    def get_web_operation(self, operation_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT o.*, r.conversation_id, r.user_message_id,
                          r.provider_profile_id, r.model_id
                   FROM web_operations o JOIN model_runs r ON r.id = o.run_id
                   WHERE o.id = ?""", (operation_id,),
            ).fetchone()
            if row is None:
                raise CoreDataError("web_operation_not_found", "The web operation does not exist.")
            result = dict(row)
            result["usage"] = json.loads(result.pop("usage_json") or "{}")
            sources = connection.execute(
                "SELECT * FROM web_sources WHERE operation_id = ? ORDER BY position", (operation_id,),
            ).fetchall()
            result["sources"] = [dict(source) for source in sources]
            return result

    def transition_web_operation(
        self, operation_id: str, status: str, *, error_code: str | None = None,
    ) -> dict[str, Any]:
        try:
            target = WebOperationStatus(status)
        except ValueError as error:
            raise CoreDataError("invalid_web_operation_transition", "Web operation status is invalid.") from error
        with self._connect() as connection:
            current = connection.execute(
                "SELECT status FROM web_operations WHERE id = ?", (operation_id,),
            ).fetchone()
            if current is None:
                raise CoreDataError("web_operation_not_found", "The web operation does not exist.")
            current_status = WebOperationStatus(current["status"])
            if target not in WEB_OPERATION_TRANSITIONS[current_status]:
                raise CoreDataError(
                    "invalid_web_operation_transition", "The web operation cannot enter that state."
                )
            dispatched_at = _utc_now() if target is WebOperationStatus.DISPATCHED else None
            finished_at = _utc_now() if not WEB_OPERATION_TRANSITIONS[target] else None
            connection.execute(
                """UPDATE web_operations SET status = ?, error_code = ?,
                       dispatched_at = COALESCE(dispatched_at, ?), finished_at = ? WHERE id = ?""",
                (target.value, error_code, dispatched_at, finished_at, operation_id),
            )
        return self.get_web_operation(operation_id)

    def update_web_operation_query(self, operation_id: str, query_text: str) -> dict[str, Any]:
        query = query_text.strip()
        if not query or len(query) > 500:
            raise CoreDataError("search_result_invalid", "Web search query must contain 1 to 500 characters.")
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE web_operations SET query_text = ?
                   WHERE id = ? AND status IN ('prepared','dispatched','searching','reading_sources')""",
                (query, operation_id),
            )
            if cursor.rowcount != 1:
                exists = connection.execute(
                    "SELECT 1 FROM web_operations WHERE id = ?", (operation_id,),
                ).fetchone()
                raise CoreDataError(
                    "invalid_web_operation_transition" if exists else "web_operation_not_found",
                    "The web operation query can no longer be updated.",
                )
        return self.get_web_operation(operation_id)

    def complete_web_operation(
        self, operation_id: str, sources: list[dict[str, Any]], usage: dict[str, Any] | None = None,
        *, provider_reference_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(sources, list) or len(sources) > 20:
            raise CoreDataError("search_result_invalid", "Web search returned an invalid number of sources.")
        now = _utc_now()
        verified_sources: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for source in sources:
            if not isinstance(source, dict):
                raise CoreDataError("search_result_invalid", "A web source is invalid.")
            title, url = source.get("title"), source.get("url")
            if not all(isinstance(value, str) and value for value in (title, url)):
                raise CoreDataError("search_result_invalid", "A web source is invalid.")
            parsed = urlsplit(url)
            if parsed.scheme != "https" or not parsed.hostname \
                    or parsed.username is not None or parsed.password is not None:
                raise CoreDataError("web_url_blocked", "A web source URL is not allowed.")
            try:
                address = ipaddress.ip_address(parsed.hostname)
            except ValueError:
                address = None
            if address is not None and (
                not address.is_global or address.is_private or address.is_loopback
                or address.is_link_local or address.is_reserved
            ):
                raise CoreDataError("web_url_blocked", "A web source URL is not allowed.")
            canonical_url = urlunsplit((
                "https", parsed.netloc.lower(), parsed.path or "/", parsed.query, "",
            ))
            if canonical_url in seen_urls:
                continue
            seen_urls.add(canonical_url)
            verified_sources.append({**source, "url": canonical_url})
        with self._connect() as connection:
            current = connection.execute(
                "SELECT status FROM web_operations WHERE id = ?", (operation_id,),
            ).fetchone()
            if current is None:
                raise CoreDataError("web_operation_not_found", "The web operation does not exist.")
            current_status = WebOperationStatus(current["status"])
            if WebOperationStatus.COMPLETED not in WEB_OPERATION_TRANSITIONS[current_status]:
                raise CoreDataError("invalid_web_operation_transition", "The web operation cannot complete.")
            connection.execute("DELETE FROM web_sources WHERE operation_id = ?", (operation_id,))
            for position, source in enumerate(verified_sources):
                title, url = source.get("title"), source.get("url")
                if not all(isinstance(value, str) and value for value in (title, url)):
                    raise CoreDataError("search_result_invalid", "A web source is invalid.")
                parsed = urlsplit(url)
                if parsed.scheme != "https" or not parsed.hostname \
                        or parsed.username is not None or parsed.password is not None:
                    raise CoreDataError("web_url_blocked", "A web source URL is not allowed.")
                domain = parsed.hostname
                snippet = source.get("snippet")
                if snippet is not None and (not isinstance(snippet, str) or len(snippet) > 20_000):
                    raise CoreDataError("search_result_invalid", "A web source snippet is invalid.")
                connection.execute(
                    """INSERT INTO web_sources(
                           id, operation_id, position, title, url, domain, snippet,
                           published_at, retrieved_at, content_hash, provider_source_id, trust
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'external_untrusted')""",
                    (_uuid(), operation_id, position, title[:500], url[:4000], domain[:255], snippet,
                     source.get("published_at"), source.get("retrieved_at") or now,
                     source.get("content_hash"), source.get("provider_source_id")),
                )
            source_manifest = [{
                "title": source.get("title"), "url": source.get("url"),
                "provider_source_id": source.get("provider_source_id"),
            } for source in verified_sources]
            manifest_hash = (
                hashlib.sha256(json.dumps(
                    source_manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                ).encode("utf-8")).hexdigest()
                if source_manifest else None
            )
            evidence_status = (
                "traceable_sources" if verified_sources else
                "provider_reference" if provider_reference_id else "missing"
            )
            connection.execute(
                """UPDATE web_operations SET status = 'completed', result_count = ?,
                       verified_source_count = ?, evidence_status = ?,
                       evidence_manifest_hash = ?, provider_reference_id = ?,
                       usage_json = ?, error_code = NULL, finished_at = ? WHERE id = ?""",
                (len(verified_sources), len(verified_sources), evidence_status, manifest_hash,
                 provider_reference_id[:500] if provider_reference_id else None,
                 json.dumps(usage or {}, ensure_ascii=True, separators=(",", ":")), now,
                 operation_id),
            )
        return self.get_web_operation(operation_id)

    def web_evidence_for_run(self, run_id: str) -> dict[str, Any]:
        operations = self.list_web_operations(run_id)
        verified = sum(int(item.get("verified_source_count") or 0) for item in operations)
        statuses = {str(item.get("evidence_status") or "not_evaluated") for item in operations}
        if verified > 0:
            status = "traceable_sources"
        elif "invalid" in statuses:
            status = "invalid"
        elif "provider_reference" in statuses:
            status = "provider_reference"
        elif operations and all(item.get("status") == "completed" for item in operations):
            status = "missing"
        else:
            status = "not_evaluated"
        return {"status": status, "verified_source_count": verified, "operations": operations}

    def set_run_grounding_status(self, run_id: str, status: str) -> dict[str, Any]:
        if status not in {"not_required", "grounded", "ungrounded_fallback", "failed"}:
            raise CoreDataError("invalid_web_evidence", "Web grounding status is invalid.")
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE model_runs SET web_grounding_status = ? WHERE id = ?", (status, run_id),
            )
            if cursor.rowcount != 1:
                raise CoreDataError("run_not_found", "The model run does not exist.")
        return self.get_model_run(run_id)

    def list_web_operations(self, run_id: str) -> list[dict[str, Any]]:
        self.get_model_run(run_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id FROM web_operations WHERE run_id = ? ORDER BY sequence", (run_id,),
            ).fetchall()
        return [self.get_web_operation(row["id"]) for row in rows]

    def cancel_active_web_operations(self, run_id: str) -> list[dict[str, Any]]:
        self.get_model_run(run_id)
        now = _utc_now()
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT id FROM web_operations
                   WHERE run_id = ? AND status IN ('prepared','dispatched','searching','reading_sources')""",
                (run_id,),
            ).fetchall()
            connection.execute(
                """UPDATE web_operations SET status = 'cancelled',
                       error_code = 'cancelled_by_user', finished_at = ?
                   WHERE run_id = ? AND status IN ('prepared','dispatched','searching','reading_sources')""",
                (now, run_id),
            )
        return [self.get_web_operation(row["id"]) for row in rows]

    def fail_active_web_operations(
        self, run_id: str, error_code: str,
    ) -> list[dict[str, Any]]:
        now = _utc_now()
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT id FROM web_operations
                   WHERE run_id = ? AND status IN
                   ('prepared','dispatched','searching','reading_sources')""",
                (run_id,),
            ).fetchall()
            connection.execute(
                """UPDATE web_operations SET status = 'failed', error_code = ?, finished_at = ?
                   WHERE run_id = ? AND status IN
                   ('prepared','dispatched','searching','reading_sources')""",
                (error_code, now, run_id),
            )
        return [self.get_web_operation(row["id"]) for row in rows]

    def list_conversation_web_operations(self, conversation_id: str) -> list[dict[str, Any]]:
        self.get_conversation(conversation_id)
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT o.id FROM web_operations o
                   JOIN model_runs r ON r.id = o.run_id
                   WHERE r.conversation_id = ? ORDER BY r.created_at, o.sequence""",
                (conversation_id,),
            ).fetchall()
        return [self.get_web_operation(row["id"]) for row in rows]

    def preview_web_source(self, run_id: str, source_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT s.* FROM web_sources s JOIN web_operations o ON o.id = s.operation_id
                   WHERE s.id = ? AND o.run_id = ?""", (source_id, run_id),
            ).fetchone()
        if row is None:
            raise CoreDataError("web_source_not_found", "The web source does not belong to this run.")
        return dict(row)

    def get_project_model_scope(self, project_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            project = connection.execute(
                "SELECT model_scope_policy FROM projects WHERE id = ? AND deleted_at IS NULL",
                (project_id,),
            ).fetchone()
            if project is None:
                raise CoreDataError("project_not_found", "The project does not exist.")
            rows = connection.execute(
                """SELECT provider_profile_id, model_id FROM project_model_allowlist
                   WHERE project_id = ? ORDER BY provider_profile_id, model_id""",
                (project_id,),
            ).fetchall()
        return {"project_id": project_id, "policy": project["model_scope_policy"],
                "models": [dict(row) for row in rows]}

    def update_project_model_scope(
        self, project_id: str, policy: str, models: list[dict[str, str]],
    ) -> dict[str, Any]:
        if policy not in {"inherit_global", "restricted"}:
            raise CoreDataError("invalid_model_scope", "Project model scope is not supported.")
        normalized: set[tuple[str, str]] = set()
        for item in models:
            if not isinstance(item, dict):
                raise CoreDataError("invalid_model_scope", "Project models must be objects.")
            provider_id, model_id = item.get("provider_profile_id"), item.get("model_id")
            if not isinstance(provider_id, str) or not provider_id or not isinstance(model_id, str) or not model_id:
                raise CoreDataError("invalid_model_scope", "Project model identifiers are required.")
            normalized.add((provider_id, model_id))
        if policy == "restricted" and not normalized:
            raise CoreDataError("invalid_model_scope", "A restricted project needs at least one model.")
        now = _utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE projects SET model_scope_policy = ?, updated_at = ?
                   WHERE id = ? AND deleted_at IS NULL""", (policy, now, project_id),
            )
            if cursor.rowcount != 1:
                raise CoreDataError("project_not_found", "The project does not exist.")
            connection.execute("DELETE FROM project_model_allowlist WHERE project_id = ?", (project_id,))
            if policy == "restricted":
                connection.executemany(
                    """INSERT INTO project_model_allowlist(
                           project_id, provider_profile_id, model_id, created_at
                       ) VALUES (?, ?, ?, ?)""",
                    [(project_id, provider_id, model_id, now) for provider_id, model_id in sorted(normalized)],
                )
            connection.execute(
                """UPDATE conversations SET context_generation = context_generation + 1
                   WHERE project_id = ? AND kind = 'project_work' AND deleted_at IS NULL""",
                (project_id,),
            )
        return self.get_project_model_scope(project_id)

    @staticmethod
    def _global_instruction_record(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        record = dict(row)
        record["enabled"] = bool(record["enabled"])
        return record

    def get_global_instructions(self) -> dict[str, Any]:
        with self._connect() as connection:
            return self._get_global_instructions_with_connection(connection)

    def _get_global_instructions_with_connection(
        self, connection: sqlite3.Connection,
    ) -> dict[str, Any]:
        row = connection.execute(
            "SELECT id, content, enabled, revision, content_hash, default_version, updated_at "
            "FROM global_instructions WHERE id = 1"
        ).fetchone()
        if row is None:
            raise CoreDataError("global_instructions_unavailable", "Global instructions are unavailable.")
        return self._global_instruction_record(row)

    @staticmethod
    def _validate_global_instruction(content: str, enabled: bool) -> str:
        if not isinstance(content, str):
            raise CoreDataError("invalid_global_instruction", "Global instructions must be text.")
        if not isinstance(enabled, bool):
            raise CoreDataError("invalid_global_instruction", "Global instruction enabled must be boolean.")
        normalized = content.strip()
        if len(normalized) > MAX_GLOBAL_INSTRUCTION_CHARS:
            raise CoreDataError(
                "invalid_global_instruction",
                f"Global instructions must contain at most {MAX_GLOBAL_INSTRUCTION_CHARS} characters.",
            )
        try:
            normalized.encode("utf-8")
        except UnicodeEncodeError as error:
            raise CoreDataError(
                "invalid_text_encoding", "Global instructions contain an invalid Unicode character.",
            ) from error
        return normalized

    def update_global_instructions(self, content: str, *, enabled: bool) -> dict[str, Any]:
        normalized = self._validate_global_instruction(content, enabled)
        now = _utc_now()
        content_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT revision FROM global_instructions WHERE id = 1"
            ).fetchone()
            if row is None:
                raise CoreDataError("global_instructions_unavailable", "Global instructions are unavailable.")
            revision = int(row["revision"]) + 1
            connection.execute(
                """UPDATE global_instructions
                   SET content = ?, enabled = ?, revision = ?, content_hash = ?, updated_at = ?
                   WHERE id = 1""",
                (normalized, int(enabled), revision, content_hash, now),
            )
        return self.get_global_instructions()

    def reset_global_instructions(self) -> dict[str, Any]:
        now = _utc_now()
        content_hash = hashlib.sha256(DEFAULT_GLOBAL_INSTRUCTIONS.encode("utf-8")).hexdigest()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT revision FROM global_instructions WHERE id = 1"
            ).fetchone()
            if row is None:
                raise CoreDataError("global_instructions_unavailable", "Global instructions are unavailable.")
            revision = int(row["revision"]) + 1
            connection.execute(
                """UPDATE global_instructions
                   SET content = ?, enabled = 1, revision = ?, content_hash = ?,
                       default_version = ?, updated_at = ?
                   WHERE id = 1""",
                (DEFAULT_GLOBAL_INSTRUCTIONS, revision, content_hash,
                 DEFAULT_GLOBAL_INSTRUCTIONS_VERSION, now),
            )
        return self.get_global_instructions()

    def list_project_knowledge(self, project_id: str, include_archived: bool = False) -> list[dict[str, Any]]:
        with self._connect() as connection:
            return self._list_project_knowledge_with_connection(
                connection, project_id, include_archived,
            )

    @staticmethod
    def _list_project_knowledge_with_connection(
        connection: sqlite3.Connection, project_id: str, include_archived: bool = False,
    ) -> list[dict[str, Any]]:
            predicate = "" if include_archived else " AND k.status = 'published'"
            rows = connection.execute(
                f"""SELECT k.*, CASE WHEN (c.id IS NULL OR c.deleted_at IS NOT NULL)
                            AND k.source_conversation_id IS NOT NULL
                            THEN 1 ELSE 0 END AS source_deleted
                       FROM project_knowledge k
                       LEFT JOIN conversations c ON c.id = k.source_conversation_id
                       WHERE k.project_id = ?{predicate}
                       ORDER BY k.pinned DESC, k.updated_at DESC, k.id""", (project_id,)
            ).fetchall()
            return [dict(row) for row in rows]

    def propose_knowledge(
        self, conversation_id: str, knowledge_type: str, title: str, content: str,
        source_message_id: str | None = None,
    ) -> dict[str, Any]:
        conversation = self.get_conversation(conversation_id)
        if conversation["kind"] != "project_work":
            raise CoreDataError("knowledge_not_allowed", "Only project work can propose project knowledge.")
        if knowledge_type not in {"fact", "decision", "constraint", "summary", "artifact_reference"}:
            raise CoreDataError("invalid_knowledge_type", "Knowledge type is not supported.")
        proposal = {"id": _uuid(), "project_id": conversation["project_id"],
                    "source_conversation_id": conversation_id, "source_message_id": source_message_id,
                    "knowledge_type": knowledge_type,
                    "title": _name(title, "invalid_knowledge_title", "Knowledge title"),
                    "content": self._message_content(content), "status": "pending",
                    "created_at": _utc_now(), "resolved_at": None}
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO knowledge_proposals(id, project_id, source_conversation_id,
                       source_message_id, knowledge_type, title, content, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
                (proposal["id"], proposal["project_id"], conversation_id, source_message_id,
                 knowledge_type, proposal["title"], proposal["content"], proposal["created_at"]),
            )
        return proposal

    def publish_knowledge(
        self, proposal_id: str, title: str | None = None, content: str | None = None,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            proposal = connection.execute(
                "SELECT * FROM knowledge_proposals WHERE id = ? AND status = 'pending'", (proposal_id,)
            ).fetchone()
            if proposal is None:
                raise CoreDataError("knowledge_proposal_not_found", "The pending knowledge proposal does not exist.")
            final_title = _name(title or proposal["title"], "invalid_knowledge_title", "Knowledge title")
            final_content = self._message_content(content or proposal["content"])
            knowledge_id, now = _uuid(), _utc_now()
            connection.execute(
                """INSERT INTO project_knowledge(id, project_id, knowledge_type, title, content,
                       pinned, source_conversation_id, source_message_id, current_revision,
                       status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 0, ?, ?, 1, 'published', ?, ?)""",
                (knowledge_id, proposal["project_id"], proposal["knowledge_type"], final_title,
                 final_content, proposal["source_conversation_id"], proposal["source_message_id"], now, now),
            )
            connection.execute(
                """INSERT INTO project_knowledge_revisions(id, knowledge_id, revision, title, content, created_at)
                   VALUES (?, ?, 1, ?, ?, ?)""", (_uuid(), knowledge_id, final_title, final_content, now)
            )
            connection.execute(
                "UPDATE knowledge_proposals SET status = 'published', resolved_at = ? WHERE id = ?",
                (now, proposal_id),
            )
            connection.execute(
                """UPDATE conversations SET context_generation = context_generation + 1
                   WHERE project_id = ? AND kind = 'project_work' AND deleted_at IS NULL""",
                (proposal["project_id"],),
            )
        return next(item for item in self.list_project_knowledge(proposal["project_id"], True)
                    if item["id"] == knowledge_id)

    def reject_knowledge_proposal(self, proposal_id: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE knowledge_proposals SET status = 'rejected', resolved_at = ?
                   WHERE id = ? AND status = 'pending'""", (_utc_now(), proposal_id)
            )
            if cursor.rowcount != 1:
                raise CoreDataError("knowledge_proposal_not_found", "The pending knowledge proposal does not exist.")

    def update_knowledge(
        self, knowledge_id: str, *, title: str | None = None, content: str | None = None,
        pinned: bool | None = None, archive: bool | None = None,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            current = connection.execute("SELECT * FROM project_knowledge WHERE id = ?", (knowledge_id,)).fetchone()
            if current is None:
                raise CoreDataError("knowledge_not_found", "The project knowledge does not exist.")
            final_title = _name(title if title is not None else current["title"],
                                "invalid_knowledge_title", "Knowledge title")
            final_content = self._message_content(content if content is not None else current["content"])
            revision = current["current_revision"] + (1 if title is not None or content is not None else 0)
            status = "archived" if archive is True else "published" if archive is False else current["status"]
            final_pinned = int(pinned) if pinned is not None else current["pinned"]
            now = _utc_now()
            connection.execute(
                """UPDATE project_knowledge SET title = ?, content = ?, pinned = ?,
                       current_revision = ?, status = ?, updated_at = ? WHERE id = ?""",
                (final_title, final_content, final_pinned, revision, status, now, knowledge_id),
            )
            if revision != current["current_revision"]:
                connection.execute(
                    """INSERT INTO project_knowledge_revisions(id, knowledge_id, revision, title, content, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (_uuid(), knowledge_id, revision, final_title, final_content, now),
                )
            connection.execute(
                """UPDATE conversations SET context_generation = context_generation + 1
                   WHERE project_id = ? AND kind = 'project_work' AND deleted_at IS NULL""",
                (current["project_id"],),
            )
        return next(item for item in self.list_project_knowledge(current["project_id"], True)
                    if item["id"] == knowledge_id)

    @staticmethod
    def _message_content(content: str) -> str:
        normalized = content.strip()
        if not 1 <= len(normalized) <= 20_000:
            raise CoreDataError("invalid_message", "Message must contain 1 to 20,000 characters.")
        try:
            normalized.encode("utf-8")
        except UnicodeEncodeError as error:
            raise CoreDataError("invalid_text_encoding", "Text contains an invalid Unicode character.") from error
        return normalized

    def _insert_message(
        self, conversation_id: str, role: str, content: str, event_type: str | None = None,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            return self._insert_message_with_connection(connection, conversation_id, role, content, event_type)

    def record_assistant_progress(
        self, run_id: str, attempt_id: str, round_no: int, content: str,
    ) -> dict[str, Any]:
        """Keep public model narration separate from the delivered assistant answer."""
        with self._connect() as connection:
            row = connection.execute(
                """SELECT r.conversation_id FROM model_runs r JOIN run_attempts a
                   ON a.run_id = r.id WHERE r.id = ? AND a.id = ?""", (run_id, attempt_id),
            ).fetchone()
            if row is None:
                raise CoreDataError("run_not_found", "The run attempt does not exist.")
            payload = json.dumps({
                "version": 1, "run_id": run_id, "attempt_id": attempt_id,
                "round_no": round_no, "content": content,
            }, ensure_ascii=False, separators=(",", ":"))
            return self._insert_message_with_connection(
                connection, row["conversation_id"], "system_event", payload, "assistant_progress",
            )

    def list_assistant_progress(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT m.* FROM messages m WHERE m.conversation_id =
                   (SELECT conversation_id FROM model_runs WHERE id = ?)
                   AND m.role = 'system_event' AND m.event_type = 'assistant_progress'
                   AND json_extract(CASE WHEN json_valid(m.content) THEN m.content ELSE '{}' END,
                                    '$.run_id') = ? ORDER BY m.sequence_no, m.id""", (run_id, run_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_tool_event(
        self,
        run_id: str,
        operation_id: str,
        tool_name: str,
        status: str,
        output_bytes: int,
        error_code: str | None,
        *,
        search_summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from agent.tool_contracts import search_completeness

        scan = search_completeness(search_summary) if tool_name == "fs.search" else None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT conversation_id FROM model_runs WHERE id = ?", (run_id,),
            ).fetchone()
            if row is None:
                raise CoreDataError("run_not_found", "The model run does not exist.")
            content = json.dumps({
                "run_id": run_id,
                "operation_id": operation_id,
                "tool_name": tool_name,
                "status": status,
                "output_bytes": output_bytes,
                "error_code": error_code,
                **({"search_completeness": scan} if scan is not None else {}),
            }, ensure_ascii=True, separators=(",", ":"))
            return self._insert_message_with_connection(
                connection,
                row["conversation_id"],
                "system_event",
                content,
                "tool_operation",
            )

    def create_pending_follow_up_action(
        self, *, conversation_id: str, source_run_id: str, source_message_id: str,
        action_type: str, query_hint: str, purpose: str,
        evidence: str, ttl_seconds: int = 600,
    ) -> dict[str, Any]:
        if action_type != "web_search":
            raise CoreDataError("invalid_follow_up_action", "The follow-up action is not supported.")
        if evidence not in {"optional", "required"}:
            raise CoreDataError("invalid_follow_up_action", "The follow-up evidence level is not supported.")
        if not query_hint.strip() or len(query_hint) > 8_000:
            raise CoreDataError("invalid_follow_up_action", "The follow-up query is invalid.")
        if not purpose.strip() or len(purpose) > 500:
            raise CoreDataError("invalid_follow_up_action", "The follow-up purpose is invalid.")
        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat(
            timespec="milliseconds",
        ).replace("+00:00", "Z")
        action_id = _uuid()
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT id FROM pending_follow_up_actions "
                "WHERE conversation_id = ? AND status = 'pending'",
                (conversation_id,),
            ).fetchall()
            for row in existing:
                connection.execute(
                    "UPDATE pending_follow_up_actions SET status = 'rejected', resolved_at = ? WHERE id = ?",
                    (_utc_now(), row["id"]),
                )
            payload = {"query_hint": query_hint.strip(), "purpose": purpose.strip(), "evidence": evidence}
            connection.execute(
                """INSERT INTO pending_follow_up_actions(
                       id, conversation_id, source_run_id, source_message_id,
                       action_type, payload_json, status, expires_at, created_at, resolved_at
                   ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, NULL)""",
                (action_id, conversation_id, source_run_id, source_message_id,
                 action_type, json.dumps(payload, ensure_ascii=False, sort_keys=True),
                 expires_at, _utc_now()),
            )
        return self._pending_follow_up_action(action_id)

    def get_pending_follow_up_action(self, conversation_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM pending_follow_up_actions "
                "WHERE conversation_id = ? AND status = 'pending' ORDER BY created_at DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
            if row is None:
                return None
            if self._is_expired(row["expires_at"]):
                connection.execute(
                    "UPDATE pending_follow_up_actions SET status = 'expired', resolved_at = ? WHERE id = ?",
                    (_utc_now(), row["id"]),
                )
                return None
            return self._pending_row_to_dict(row)

    def consume_pending_follow_up_action(
        self, conversation_id: str, action_id: str,
    ) -> dict[str, Any]:
        return self._resolve_pending_follow_up_action(conversation_id, action_id, "consumed")

    def reject_pending_follow_up_action(
        self, conversation_id: str, action_id: str,
    ) -> dict[str, Any]:
        return self._resolve_pending_follow_up_action(conversation_id, action_id, "rejected")

    def _resolve_pending_follow_up_action(
        self, conversation_id: str, action_id: str, status: str,
    ) -> dict[str, Any]:
        if status not in {"consumed", "rejected"}:
            raise CoreDataError("invalid_follow_up_action", "The follow-up status is invalid.")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM pending_follow_up_actions WHERE id = ? AND conversation_id = ?",
                (action_id, conversation_id),
            ).fetchone()
            if row is None or row["status"] != "pending":
                raise CoreDataError("pending_follow_up_action_stale", "The follow-up action is no longer pending.")
            if self._is_expired(row["expires_at"]):
                connection.execute(
                    "UPDATE pending_follow_up_actions SET status = 'expired', resolved_at = ? WHERE id = ?",
                    (_utc_now(), action_id),
                )
                raise CoreDataError("pending_follow_up_action_stale", "The follow-up action has expired.")
            connection.execute(
                "UPDATE pending_follow_up_actions SET status = ?, resolved_at = ? WHERE id = ?",
                (status, _utc_now(), action_id),
            )
            updated = connection.execute(
                "SELECT * FROM pending_follow_up_actions WHERE id = ?", (action_id,)
            ).fetchone()
            return self._pending_row_to_dict(updated)

    @staticmethod
    def _is_expired(value: str) -> bool:
        return datetime.now(timezone.utc) >= datetime.fromisoformat(value.replace("Z", "+00:00"))

    @staticmethod
    def _pending_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        payload = json.loads(row["payload_json"] or "{}")
        return {
            "id": row["id"], "conversation_id": row["conversation_id"],
            "source_run_id": row["source_run_id"], "source_message_id": row["source_message_id"],
            "action_type": row["action_type"], "status": row["status"],
            "expires_at": row["expires_at"], "created_at": row["created_at"],
            "resolved_at": row["resolved_at"], **payload,
        }

    def _pending_follow_up_action(self, action_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM pending_follow_up_actions WHERE id = ?", (action_id,)
            ).fetchone()
            if row is None:
                raise CoreDataError("pending_follow_up_action_not_found", "The follow-up action does not exist.")
            return self._pending_row_to_dict(row)

    def record_execution_summary(self, run_id: str, summary: dict[str, Any]) -> dict[str, Any]:
        """Persist a compact routing audit without prompts or source bodies."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT conversation_id FROM model_runs WHERE id = ?", (run_id,),
            ).fetchone()
            if row is None:
                raise CoreDataError("run_not_found", "The model run does not exist.")
            content = json.dumps(
                {"run_id": run_id, **summary}, ensure_ascii=False,
                sort_keys=True, separators=(",", ":"),
            )
            return self._insert_message_with_connection(
                connection, row["conversation_id"], "system_event", content,
                "execution_summary",
            )

    @staticmethod
    def _insert_message_with_connection(
        connection: sqlite3.Connection, conversation_id: str, role: str,
        content: str, event_type: str | None = None,
    ) -> dict[str, Any]:
        sequence_no = connection.execute(
            "SELECT COALESCE(MAX(sequence_no), 0) + 1 FROM messages WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()[0]
        message = {"id": _uuid(), "conversation_id": conversation_id, "role": role,
                   "content": content, "event_type": event_type, "source_message_id": None,
                   "created_at": _utc_now(), "sequence_no": sequence_no}
        connection.execute(
            """INSERT INTO messages(id, conversation_id, role, content, event_type,
                   source_message_id, created_at, sequence_no) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (message["id"], conversation_id, role, content, event_type, None,
             message["created_at"], sequence_no),
        )
        connection.execute(
            """UPDATE conversations SET updated_at = ?, context_generation = context_generation + 1
               WHERE id = ?""", (message["created_at"], conversation_id)
        )
        return message

    @staticmethod
    def _conversation_summary(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        project_category = result.pop("project_category", None)
        result["resolved_category"] = (
            result.get("category") or result.get("purpose_override") or project_category
        )
        return result
