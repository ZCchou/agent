from __future__ import annotations

import json
import hashlib
import uuid
from typing import Any

from agent.execution.domain import ExecutionPlan, InvocationSpec, stable_hash
from agent.storage import CoreDataError, _open_database, _utc_now


class ExecutionRepository:
    """SQL-only persistence for immutable execution plans and their steps."""

    def __init__(self, database_path: Any) -> None:
        self.database_path = database_path

    def save_plan(
        self, run_id: str, plan: ExecutionPlan, requirements: dict[str, Any],
        resolved_intent: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _utc_now()
        with _open_database(self.database_path) as connection:
            existing = connection.execute(
                "SELECT id, plan_hash FROM execution_plans WHERE run_id = ?", (run_id,),
            ).fetchone()
            if existing is not None:
                if existing["plan_hash"] != plan.plan_hash:
                    raise CoreDataError("execution_plan_stale", "The run already has a different plan.")
                return self._get_with_connection(connection, existing["id"])
            connection.execute(
                """INSERT INTO execution_plans(
                       id, run_id, status, selection_mode, routing_preset, policy_version,
                       requirements_json, requirements_hash, coordinator_provider_profile_id,
                       coordinator_model_id, generation_protocol, web_route,
                       web_adapter_or_backend_id, web_backend_kind, reasoning_effort, selection_source,
                       selection_reason_code, budget_json, estimated_extra_online_cost_usd,
                       plan_hash, route_catalog_hash, route_policy_version, authority_generation,
                       resolved_intent_json, resolution_hash, resolved_at, created_at, updated_at
                   ) VALUES (?, ?, 'planned', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (plan.plan_id, run_id, plan.selection_mode, plan.routing_preset,
                 plan.policy_version, json.dumps(requirements, ensure_ascii=False, sort_keys=True),
                 plan.requirements_hash, plan.coordinator_provider_profile_id,
                 plan.coordinator_model_id, plan.generation_protocol, plan.web_route,
                 plan.web_adapter_or_backend_id, plan.web_backend_kind,
                 plan.reasoning_effort, plan.selection_source,
                 plan.selection_reason_code, json.dumps(plan.budget.to_dict(), sort_keys=True),
                 None if plan.estimated_extra_online_cost_usd is None else str(plan.estimated_extra_online_cost_usd),
                 plan.plan_hash, plan.route_catalog_hash, plan.route_policy_version,
                 plan.authority_generation,
                 json.dumps(resolved_intent or requirements, ensure_ascii=False, sort_keys=True),
                 stable_hash(resolved_intent or requirements), now, now, now),
            )
            for step in plan.steps:
                connection.execute(
                    """INSERT INTO execution_steps(
                           id, plan_id, phase, order_index, step_type, provider_profile_id,
                           model_id, protocol_id, status, required, input_source_ids_json,
                           activation_condition,
                           invocation_route_id, adapter_id, capability_signature_json,
                           route_config_fingerprint, planned_target_fingerprint
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (step.step_id, plan.plan_id, step.phase, step.order_index, step.step_type,
                     step.provider_profile_id, step.model_id, step.protocol_id,
                     1 if step.required else 0, json.dumps(list(step.input_source_ids)),
                     step.activation_condition,
                     step.invocation_route_id, step.adapter_id,
                     json.dumps(list(step.capability_signature)),
                     step.route_config_fingerprint, step.route_config_fingerprint),
                )
            connection.execute(
                """UPDATE model_runs SET execution_plan_id = ?, execution_policy_version = ?
                   WHERE id = ?""", (plan.plan_id, plan.policy_version, run_id),
            )
            return self._get_with_connection(connection, plan.plan_id)

    def recent_web_intents(self, conversation_id: str, limit: int = 3) -> list[dict[str, Any]]:
        with _open_database(self.database_path) as connection:
            rows = connection.execute(
                """SELECT r.id AS run_id, r.created_at, r.web_grounding_status,
                          p.resolved_intent_json,
                          EXISTS(SELECT 1 FROM web_operations o
                                  WHERE o.run_id = r.id AND o.status = 'completed') AS search_executed
                     FROM model_runs r JOIN execution_plans p ON p.run_id = r.id
                    WHERE r.conversation_id = ? AND r.status = 'completed'
                    ORDER BY r.created_at DESC LIMIT ?""",
                (conversation_id, limit),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                resolved = json.loads(row["resolved_intent_json"] or "{}")
            except json.JSONDecodeError:
                resolved = {}
            result.append({
                "run_id": row["run_id"], "created_at": row["created_at"],
                "resolved_intent": resolved if isinstance(resolved, dict) else {},
                "search_executed": bool(row["search_executed"]),
                "web_grounding_status": row["web_grounding_status"],
            })
        return result

    def update_resolved_intent(self, plan_id: str, intent: dict[str, Any]) -> dict[str, Any]:
        """Finalize an ambiguous intent once without changing the immutable route topology."""
        now = _utc_now()
        encoded = json.dumps(intent, ensure_ascii=False, sort_keys=True)
        with _open_database(self.database_path) as connection:
            row = connection.execute(
                "SELECT resolved_intent_json FROM execution_plans WHERE id = ?", (plan_id,),
            ).fetchone()
            if row is None:
                raise CoreDataError("execution_plan_not_found", "The execution plan does not exist.")
            try:
                current = json.loads(row["resolved_intent_json"] or "{}")
            except json.JSONDecodeError:
                current = {}
            if isinstance(current, dict) and current.get("requirement") != "optional" \
                    and "advisor" not in current:
                return self._get_with_connection(connection, plan_id)
            if isinstance(current, dict):
                # Preserve the auditable rule/advisor decision while adding the
                # finalized web-intent fields produced by the execution step.
                intent = {**current, **intent}
                encoded = json.dumps(intent, ensure_ascii=False, sort_keys=True)
            connection.execute(
                """UPDATE execution_plans
                      SET resolved_intent_json = ?, resolution_hash = ?, resolved_at = ?, updated_at = ?
                    WHERE id = ?""",
                (encoded, stable_hash(intent), now, now, plan_id),
            )
            return self._get_with_connection(connection, plan_id)

    def save_invocation_spec(self, spec: InvocationSpec) -> dict[str, Any]:
        now = _utc_now()
        with _open_database(self.database_path) as connection:
            plan = connection.execute(
                """SELECT authority_generation, web_adapter_or_backend_id, web_backend_kind
                   FROM execution_plans WHERE id = ?""", (spec.plan_id,),
            ).fetchone()
            step = connection.execute(
                "SELECT plan_id, planned_target_fingerprint FROM execution_steps WHERE id = ?",
                (spec.step_id,),
            ).fetchone()
            if plan is None or step is None or step["plan_id"] != spec.plan_id:
                raise CoreDataError("invocation_spec_invalid", "Invocation plan or step is invalid.")
            if int(plan["authority_generation"]) != spec.authority_generation:
                raise CoreDataError("invocation_spec_stale", "Invocation authority changed.")
            if spec.kind == "model":
                planned_fingerprint = step["planned_target_fingerprint"]
                if planned_fingerprint not in {"legacy-v14", spec.route_fingerprint}:
                    raise CoreDataError(
                        "invocation_spec_stale", "Invocation route changed after planning."
                    )
            elif plan["web_adapter_or_backend_id"] != spec.provider_or_backend_id \
                    or plan["web_backend_kind"] not in {"tavily", "searxng"}:
                raise CoreDataError(
                    "invocation_spec_stale", "Host search backend changed after planning."
                )
            existing = connection.execute(
                "SELECT * FROM invocation_specs WHERE step_id=? AND round_no=? AND attempt_no=?",
                (spec.step_id, spec.round_no, spec.attempt_no),
            ).fetchone()
            if existing is not None:
                if existing["spec_hash"] != spec.spec_hash:
                    raise CoreDataError(
                        "invocation_spec_invalid", "The invocation sequence already has another spec.",
                    )
                return self._decode_invocation_spec(existing)
            connection.execute(
                """INSERT INTO invocation_specs(
                       id, plan_id, step_id, round_no, attempt_no, kind,
                       provider_or_backend_id, model_id, route_id, protocol_id, adapter_id,
                       endpoint_role, route_fingerprint, capability_signature_json,
                       input_refs_json, tools_json, generation_json, limits_json,
                       serialization_version, authority_generation, status, spec_hash, created_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    spec.spec_id, spec.plan_id, spec.step_id, spec.round_no, spec.attempt_no,
                    spec.kind, spec.provider_or_backend_id, spec.model_id, spec.route_id,
                    spec.protocol_id, spec.adapter_id, spec.endpoint_role,
                    spec.route_fingerprint, json.dumps(list(spec.capability_signature)),
                    json.dumps({
                        "context_snapshot_id": spec.context_snapshot_id,
                        "message_id": spec.message_id,
                        "artifact_ids": list(spec.artifact_ids),
                        "rejection_receipt_ids": list(spec.rejection_receipt_ids),
                        "tool_result_ids": list(spec.tool_result_ids),
                    }, sort_keys=True),
                    json.dumps({
                        "contract_version": spec.tool_contract_version,
                        "allowed_names": list(spec.allowed_tool_names),
                        "transport": spec.tool_transport,
                        "schema_hash": spec.tool_schema_hash,
                        "native_search_mode": spec.native_search_mode,
                    }, sort_keys=True),
                    json.dumps({
                        "reasoning_effort": spec.reasoning_effort,
                        "max_output_tokens": spec.max_output_tokens,
                        "response_format": spec.response_format,
                        **({"response_schema": json.loads(spec.response_schema_json)} if spec.response_schema_json else {}),
                    }, sort_keys=True),
                    json.dumps({
                        "connect_timeout_ms": spec.connect_timeout_ms,
                        "activity_timeout_ms": spec.activity_timeout_ms,
                        **({"first_output_timeout_ms": spec.first_output_timeout_ms} if spec.first_output_timeout_ms is not None else {}),
                        "total_timeout_ms": spec.total_timeout_ms,
                    }, sort_keys=True),
                    spec.serialization_version, spec.authority_generation, spec.status,
                    spec.spec_hash, now,
                ),
            )
            return self.get_invocation_spec(spec.spec_id, connection=connection)

    def get_invocation_spec(
        self, spec_id: str, *, connection: Any | None = None,
    ) -> dict[str, Any]:
        if connection is not None:
            row = connection.execute(
                "SELECT * FROM invocation_specs WHERE id = ?", (spec_id,),
            ).fetchone()
            if row is None:
                raise CoreDataError("invocation_spec_invalid", "Invocation spec does not exist.")
            return self._decode_invocation_spec(row)
        with _open_database(self.database_path) as owned:
            return self.get_invocation_spec(spec_id, connection=owned)

    def mark_invocation_dispatched(self, spec_id: str, request_fingerprint: str) -> None:
        with _open_database(self.database_path) as connection:
            cursor = connection.execute(
                """UPDATE invocation_specs SET status='dispatched', request_fingerprint=?,
                          dispatched_at=? WHERE id=? AND status='prepared'""",
                (request_fingerprint, _utc_now(), spec_id),
            )
            if cursor.rowcount != 1:
                raise CoreDataError(
                    "invocation_spec_stale", "Invocation spec was already dispatched or changed.",
                )

    def finish_invocation(
        self, spec_id: str, status: str, error_code: str | None = None,
    ) -> None:
        if status not in {"completed", "failed", "cancelled", "interrupted", "unknown_outcome"}:
            raise CoreDataError("invocation_spec_invalid", "Invocation terminal status is invalid.")
        with _open_database(self.database_path) as connection:
            cursor = connection.execute(
                """UPDATE invocation_specs SET status=?, error_code=?, finished_at=?
                   WHERE id=? AND status IN ('dispatched','streaming')""",
                (status, error_code, _utc_now(), spec_id),
            )
            if cursor.rowcount != 1:
                raise CoreDataError("invocation_spec_stale", "Invocation spec is not active.")

    @staticmethod
    def _decode_invocation_spec(row: Any) -> dict[str, Any]:
        value = dict(row)
        value["capability_signature"] = json.loads(value.pop("capability_signature_json"))
        value["inputs"] = json.loads(value.pop("input_refs_json"))
        value["tools"] = json.loads(value.pop("tools_json"))
        value["generation"] = json.loads(value.pop("generation_json"))
        value["limits"] = json.loads(value.pop("limits_json"))
        return value

    def get_execution_plan(self, plan_id: str) -> dict[str, Any]:
        with _open_database(self.database_path) as connection:
            return self._get_with_connection(connection, plan_id)

    def get_for_run(self, run_id: str) -> dict[str, Any] | None:
        with _open_database(self.database_path) as connection:
            row = connection.execute("SELECT id FROM execution_plans WHERE run_id = ?", (run_id,)).fetchone()
            return None if row is None else self._get_with_connection(connection, row["id"])

    def transition_plan(self, plan_id: str, expected: set[str], target: str) -> dict[str, Any]:
        with _open_database(self.database_path) as connection:
            row = connection.execute("SELECT status FROM execution_plans WHERE id = ?", (plan_id,)).fetchone()
            if row is None:
                raise CoreDataError("execution_plan_not_found", "The execution plan does not exist.")
            if row["status"] not in expected:
                raise CoreDataError("invalid_execution_plan_transition", "Execution plan transition is invalid.")
            connection.execute(
                "UPDATE execution_plans SET status = ?, updated_at = ? WHERE id = ?",
                (target, _utc_now(), plan_id),
            )
            return self._get_with_connection(connection, plan_id)

    def transition_step(
        self, step_id: str, expected: set[str], target: str, error_code: str | None = None,
    ) -> dict[str, Any]:
        now = _utc_now()
        with _open_database(self.database_path) as connection:
            row = connection.execute("SELECT * FROM execution_steps WHERE id = ?", (step_id,)).fetchone()
            if row is None:
                raise CoreDataError("execution_step_not_found", "The execution step does not exist.")
            if row["status"] not in expected:
                raise CoreDataError("invalid_execution_step_transition", "Execution step transition is invalid.")
            started = now if target == "running" else row["started_at"]
            finished = now if target in {
                "completed", "skipped", "failed", "cancelled", "interrupted", "unknown_outcome",
                "stale",
            } else None
            connection.execute(
                """UPDATE execution_steps SET status = ?, error_code = ?, started_at = ?,
                       skip_reason_code = CASE WHEN ? = 'skipped' THEN ? ELSE skip_reason_code END,
                       finished_at = COALESCE(?, finished_at) WHERE id = ?""",
                (target, error_code, started, target, error_code, finished, step_id),
            )
            return dict(connection.execute("SELECT * FROM execution_steps WHERE id = ?", (step_id,)).fetchone())

    def update_step_usage(self, step_id: str, usage: dict[str, Any]) -> None:
        safe_usage = {
            key: int(value) for key, value in usage.items()
            if key in {"input_tokens", "output_tokens", "cached_input_tokens", "search_calls"}
            and isinstance(value, int) and not isinstance(value, bool) and value >= 0
        }
        with _open_database(self.database_path) as connection:
            cursor = connection.execute(
                "UPDATE execution_steps SET usage_json = ? WHERE id = ?",
                (json.dumps(safe_usage, sort_keys=True), step_id),
            )
            if cursor.rowcount != 1:
                raise CoreDataError(
                    "execution_step_not_found", "The execution step does not exist."
                )

    def bind_step_inputs(
        self, step_id: str, source_ids: list[str], input_hash: str,
    ) -> None:
        normalized = list(dict.fromkeys(source_ids))
        with _open_database(self.database_path) as connection:
            cursor = connection.execute(
                """UPDATE execution_steps
                   SET input_source_ids_json = ?, input_hash = ?
                   WHERE id = ? AND status IN ('queued','running','interrupted')""",
                (json.dumps(normalized, sort_keys=True), input_hash, step_id),
            )
            if cursor.rowcount != 1:
                raise CoreDataError(
                    "execution_step_not_found", "Execution step inputs cannot be updated."
                )

    def reset_for_retry(self, run_id: str) -> None:
        with _open_database(self.database_path) as connection:
            row = connection.execute("SELECT id FROM execution_plans WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                return
            connection.execute(
                """UPDATE execution_steps SET status = 'queued', error_code = NULL,
                       started_at = NULL, finished_at = NULL
                WHERE plan_id = ? AND status IN
                    ('failed','cancelled','interrupted','unknown_outcome')""", (row["id"],),
            )
            connection.execute(
                "UPDATE execution_plans SET status = 'planned', updated_at = ? WHERE id = ?",
                (_utc_now(), row["id"]),
            )

    def has_run_dispatch_evidence(self, run_id: str) -> bool:
        """A planless legacy child is recoverable only before any observable dispatch."""
        with _open_database(self.database_path) as connection:
            return bool(connection.execute(
                """SELECT EXISTS(SELECT 1 FROM run_attempts WHERE run_id=? AND
                       (invocation_spec_id IS NOT NULL OR produced_output=1 OR input_tokens>0
                        OR output_tokens>0 OR estimated_cost>0))
                   OR EXISTS(SELECT 1 FROM tool_operations WHERE run_id=?)
                   OR EXISTS(SELECT 1 FROM web_operations WHERE run_id=?)
                   OR EXISTS(SELECT 1 FROM assistant_drafts WHERE run_id=? AND content<>'')
                   OR EXISTS(SELECT 1 FROM model_runs WHERE id=? AND assistant_message_id IS NOT NULL)""",
                (run_id,) * 5,
            ).fetchone()[0])

    def interrupt_active(self) -> None:
        """Persist plan/step interruption before model tasks disappear on Core exit."""
        now = _utc_now()
        with _open_database(self.database_path) as connection:
            connection.execute(
                """UPDATE invocation_specs SET status='unknown_outcome',
                       error_code='core_interrupted', finished_at=?
                   WHERE status IN ('dispatched','streaming')""",
                (now,),
            )
            connection.execute(
                """UPDATE invocation_specs SET status='interrupted',
                       error_code='core_interrupted', finished_at=?
                   WHERE status='prepared'""",
                (now,),
            )
            running_steps = connection.execute(
                "SELECT id, step_type, plan_id FROM execution_steps WHERE status = 'running'"
            ).fetchall()
            for step in running_steps:
                has_dispatched_search = step["step_type"] == "web_research" and connection.execute(
                    """SELECT 1 FROM web_operations o
                       JOIN execution_plans p ON p.run_id = o.run_id
                       WHERE p.id = ? AND o.status IN ('dispatched','searching') LIMIT 1""",
                    (step["plan_id"],),
                ).fetchone() is not None
                connection.execute(
                    """UPDATE execution_steps SET status = ?, error_code = ?, finished_at = ?
                       WHERE id = ? AND status = 'running'""",
                    (
                        "unknown_outcome" if has_dispatched_search else "interrupted",
                        "execution_step_unknown_outcome" if has_dispatched_search else "core_interrupted",
                        now, step["id"],
                    ),
                )
            connection.execute(
                """UPDATE execution_plans SET status = 'interrupted', updated_at = ?
                   WHERE status IN ('resolving','executing','synthesizing')""", (now,),
            )

    def save_artifact(
        self, step_id: str, artifact_type: str, content: dict[str, Any], *,
        trust: str = "model_inference", derived_from_untrusted: bool = False,
        source_refs: list[str] | None = None,
        select_as_output: bool = True,
    ) -> dict[str, Any]:
        encoded = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        byte_size = len(encoded.encode("utf-8"))
        if byte_size > 65536:
            raise CoreDataError("execution_artifact_too_large", "Specialist output exceeds 64 KiB.")
        artifact_id = str(uuid.uuid4())
        with _open_database(self.database_path) as connection:
            total = connection.execute(
                """SELECT COALESCE(SUM(a.byte_size), 0)
                     FROM execution_artifacts a
                     JOIN execution_steps existing_step ON existing_step.id = a.step_id
                     JOIN execution_steps target_step ON target_step.plan_id = existing_step.plan_id
                     WHERE target_step.id = ?""",
                (step_id,),
            ).fetchone()[0]
            if total + byte_size > 131072:
                raise CoreDataError(
                    "execution_artifact_budget_exceeded",
                    "Execution artifacts exceed the 128 KiB plan budget.",
                )
            connection.execute(
                """INSERT INTO execution_artifacts(
                       id, step_id, artifact_type, content_json, trust, derived_from_untrusted,
                       source_refs_json, content_hash, byte_size, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (artifact_id, step_id, artifact_type, encoded, trust,
                 1 if derived_from_untrusted else 0, json.dumps(source_refs or []),
                 hashlib.sha256(encoded.encode("utf-8")).hexdigest(), byte_size, _utc_now()),
            )
            if select_as_output:
                connection.execute(
                    "UPDATE execution_steps SET artifact_id = ? WHERE id = ?", (artifact_id, step_id),
                )
            return dict(connection.execute(
                "SELECT * FROM execution_artifacts WHERE id = ?", (artifact_id,),
            ).fetchone())

    @staticmethod
    def _get_with_connection(connection: Any, plan_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM execution_plans WHERE id = ?", (plan_id,)).fetchone()
        if row is None:
            raise CoreDataError("execution_plan_not_found", "The execution plan does not exist.")
        steps = connection.execute(
            "SELECT * FROM execution_steps WHERE plan_id = ? ORDER BY order_index, id", (plan_id,),
        ).fetchall()
        result = dict(row)
        for key in ("requirements_json", "budget_json", "resolved_intent_json"):
            result[key[:-5]] = json.loads(result.pop(key))
        result["steps"] = []
        result["degraded_capabilities"] = result["requirements"].get(
            "capability_advisories", [],
        )
        for step in steps:
            item = dict(step)
            item["input_source_ids"] = json.loads(item.pop("input_source_ids_json"))
            item["capability_signature"] = json.loads(
                item.pop("capability_signature_json", '["text"]')
            )
            item["usage"] = json.loads(item.pop("usage_json"))
            artifact = connection.execute(
                "SELECT * FROM execution_artifacts WHERE step_id = ?", (item["id"],),
            ).fetchone()
            if artifact is not None:
                artifact_value = dict(artifact)
                artifact_value["content"] = json.loads(artifact_value.pop("content_json"))
                artifact_value["source_refs"] = json.loads(artifact_value.pop("source_refs_json"))
                item["artifact"] = artifact_value
            result["steps"].append(item)
        invocation_rows = connection.execute(
            """SELECT * FROM invocation_specs WHERE plan_id = ?
               ORDER BY created_at, step_id, round_no, attempt_no""",
            (plan_id,),
        ).fetchall()
        result["invocations"] = []
        for invocation_row in invocation_rows:
            invocation = ExecutionRepository._decode_invocation_spec(invocation_row)
            # Public plan diagnostics contain identity and safe metadata only.
            invocation.pop("request_fingerprint", None)
            result["invocations"].append(invocation)
        return result
