from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path
from dataclasses import asdict
from typing import Any

from agent.context_engine.domain import CompactionClaim, CompactionTarget, PackedContext
from agent.context_engine.policy import DEFAULT_POLICY
from agent.context_engine.retrieval import query_units
from agent.context_engine.attachment_references import validate_saved_selection
from agent.storage import CoreDataError, CoreRepository, _open_database, _utc_now


class ContextRepository:
    """SQLite adapter for context-only state and immutable audit records."""

    def __init__(self, core: CoreRepository) -> None:
        self.core = core
        self.database_path = Path(core.database_path)

    def _connect(self) -> sqlite3.Connection:
        return _open_database(self.database_path)

    def load_context_sources(
        self, run_id: str, *, purpose: str = "assemble",
        before_sequence: int | None = None, limit: int | None = None,
    ) -> dict[str, Any]:
        if purpose not in {"assemble", "compact", "expanded_retry"}:
            raise ValueError("Unsupported context source purpose")
        if purpose != "compact" and (limit is not None or before_sequence is not None):
            raise ValueError("Assembly must retain all uncompacted history")
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
            raise ValueError("The compaction row limit must be positive")
        started = time.perf_counter()
        # All context-bearing rows are read from one SQLite snapshot.  The Core
        # helpers accept this connection so messages, knowledge and generation
        # cannot come from different commits.
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            run_row = connection.execute("SELECT * FROM model_runs WHERE id = ?", (run_id,)).fetchone()
            if run_row is None:
                raise CoreDataError("run_not_found", "The model run does not exist.")
            run = self.core._model_run_from_row(connection, run_row)
            conversation = self.core._get_conversation_header_with_connection(
                connection, run["conversation_id"],
            )
            generation = int(conversation.get("context_generation") or 0)
            current = connection.execute(
                "SELECT * FROM messages WHERE id = ? AND conversation_id = ?",
                (run["user_message_id"], conversation["id"]),
            ).fetchone()
            if current is None:
                raise CoreDataError("invalid_persisted_data", "The run user message is unavailable.")
            checkpoint = connection.execute(
                """SELECT * FROM context_checkpoints
                   WHERE conversation_id = ? AND status = 'valid' AND through_sequence < ?
                   ORDER BY through_sequence DESC, COALESCE(through_offset, 2147483647) DESC,
                            created_at DESC LIMIT 1""",
                (conversation["id"], current["sequence_no"]),
            ).fetchone()
            frontier = int(checkpoint["through_sequence"]) if checkpoint else 0
            # Include the coverage boundary only for a partial message. SQLite's
            # existing conversation/sequence index serves this range directly.
            lower = ">=" if checkpoint and checkpoint["through_offset"] is not None else ">"
            upper = min(current["sequence_no"], before_sequence) if before_sequence is not None else current["sequence_no"]
            tail_sql = f"SELECT * FROM messages WHERE conversation_id = ? AND sequence_no {lower} ? AND sequence_no < ?"
            parameters = [conversation["id"], frontier, upper]
            if purpose == "compact":
                tail_sql += " AND role IN ('user','assistant')"
            tail_sql += " ORDER BY sequence_no, id"
            if purpose == "compact":
                tail_sql += " LIMIT ?"
                parameters.append(limit or 256)
            messages = [dict(row) for row in connection.execute(tail_sql, parameters)]
            current = dict(current)
            current["attachments"] = [dict(row) for row in connection.execute(
                "SELECT * FROM message_attachments WHERE message_id = ? ORDER BY created_at, rowid",
                (current["id"],),
            )]
            for attachment in current["attachments"]:
                attachment["artifacts"] = [dict(row) for row in connection.execute(
                    "SELECT *, id AS artifact_id FROM attachment_artifacts WHERE attachment_id = ? ORDER BY created_at, rowid",
                    (attachment["id"],),
                )]
                self.core._add_legacy_model_artifact_view(attachment)
            current["attachment_rejections"] = [dict(row) for row in connection.execute(
                "SELECT * FROM message_attachment_rejections WHERE message_id = ? ORDER BY created_at, rowid",
                (current["id"],),
            )]
            conversation["messages"] = messages + [current]
            manifest = [dict(row) for row in connection.execute(
                """SELECT a.id AS attachment_id, a.message_id, substr(a.name,1,255) AS name,
                          a.kind, a.sha256, 'not_checked' AS availability
                   FROM messages m JOIN message_attachments a ON a.message_id = m.id
                   WHERE m.conversation_id = ? AND m.sequence_no < ?
                   ORDER BY m.sequence_no DESC, a.created_at DESC, a.rowid DESC LIMIT 32""",
                (conversation["id"], current["sequence_no"]),
            )][::-1]
            replay, replay_rows = [], 0
            if purpose == "expanded_retry" and checkpoint:
                replay, replay_rows = self._load_expanded_replay(connection, conversation["id"], checkpoint, current)
            # Filter runs in SQL rather than loading every historical run. Both
            # endpoints matter when a checkpoint cuts through a paired turn.
            identifiers = {message["id"] for message in messages + replay + [current]}
            runs = [dict(row) for row in connection.execute(
                """SELECT r.* FROM model_runs r WHERE r.conversation_id = ? AND
                   (r.user_message_id IN (SELECT value FROM json_each(?)) OR
                    r.user_message_id IN (SELECT related.user_message_id FROM model_runs related
                        WHERE related.conversation_id = ? AND related.assistant_message_id IN
                            (SELECT value FROM json_each(?))))
                   ORDER BY r.created_at, r.rowid""",
                (conversation["id"], json.dumps(list(identifiers)), conversation["id"], json.dumps(list(identifiers))),
            )]
            operations = [dict(row) for row in connection.execute(
                """SELECT o.id, o.tool_name, o.status, o.result_summary, o.error_code, o.finished_at
                   FROM tool_operations o JOIN model_runs r ON r.id = o.run_id
                   WHERE r.conversation_id = ? ORDER BY o.created_at DESC LIMIT 20""",
                (conversation["id"],),
            ).fetchall()]
            knowledge = self.core._list_project_knowledge_with_connection(
                connection, conversation["project_id"], False,
            ) if conversation["kind"] == "project_work" else []
            global_instructions = self.core._get_global_instructions_with_connection(connection)
            # Linked retries reuse the exact same user-message selection, even
            # when the new run has no execution plan of its own yet.
            plan = connection.execute(
                """SELECT p.resolved_intent_json, p.requirements_json, p.resolution_hash,
                          p.requirements_hash FROM execution_plans p
                   JOIN model_runs r ON r.id = p.run_id
                   WHERE r.user_message_id = ? AND r.conversation_id = ?
                   ORDER BY (p.run_id = ?) DESC, p.created_at DESC LIMIT 1""",
                (run["user_message_id"], run["conversation_id"], run_id),
            ).fetchone()
            selection = validate_saved_selection(plan, json.loads(plan[0])) if plan else []
            advisories = json.loads(plan[1]).get("capability_advisories", []) if plan else []
            vision_specialist = connection.execute("SELECT 1 FROM execution_steps s JOIN execution_plans p ON p.id = s.plan_id WHERE p.run_id = ? AND s.step_type = 'vision_analysis'", (run_id,)).fetchone() is not None
            selections = {item["current_attachment_id"]: item for item in selection}
            selected_source_rows = 0
            for message in conversation["messages"]:
                if message["id"] == run["user_message_id"]:
                    for attachment in message.get("attachments", []):
                        selection = selections.get(attachment["id"], {})
                        if not selection and attachment.get("source_attachment_id"):
                            raise CoreDataError(
                                "attachment_reference_selection_unavailable",
                                "The saved attachment selection is unavailable. Select the attachment again.",
                            )
                        if selection:
                            source = connection.execute(
                                """SELECT a.sha256, a.storage_key, a.message_id FROM message_attachments a
                                   JOIN messages m ON m.id = a.message_id
                                   WHERE a.id = ? AND m.conversation_id = ?""",
                                (selection["source_attachment_id"], run["conversation_id"]),
                            ).fetchone()
                            if source is None:
                                raise CoreDataError("attachment_reference_forbidden", "The selected attachment is no longer in this conversation.")
                            selected_source_rows += 1
                            if source["sha256"] != attachment["sha256"] or source["storage_key"] != attachment["storage_key"]:
                                raise CoreDataError("attachment_changed", "The selected source no longer matches its snapshot.")
                            if source["message_id"] != selection["source_message_id"]:
                                raise CoreDataError("attachment_reference_selection_changed", "The selected source message has changed.")
                        attachment.update(selection)
        return {"run": run, "conversation": conversation, "runs": runs,
                "reference_manifest": manifest, "replay_messages": replay,
                "checkpoint": dict(checkpoint) if checkpoint else None,
                "knowledge": knowledge, "operations": operations,
                "capability_advisories": advisories, "has_vision_specialist": vision_specialist,
                 "context_generation": generation,
                 "global_instructions": global_instructions,
                 "performance": {
                     # Materialized source/metadata rows; SQL index/page visits
                     # and scalar subquery work are deliberately not this count.
                     "rows_read": len(messages) + 4 + len(runs) + len(manifest) + replay_rows
                         + len(operations) + len(knowledge) + int(checkpoint is not None)
                         + int(plan is not None) + int(vision_specialist) + selected_source_rows
                         + sum(run.get(key) is not None for key in ("attempt", "draft", "assistant_message"))
                         + len(current["attachments"]) + len(current["attachment_rejections"])
                         + sum(len(attachment["artifacts"]) for attachment in current["attachments"]),
                     "messages_processed": len(messages) + 1 + replay_rows,
                     "elapsed_ms": (time.perf_counter() - started) * 1000,
                 }}

    @staticmethod
    def _load_expanded_replay(connection, conversation_id, checkpoint, current):
        # Expanded retries deliberately search covered text. Stream it in this
        # same snapshot, retaining only the two frontier rows and four winners.
        query = query_units(current["content"])
        recent, scored, count = [], [], 0
        for row in connection.execute(
            """SELECT * FROM messages WHERE conversation_id = ? AND sequence_no <= ?
               AND role IN ('user','assistant') ORDER BY sequence_no, id""",
            (conversation_id, checkpoint["through_sequence"]),
        ):
            count += 1
            message = dict(row)
            recent = (recent + [message])[-2:]
            score = len(query & query_units(message["content"]))
            if score:
                scored.append((score, message["sequence_no"], message["id"], message))
                scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
                del scored[4:]
        selected = {message["id"]: message for message in recent}
        selected.update((item[2], item[3]) for item in scored)
        return sorted(selected.values(), key=lambda item: (item["sequence_no"], item["id"])), count

    def active_checkpoint(self, conversation_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM context_checkpoints
                   WHERE conversation_id = ? AND status = 'valid'
                   ORDER BY through_sequence DESC, COALESCE(through_offset, 2147483647) DESC,
                            created_at DESC LIMIT 1""",
                (conversation_id,),
            ).fetchone()
        return dict(row) if row else None

    def context_generation(self, conversation_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT context_generation FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
        if row is None:
            raise CoreDataError("conversation_not_found", "The conversation does not exist.")
        return int(row["context_generation"] or 0)

    def global_instruction_revision(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT revision FROM global_instructions WHERE id = 1"
            ).fetchone()
        if row is None:
            raise CoreDataError("global_instructions_unavailable", "Global instructions are unavailable.")
        return int(row["revision"])

    def compaction_status(self, conversation_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM context_compaction_attempts WHERE conversation_id = ?
                   ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                (conversation_id,),
            ).fetchone()
        return dict(row) if row else None

    def next_snapshot_round(self, run_id: str) -> int:
        """Append new attempts without overwriting the run's immutable context evidence."""
        with self._connect() as connection:
            return int(connection.execute(
                "SELECT COALESCE(MAX(round_no), -1) + 1 FROM context_snapshots WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0])

    def save_snapshot(self, run_id: str, round_no: int, packed: PackedContext, policy_json: str) -> str:
        snapshot_id, now = str(uuid.uuid4()), _utc_now()
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM context_snapshots WHERE run_id = ? AND round_no = ?",
                (run_id, round_no),
            ).fetchone()
            if existing is not None:
                same = (
                    existing["policy_json"] == policy_json
                    and int(existing["context_generation"]) == packed.context_generation
                    and existing["context_window"] == packed.context_window
                    and existing["input_target"] == packed.input_target
                    and existing["output_reserve"] == packed.output_reserve
                    and existing["estimated_tokens"] == packed.estimated_tokens
                    and existing["stable_prefix_hash"] == packed.stable_prefix_hash
                    and existing["dynamic_suffix_hash"] == packed.dynamic_suffix_hash
                    and existing["content_hash"] == packed.content_hash
                    and existing["retrieval_confidence"] == packed.retrieval_confidence.value
                    and int(existing["global_instruction_revision"] or 0)
                    == packed.global_instruction_revision
                    and existing["global_instruction_hash"] == packed.global_instruction_hash
                    and int(existing["compression_applied"]) == int(packed.compression_applied)
                )
                if not same:
                    raise CoreDataError(
                        "context_snapshot_conflict",
                        "The existing context snapshot differs from this retry.",
                    )
                connection.execute(
                    "UPDATE model_runs SET context_snapshot_id = ?, context_generation = ? WHERE id = ?",
                    (existing["id"], packed.context_generation, run_id),
                )
                return str(existing["id"])
            connection.execute(
                """INSERT INTO context_snapshots(
                       id, run_id, round_no, assembler_version, policy_version, policy_json,
                       context_generation, global_instruction_revision, global_instruction_hash,
                       context_window, input_target, output_reserve,
                       token_method, estimated_tokens, stable_prefix_hash, dynamic_suffix_hash,
                       content_hash, retrieval_confidence, compression_applied, omitted_items, created_at
                   ) VALUES (?, ?, ?, 'context-engine-v2', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (snapshot_id, run_id, round_no, DEFAULT_POLICY.version, policy_json,
                 packed.context_generation, packed.global_instruction_revision,
                 packed.global_instruction_hash, packed.context_window, packed.input_target,
                 packed.output_reserve, "conservative-char-v2", packed.estimated_tokens,
                 packed.stable_prefix_hash, packed.dynamic_suffix_hash, packed.content_hash,
                 packed.retrieval_confidence.value, int(packed.compression_applied),
                 sum(1 for item in packed.items if not item.included), now),
            )
            for position, item in enumerate(packed.items):
                record = item.to_record()
                connection.execute(
                    """INSERT INTO context_items(
                           id, snapshot_id, source_type, source_id, position, estimated_tokens,
                           included, exclusion_reason, section, representation, trust,
                           derived_from_untrusted, source_start, source_end, source_hash,
                           source_version, inclusion_reason, metadata_json
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (str(uuid.uuid4()), snapshot_id, record["source_type"], record["source_id"],
                     position, record["estimated_tokens"], int(record["included"]),
                     record["exclusion_reason"], record["section"], record["representation"],
                     record["trust"], int(record["derived_from_untrusted"]),
                     record["source_start"], record["source_end"], record["source_hash"],
                     record["source_version"], record["inclusion_reason"],
                     json.dumps(record["metadata"], ensure_ascii=True, separators=(",", ":"))),
                )
            connection.execute(
                "UPDATE model_runs SET context_snapshot_id = ?, context_generation = ? WHERE id = ?",
                (snapshot_id, packed.context_generation, run_id),
            )
        return snapshot_id

    def save_tool_round_snapshot(
        self, run_id: str, round_no: int, prompt_messages: list[dict[str, Any]],
    ) -> str:
        """Record a later provider call without duplicating source bodies.

        Tool-loop prompts reuse the immutable round-zero context and append normalized
        assistant/tool messages.  The audit row therefore copies source references from
        the previous round and records only hashes/counts for the appended prompt state.
        """
        if round_no < 1:
            raise ValueError("tool round snapshots require round_no >= 1")
        canonical = json.dumps(
            prompt_messages, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
        )
        dynamic_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        estimated_tokens = max(1, (len(canonical) + 2) // 3)
        snapshot_id, now = str(uuid.uuid4()), _utc_now()
        with self._connect() as connection:
            previous = connection.execute(
                "SELECT * FROM context_snapshots WHERE run_id = ? ORDER BY round_no DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            if previous is None:
                raise CoreDataError("context_source_unavailable", "The base context snapshot is unavailable.")
            connection.execute(
                """INSERT INTO context_snapshots(
                       id, run_id, round_no, assembler_version, policy_version, policy_json,
                       context_generation, global_instruction_revision, global_instruction_hash,
                       context_window, input_target, output_reserve,
                       token_method, estimated_tokens, stable_prefix_hash, dynamic_suffix_hash,
                       content_hash, retrieval_confidence, compression_applied, omitted_items, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (snapshot_id, run_id, round_no, previous["assembler_version"],
                 previous["policy_version"], previous["policy_json"],
                 previous["context_generation"], previous["global_instruction_revision"],
                 previous["global_instruction_hash"], previous["context_window"],
                 previous["input_target"], previous["output_reserve"],
                 previous["token_method"], estimated_tokens, previous["stable_prefix_hash"],
                 dynamic_hash, dynamic_hash, previous["retrieval_confidence"],
                 previous["compression_applied"], previous["omitted_items"], now),
            )
            connection.execute(
                """INSERT INTO context_items(
                       id, snapshot_id, source_type, source_id, position, estimated_tokens,
                       included, exclusion_reason, section, representation, trust,
                       derived_from_untrusted, source_start, source_end, source_hash,
                       source_version, inclusion_reason, metadata_json
                   ) SELECT lower(hex(randomblob(16))), ?, source_type, source_id, position,
                            estimated_tokens, included, exclusion_reason, section,
                            representation, trust, derived_from_untrusted, source_start,
                            source_end, source_hash, source_version, inclusion_reason, metadata_json
                     FROM context_items WHERE snapshot_id = ? ORDER BY position""",
                (snapshot_id, previous["id"]),
            )
            connection.execute(
                """INSERT INTO context_items(
                       id, snapshot_id, source_type, source_id, position, estimated_tokens,
                       included, exclusion_reason, section, representation, trust,
                       derived_from_untrusted, source_hash, inclusion_reason, metadata_json
                   ) VALUES (?, ?, 'tool_round', ?,
                       (SELECT COALESCE(MAX(position), -1) + 1 FROM context_items WHERE snapshot_id = ?),
                       ?, 1, NULL, 'task_state', 'metadata', 'system_observation', 0, ?,
                       'tool_round_append', ?)""",
                (str(uuid.uuid4()), snapshot_id, run_id, snapshot_id, estimated_tokens,
                 dynamic_hash, json.dumps({"round_no": round_no, "message_count": len(prompt_messages)},
                                          ensure_ascii=True, separators=(",", ":"))),
            )
            connection.execute(
                "UPDATE model_runs SET context_snapshot_id = ? WHERE id = ?",
                (snapshot_id, run_id),
            )
        return snapshot_id

    def preview(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            snapshot = connection.execute(
                "SELECT * FROM context_snapshots WHERE run_id = ? ORDER BY round_no DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            if snapshot is None:
                return None
            items = connection.execute(
                "SELECT * FROM context_items WHERE snapshot_id = ? ORDER BY position", (snapshot["id"],),
            ).fetchall()
            compaction = connection.execute(
                """SELECT status, input_tokens, output_tokens, estimated_cost, error_code, finished_at
                   FROM context_compaction_attempts WHERE run_id = ?
                   ORDER BY created_at DESC LIMIT 1""", (run_id,),
            ).fetchone()
            usage = connection.execute(
                """SELECT input_tokens, output_tokens, cached_input_tokens, estimated_cost
                   FROM run_attempts WHERE run_id = ? ORDER BY attempt_number DESC LIMIT 1""",
                (run_id,),
            ).fetchone()
        result = dict(snapshot)
        result["compaction"] = dict(compaction) if compaction else None
        result["run_usage"] = dict(usage) if usage else None
        result["items"] = []
        for item in items:
            value = dict(item)
            value["metadata"] = json.loads(value.pop("metadata_json") or "{}")
            result["items"].append(value)
        return result

    def source_preview(self, run_id: str, item_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            item = connection.execute(
                """SELECT i.*, r.conversation_id, c.project_id
                   FROM context_items i JOIN context_snapshots s ON s.id = i.snapshot_id
                   JOIN model_runs r ON r.id = s.run_id JOIN conversations c ON c.id = r.conversation_id
                   WHERE s.run_id = ? AND i.id = ?""", (run_id, item_id),
            ).fetchone()
            if item is None:
                raise CoreDataError("context_source_unavailable", "The context source is unavailable.")
            if item["source_type"] == "attachment":
                return self._attachment_source_preview(run_id, item)
            if item["source_type"] == "rag_chunk" and item["project_id"] and item["included"]:
                from agent.rag.repository import RagRepository
                source = RagRepository(self.core, recover_jobs=False).preview_evidence(
                    item["project_id"], item["source_id"], length=8000)
                if source.get("run_id") != run_id or source["text_hash"] != item["source_hash"]:
                    raise CoreDataError("rag_evidence_forbidden", "该证据不属于当前上下文。")
                return {"item_id": item_id, "source_type": "rag_chunk", "source": source}
            if item["source_type"] in {"message", "current_message"}:
                source = connection.execute(
                    """SELECT id, role, content, sequence_no, created_at FROM messages
                       WHERE id = ? AND conversation_id = ?""",
                    (item["source_id"], item["conversation_id"]),
                ).fetchone()
            elif item["source_type"] == "project_knowledge" and item["project_id"] is not None:
                source = connection.execute(
                    """SELECT id, title, content, current_revision, updated_at FROM project_knowledge
                       WHERE id = ? AND project_id = ?""", (item["source_id"], item["project_id"]),
                ).fetchone()
            elif item["source_type"] == "global_instructions":
                source = connection.execute(
                    """SELECT id, content, enabled, revision, content_hash, default_version, updated_at
                       FROM global_instructions WHERE id = 1""",
                ).fetchone()
            else:
                source = None
        if source is None:
            raise CoreDataError("context_source_unavailable", "The context source is unavailable.")
        result = dict(source)
        if item["source_hash"]:
            content = str(result.get("content", ""))
            if hashlib.sha256(content.encode("utf-8")).hexdigest() != item["source_hash"]:
                raise CoreDataError("context_source_changed", "The context source has changed.")
        return {"item_id": item_id, "source_type": item["source_type"], "source": result}

    def _attachment_source_preview(self, run_id: str, item: sqlite3.Row) -> dict[str, Any]:
        from agent.context_engine.service import ContextEngineService
        from agent.context_engine.attachment_references import text_range
        run = self.core.get_model_run(run_id)
        attachment = self.core.get_attachment(item["source_id"])
        if attachment["conversation_id"] != item["conversation_id"] \
                or attachment["message_id"] != run["user_message_id"] or not item["included"]:
            raise CoreDataError("attachment_reference_forbidden", "This attachment is not an included source for this run.")
        metadata = json.loads(item["metadata_json"])
        # Both image and text preview consume exactly the same validated saved
        # selection as assembly, before reading either original or artifact bytes.
        selection = next(a for m in self.load_context_sources(run_id)["conversation"]["messages"]
                         if m["id"] == run["user_message_id"] for a in m.get("attachments", [])
                         if a["id"] == attachment["id"])
        if metadata.get("source_attachment_id") != (selection.get("source_attachment_id") or selection["id"]) \
                or metadata.get("source_message_id") != (selection.get("source_message_id") or selection["message_id"]):
            raise CoreDataError("context_source_changed", "The preview no longer matches the saved attachment source.")
        source_id = metadata.get("source_attachment_id")
        if source_id:
            source = self.core.get_attachment(source_id)
            if source["conversation_id"] != attachment["conversation_id"]:
                raise CoreDataError("attachment_reference_forbidden", "The attachment source is outside this conversation.")
        artifact_kind = "text.utf8" if selection["kind"] == "text" else "image.normalized"
        artifact = next((a for a in selection["artifacts"] if a["kind"] == artifact_kind), None)
        if metadata.get("artifact_id") != (artifact or {}).get("artifact_id"):
            raise CoreDataError("context_source_changed", "The attachment artifact has changed.")
        if item["source_hash"] != (artifact or attachment)["sha256"]:
            raise CoreDataError("context_source_changed", "The attachment source has changed.")
        # Revalidate both original and model-ready data; never return a path.
        ContextEngineService._read_attachment(attachment, text=False)
        is_text = attachment["kind"] == "text"
        content = ContextEngineService._read_attachment(attachment, text=is_text,
            model_variant=not is_text, artifact=artifact)
        result = {"id": attachment["id"], "name": attachment["name"], "kind": attachment["kind"],
                  "sha256": item["source_hash"], "source_attachment_id": source_id}
        if is_text:
            _, allowed_start, allowed_end = text_range(selection, content)
            start, end = item["source_start"], item["source_end"]
            if type(start) is not int or type(end) is not int or not allowed_start <= start <= end <= allowed_end:
                raise CoreDataError("attachment_reference_range_invalid", "The preview range exceeds the selected range.")
            result.update(content=content[start:end], source_start=start, source_end=end)
        else:
            media = (artifact or {}).get("media_type") or attachment.get("model_media_type") or attachment["media_type"]
            if media not in {"image/png", "image/jpeg"}:
                raise CoreDataError("image_format_unsupported", "This image cannot be previewed.")
            result["data_url"] = f"data:{media};base64,{content}"
        return {"item_id": item["id"], "source_type": "attachment", "source": result}

    def record_preparation_failure(
        self, *, run_id: str, conversation_id: str, context_generation: int,
        previous: dict[str, Any] | None, target: CompactionTarget, code: str,
    ) -> bool:
        """Persist recovery evidence, never a source claim or a replay authorization.

        The legacy NOT NULL source_hash column uses the empty string for absence;
        phase=preparation and a NULL token distinguish this from claimed work.
        """
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                """SELECT r.conversation_id, r.status, c.context_generation
                   FROM model_runs r JOIN conversations c ON c.id = r.conversation_id
                   WHERE r.id = ?""", (run_id,),
            ).fetchone()
            latest = connection.execute(
                """SELECT * FROM context_compaction_attempts WHERE conversation_id = ?
                   ORDER BY created_at DESC, rowid DESC LIMIT 1""", (conversation_id,),
            ).fetchone()
            if (run is None or run["conversation_id"] != conversation_id
                    or run["context_generation"] != context_generation
                    or run["status"] not in {"queued", "building_context", "waiting_context", "completed"}
                    or (dict(latest) if latest else None) != previous
                    or (latest and latest["status"] in {"queued", "running"})):
                return False
            attempt_id, now = str(uuid.uuid4()), _utc_now()
            connection.execute(
                """INSERT INTO context_compaction_attempts
                   (id, conversation_id, run_id, idempotency_key, context_generation, source_hash,
                    status, error_code, created_at, finished_at, target_json, budget_policy_json, claim_token)
                   VALUES (?, ?, ?, ?, ?, '', 'failed', ?, ?, ?, ?, ?, NULL)""",
                (attempt_id, conversation_id, run_id, f"preparation:{attempt_id}", context_generation,
                 code, now, now, json.dumps(asdict(target), sort_keys=True),
                 json.dumps({"policy_version": DEFAULT_POLICY.version, "phase": "preparation"}, sort_keys=True)),
            )
        return True

    def claim_compaction(
        self, *, conversation_id: str, run_id: str, source_hash: str,
        context_generation: int, target: CompactionTarget, policy_version: str,
    ) -> CompactionClaim:
        target_json = json.dumps(asdict(target), sort_keys=True, separators=(",", ":"))
        policy_json = json.dumps({"policy_version": policy_version}, sort_keys=True)
        identity = hashlib.sha256(
            f"{conversation_id}:{context_generation}:{source_hash}:{target_json}:{policy_json}".encode()
        ).hexdigest()
        attempt_id, token = str(uuid.uuid4()), str(uuid.uuid4())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT context_generation FROM conversations WHERE id = ?", (conversation_id,),
            ).fetchone()
            run = connection.execute(
                "SELECT conversation_id FROM model_runs WHERE id = ?", (run_id,),
            ).fetchone()
            if row is None or run is None or run["conversation_id"] != conversation_id:
                raise CoreDataError("context_source_unavailable", "The compaction source is unavailable.")
            if row["context_generation"] != context_generation:
                raise CoreDataError("context_source_changed", "The compaction source changed.")
            if connection.execute(
                """SELECT 1 FROM context_compaction_attempts WHERE conversation_id = ?
                   AND status IN ('queued', 'running')""", (conversation_id,),
            ).fetchone():
                raise CoreDataError("context_compaction_in_progress", "Context is already being organized.")
            connection.execute(
                """INSERT INTO context_compaction_attempts
                   (id, conversation_id, run_id, idempotency_key, context_generation, source_hash,
                    status, created_at, target_json, budget_policy_json, claim_token)
                   VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?)""",
                (attempt_id, conversation_id, run_id, f"{identity}:{token}", context_generation,
                 source_hash, _utc_now(), target_json, policy_json, token),
            )
        return CompactionClaim(attempt_id, token, target)

    def begin_compaction(
        self, *, conversation_id: str, run_id: str, context_generation: int,
        source_hash: str, idempotency_key: str,
    ) -> dict[str, Any]:
        """Compatibility entry point for older internal callers."""
        run = self.core.get_model_run(run_id)
        self.claim_compaction(
            conversation_id=conversation_id, run_id=run_id, source_hash=source_hash,
            context_generation=context_generation,
            target=CompactionTarget(run["provider_profile_id"], run["model_id"], "unknown", None, None),
            policy_version=DEFAULT_POLICY.version,
        )
        return self.compaction_status(conversation_id)

    def recoverable_compactions(self, provider_profile_id: str) -> list[dict[str, Any]]:
        """Only the latest conversation attempt can authorize recovery."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM context_compaction_attempts ORDER BY created_at DESC, rowid DESC"
            ).fetchall()
        result, seen = [], set()
        for row in rows:
            item = dict(row)
            if item["conversation_id"] in seen:
                continue
            seen.add(item["conversation_id"])
            if json.loads(item["budget_policy_json"] or "{}").get("phase") == "preparation":
                continue  # Only an explicit user resume may retry unprepared work.
            target = json.loads(item["target_json"] or "{}")
            if item["status"] in {"failed", "interrupted"} and target.get("provider_profile_id") == provider_profile_id:
                result.append(item)
        return result

    def fail_compaction(
        self, attempt_id: str, code: str, *, claim_token: str, interrupted: bool = False,
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE context_compaction_attempts SET status = ?, error_code = ?, finished_at = ?
                   WHERE id = ? AND claim_token = ? AND status = 'running'""",
                ("interrupted" if interrupted else "failed", code, _utc_now(), attempt_id, claim_token),
            )
        return cursor.rowcount == 1

    def record_compaction_usage(
        self, claim: CompactionClaim, *, input_tokens: int | None, output_tokens: int | None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE context_compaction_attempts SET input_tokens = ?, output_tokens = ?
                   WHERE id = ? AND claim_token = ? AND status = 'running'""",
                (input_tokens, output_tokens, claim.attempt_id, claim.claim_token),
            )

    def validate_compaction_claim(self, claim: CompactionClaim, expected_generation: int) -> None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT a.status, a.claim_token, a.context_generation,
                          c.context_generation AS current_generation
                   FROM context_compaction_attempts a JOIN conversations c ON c.id = a.conversation_id
                   WHERE a.id = ?""", (claim.attempt_id,),
            ).fetchone()
        if row is None or row["status"] != "running" or row["claim_token"] != claim.claim_token:
            raise CoreDataError("invalid_compaction_state", "The compaction claim is no longer active.")
        if row["context_generation"] != expected_generation or row["current_generation"] != expected_generation:
            raise CoreDataError("context_source_changed", "The compaction source changed before dispatch.")

    def commit_checkpoint(
        self, *, attempt_id: str, claim_token: str, conversation_id: str, run_id: str,
        expected_generation: int, parent_checkpoint_id: str | None,
        start_sequence: int, start_offset: int, through_sequence: int,
        through_offset: int | None, content: dict[str, Any], source_hash: str,
        provider_profile_id: str, model_id: str, input_tokens: int | None,
        output_tokens: int | None, estimated_cost: float | None,
    ) -> dict[str, Any] | None:
        checkpoint_id, now = str(uuid.uuid4()), _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT context_generation FROM conversations WHERE id = ?", (conversation_id,),
            ).fetchone()
            attempt = connection.execute(
                "SELECT * FROM context_compaction_attempts WHERE id = ?", (attempt_id,),
            ).fetchone()
            if row is None or attempt is None:
                raise CoreDataError("context_source_unavailable", "The compaction source is unavailable.")
            if attempt["status"] != "running" or attempt["claim_token"] != claim_token:
                return None
            target = json.loads(attempt["target_json"] or "{}")
            if (attempt["conversation_id"] != conversation_id or attempt["run_id"] != run_id
                    or attempt["context_generation"] != expected_generation
                    or target.get("provider_profile_id") != provider_profile_id
                    or target.get("model_id") != model_id):
                return None
            if row["context_generation"] != expected_generation or attempt["source_hash"] != source_hash:
                connection.execute(
                    """UPDATE context_compaction_attempts SET status = 'stale',
                           error_code = 'context_source_changed', finished_at = ? WHERE id = ?""",
                    (now, attempt_id),
                )
                return None
            active = connection.execute(
                """SELECT id FROM context_checkpoints WHERE conversation_id = ? AND status = 'valid'
                   ORDER BY through_sequence DESC, COALESCE(through_offset, 2147483647) DESC,
                            created_at DESC LIMIT 1""", (conversation_id,),
            ).fetchone()
            active_id = active["id"] if active else None
            if active_id != parent_checkpoint_id:
                connection.execute(
                    """UPDATE context_compaction_attempts SET status = 'stale',
                           error_code = 'context_source_changed', finished_at = ? WHERE id = ?""",
                    (now, attempt_id),
                )
                return None
            connection.execute(
                """INSERT INTO context_checkpoints(
                       id, conversation_id, parent_checkpoint_id, start_sequence, start_offset,
                       through_sequence, through_offset, content_json, source_hash,
                        provider_profile_id, model_id, policy_version, status,
                       input_tokens, output_tokens, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'valid', ?, ?, ?)""",
                (checkpoint_id, conversation_id, parent_checkpoint_id, start_sequence, start_offset,
                 through_sequence, through_offset,
                 json.dumps(content, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
                 source_hash, provider_profile_id, model_id, DEFAULT_POLICY.version,
                 input_tokens, output_tokens, now),
            )
            connection.execute(
                """UPDATE context_compaction_attempts SET status = 'valid', checkpoint_id = ?,
                       input_tokens = ?, output_tokens = ?, estimated_cost = ?, finished_at = ?
                   WHERE id = ?""",
                (checkpoint_id, input_tokens, output_tokens, estimated_cost, now, attempt_id),
            )
            connection.execute(
                """UPDATE conversations SET context_generation = context_generation + 1
                   WHERE id = ?""", (conversation_id,),
            )
            connection.execute(
                 "UPDATE model_runs SET automatic_compaction_used = 1 WHERE id = ?", (run_id,),
            )
        return {"id": checkpoint_id, "conversation_id": conversation_id,
                "through_sequence": through_sequence, "through_offset": through_offset,
                "source_hash": source_hash, "content": content, "created_at": now}

    def pause_compaction(self, conversation_id: str, code: str = "cancelled_by_user") -> dict[str, Any] | None:
        now = _utc_now()
        with self._connect() as connection:
            row = connection.execute(
                """SELECT id FROM context_compaction_attempts WHERE conversation_id = ?
                   AND status IN ('queued','running') ORDER BY created_at DESC LIMIT 1""",
                (conversation_id,),
            ).fetchone()
            if row is None:
                return self.compaction_status(conversation_id)
            connection.execute(
                """UPDATE context_compaction_attempts SET status = 'interrupted', error_code = ?,
                       finished_at = ?, claim_token = NULL WHERE id = ? AND status IN ('queued','running')""",
                (code, now, row["id"]),
            )
        return self.compaction_status(conversation_id)

    def interrupt_compactions(self) -> list[str]:
        now = _utc_now()
        with self._connect() as connection:
            ids = [row["id"] for row in connection.execute(
                "SELECT id FROM context_compaction_attempts WHERE status IN ('queued','running')"
            ).fetchall()]
            connection.execute(
                """UPDATE context_compaction_attempts SET status = 'interrupted',
                       error_code = 'core_interrupted', finished_at = ?, claim_token = NULL
                   WHERE status IN ('queued','running')""", (now,),
            )
            connection.execute(
                """UPDATE model_runs SET status = 'waiting_context', finished_at = NULL
                   WHERE status = 'interrupted' AND error_code = 'core_interrupted'
                     AND id IN (
                         SELECT a.run_id FROM context_compaction_attempts a
                         WHERE a.status = 'interrupted' AND a.error_code = 'core_interrupted'
                           AND a.id = (SELECT latest.id FROM context_compaction_attempts latest
                                       WHERE latest.conversation_id = a.conversation_id
                                       ORDER BY latest.created_at DESC, latest.rowid DESC LIMIT 1)
                     )"""
            )
        return ids
