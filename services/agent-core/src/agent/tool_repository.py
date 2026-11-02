from __future__ import annotations

import json
import sqlite3
from hashlib import sha256
from pathlib import Path
from typing import Any

from agent.storage import CoreDataError, _open_database, _utc_now, _uuid
from agent.tool_contracts import ToolCall, canonical_arguments, search_completeness, validate_tool_call


ACTIVE_TOOL_STATES = {
    "requested", "validating", "reviewing", "waiting_user", "executing",
}
TERMINAL_TOOL_STATES = {"completed", "rejected", "failed", "cancelled", "interrupted"}


class ToolRepository:
    """Persists tool metadata and bounded terminal audit output, excluding file bodies/diffs."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)

    def _connect(self) -> sqlite3.Connection:
        return _open_database(self.database_path)

    def create(
        self, run_id: str, attempt_id: str, sequence: int, call: ToolCall,
    ) -> dict[str, Any]:
        validate_tool_call(call)
        arguments_json, arguments_hash = canonical_arguments(call.arguments)
        operation_id, now = _uuid(), _utc_now()
        try:
            with self._connect() as connection:
                connection.execute(
                    """INSERT INTO tool_operations(
                           id, run_id, attempt_id, sequence, provider_call_id, tool_name,
                           arguments_json, arguments_hash, status, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'requested', ?)""",
                    (operation_id, run_id, attempt_id, sequence, call.provider_call_id,
                     call.name, arguments_json, arguments_hash, now),
                )
        except sqlite3.IntegrityError as error:
            existing = self.find_by_provider_call(run_id, attempt_id, call.provider_call_id)
            if existing is not None and existing["arguments_hash"] == arguments_hash:
                return existing
            raise CoreDataError("duplicate_tool_call", "Tool call id was reused with different data.") from error
        return self.get(operation_id)

    def get(self, operation_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM tool_operations WHERE id = ?", (operation_id,),
            ).fetchone()
        if row is None:
            raise CoreDataError("tool_operation_not_found", "Tool operation does not exist.")
        return self._decode(row)

    def find_by_provider_call(
        self, run_id: str, attempt_id: str, provider_call_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM tool_operations
                   WHERE run_id = ? AND attempt_id = ? AND provider_call_id = ?""",
                (run_id, attempt_id, provider_call_id),
            ).fetchone()
        return self._decode(row) if row is not None else None

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tool_operations WHERE run_id = ? ORDER BY sequence, rowid",
                (run_id,),
            ).fetchall()
        return [self._decode(row) for row in rows]

    def list_for_conversation(self, conversation_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT o.* FROM tool_operations o JOIN model_runs r ON r.id=o.run_id
                   WHERE r.conversation_id=? ORDER BY o.created_at,o.sequence""", (conversation_id,),
            ).fetchall()
        return [self._decode(row) for row in rows]

    def transition(
        self, operation_id: str, expected: set[str], status: str, *,
        result_summary: str | None = None, result_hash: str | None = None,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        allowed = ACTIVE_TOOL_STATES | TERMINAL_TOOL_STATES
        if status not in allowed or not expected:
            raise CoreDataError("invalid_tool_transition", "Tool status transition is invalid.")
        placeholders = ",".join("?" for _ in expected)
        finished_at = _utc_now() if status in TERMINAL_TOOL_STATES else None
        operation = self.get(operation_id)
        limit = 8 * 1024 * 1024 if operation["tool_name"] == "terminal.run" else 1000
        if operation["tool_name"] == "terminal.run" and result_summary is not None and len(result_summary) > limit:
            raise CoreDataError("tool_limit_exceeded", "Terminal audit exceeds its limit.")
        safe_summary = result_summary[:limit] if result_summary is not None else None
        stored_hash = result_hash or (
            sha256(result_summary.encode("utf-8")).hexdigest()
            if result_summary is not None else None
        )
        with self._connect() as connection:
            cursor = connection.execute(
                f"""UPDATE tool_operations SET status = ?, result_summary = ?, result_hash = ?,
                           error_code = ?, finished_at = ?
                       WHERE id = ? AND status IN ({placeholders})""",
                (status, safe_summary, stored_hash, error_code, finished_at,
                 operation_id, *sorted(expected)),
            )
            if cursor.rowcount != 1:
                raise CoreDataError("invalid_tool_transition", "Tool operation cannot enter that state.")
        return self.get(operation_id)

    def interrupt_active(self) -> list[str]:
        now = _utc_now()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id FROM tool_operations WHERE status IN ('requested','validating','reviewing','waiting_user','executing')"
            ).fetchall()
            connection.execute(
                """UPDATE tool_operations SET status = 'interrupted', error_code = 'tool_interrupted',
                       finished_at = ? WHERE status IN
                       ('requested','validating','reviewing','waiting_user','executing')""",
                (now,),
            )
        return [row["id"] for row in rows]

    def record_review(
        self, operation_id: str, provider_profile_id: str, model_id: str,
        decision: str, reason_code: str, duration_ms: int,
        usage: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if decision not in {"approve", "reject", "uncertain"}:
            raise CoreDataError("invalid_review_decision", "Tool review decision is invalid.")
        review_id = _uuid()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO tool_reviews(
                       id, operation_id, provider_profile_id, model_id, decision,
                       reason_code, input_tokens, output_tokens, duration_ms, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (review_id, operation_id, provider_profile_id, model_id, decision,
                 reason_code[:100], (usage or {}).get("input_tokens"),
                 (usage or {}).get("output_tokens"), duration_ms, _utc_now()),
            )
            row = connection.execute(
                "SELECT * FROM tool_reviews WHERE id = ?", (review_id,),
            ).fetchone()
        return dict(row)

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        try:
            result["arguments"] = json.loads(result.pop("arguments_json"))
        except (TypeError, json.JSONDecodeError) as error:
            raise CoreDataError("invalid_persisted_data", "Stored tool arguments are invalid.") from error
        if result["tool_name"] == "fs.search":
            try:
                summary = json.loads(result.get("result_summary") or "null")
            except (TypeError, ValueError):
                summary = None
            result["search_completeness"] = search_completeness(
                summary.get("search_completeness") if isinstance(summary, dict) else None,
            )
        return result
