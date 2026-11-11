from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Awaitable, Callable

from agent.storage import CoreDataError
from agent.tool_contracts import ToolCall, canonical_arguments, search_completeness
from agent.tool_repository import ACTIVE_TOOL_STATES, ToolRepository

EventSink = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class ToolResolution:
    success: bool
    output: str
    error_code: str | None


class ToolLoopService:
    # Calls/reads belong to one explicit attempt, not the run's audit sequence.
    MAX_ROUNDS = 256
    MAX_READ_OPERATIONS = 200
    # Run allowance is separate from the 20-mutation change-set contract.
    MAX_FILE_MUTATIONS = 200
    MAX_RESULT_BYTES = 20 * 1024 * 1024
    MAX_WEB_SEARCHES = 3
    MAX_WEB_OPENS = 8
    MAX_WEB_RESULT_CHARACTERS = 120_000

    def __init__(self, repository: ToolRepository, event_sink: EventSink) -> None:
        self.repository = repository
        self.event_sink = event_sink
        self.waiters: dict[str, asyncio.Future[ToolResolution]] = {}
        self.web_result_characters: dict[str, int] = {}
        self.repository.interrupt_active()

    def configure_limits(self, values: Any) -> None:
        fields = {"calls": "MAX_ROUNDS", "reads": "MAX_READ_OPERATIONS", "mutations": "MAX_FILE_MUTATIONS",
                  "web_searches": "MAX_WEB_SEARCHES", "web_opens": "MAX_WEB_OPENS",
                  "web_characters": "MAX_WEB_RESULT_CHARACTERS", "max_result_bytes": "MAX_RESULT_BYTES"}
        if not isinstance(values, dict) or any(key not in fields or type(value) is not int or value < 0
                                               for key, value in values.items()):
            raise CoreDataError("invalid_capability_policy", "Runtime tool budgets must be nonnegative integer limits.")
        for key, value in values.items():
            setattr(self, fields[key], value)

    async def request(
        self, *, run_id: str, attempt_id: str, sequence: int, call: ToolCall,
        budget_attempt_ids: tuple[str, ...] = (),
        local_executor: Callable[[], str] | None = None,
        async_executor: Callable[[], Awaitable[str]] | None = None,
    ) -> tuple[dict[str, Any], ToolResolution]:
        self._enforce_limits(run_id, attempt_id, call, budget_attempt_ids)
        operation = self.repository.create(run_id, attempt_id, sequence, call)
        if operation["status"] in {"completed", "failed", "rejected"}:
            raise CoreDataError(
                "tool_operation_replayed",
                "A completed tool call cannot be replayed without its transient result.",
            )
        operation = self.repository.transition(
            operation["id"], {"requested"}, "validating",
        )
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[ToolResolution] = loop.create_future()
        self.waiters[operation["id"]] = waiter
        try:
            if local_executor is not None or async_executor is not None:
                self.update_status(operation["id"], "executing")
                try:
                    executor_task = asyncio.create_task(async_executor() if async_executor else asyncio.to_thread(local_executor))
                    def cancel_executor(result):
                        if result.cancelled():
                            executor_task.cancel()
                    waiter.add_done_callback(cancel_executor)
                    output = await executor_task
                except CoreDataError as error:
                    self.resolve(operation["id"], success=False, output=error.message, error_code=error.code)
                except Exception:
                    code = "document_operation_failed" if call.name.startswith('document.') else "rag_operation_failed" if async_executor else "skill_unavailable"
                    self.resolve(operation["id"], success=False, output="Local operation failed. Check its state before retrying.", error_code=code)
                else:
                    self.resolve(operation["id"], success=True, output=output)
            else:
                await self.event_sink({
                    "run_id": run_id,
                    "event_type": "run.tool_requested",
                    "data": {"operation": operation},
                })
            resolution = await waiter
            return self.repository.get(operation["id"]), resolution
        finally:
            self.waiters.pop(operation["id"], None)

    def update_status(self, operation_id: str, status: str) -> dict[str, Any]:
        if status not in {"reviewing", "waiting_user", "executing"}:
            raise CoreDataError("invalid_tool_transition", "Host tool status is invalid.")
        operation = self.repository.get(operation_id)
        if operation_id not in self.waiters:
            raise CoreDataError("tool_operation_not_waiting", "Tool operation is not waiting for Host.")
        return self.repository.transition(
            operation_id, ACTIVE_TOOL_STATES - {status}, status,
        )

    def resolve(
        self, operation_id: str, *, success: bool, output: str,
        error_code: str | None = None, rejected: bool = False,
    ) -> dict[str, Any]:
        waiter = self.waiters.get(operation_id)
        if waiter is None or waiter.done():
            raise CoreDataError("tool_operation_not_waiting", "Tool operation is not waiting for a result.")
        encoded = output.encode("utf-8")
        if len(encoded) > self.MAX_RESULT_BYTES:
            raise CoreDataError("tool_data_limit_exceeded", "Tool result exceeds the run data budget.")
        operation = self.repository.get(operation_id)
        if success and operation["tool_name"] in {"web.search", "web.open"}:
            used = self.web_result_characters.get(operation["run_id"], 0)
            if used + len(output) > self.MAX_WEB_RESULT_CHARACTERS:
                raise CoreDataError(
                    "search_budget_exceeded",
                    "The run exceeded its web-content character budget.",
                )
            self.web_result_characters[operation["run_id"]] = used + len(output)
        target = "rejected" if rejected else "completed" if success else "failed"
        summary = f"{len(encoded)} bytes returned" if success else (error_code or "tool_failed")
        if success and operation["tool_name"] == "fs.search":
            try:
                completeness = search_completeness(json.loads(output))
            except (ValueError, TypeError):
                completeness = None
            if completeness is not None:
                summary = json.dumps({"output_bytes": len(encoded), "search_completeness": completeness},
                                     separators=(",", ":"))
        if operation["tool_name"] == "terminal.run" and success:
            # Host returns bounded process output. Keep the actual outcome for audit/reload.
            result = json.loads(output)
            if not isinstance(result, dict) or not {"stdout", "stderr", "exit_code", "duration_ms", "timed_out", "cancelled", "stdout_truncated", "stderr_truncated"} <= result.keys():
                raise CoreDataError("invalid_terminal_result", "Terminal result is incomplete.")
            summary = output
        completed = self.repository.transition(
            operation_id, {operation["status"]}, target,
            result_summary=summary, result_hash=sha256(encoded).hexdigest(), error_code=error_code,
        )
        waiter.set_result(ToolResolution(success, output, error_code))
        return completed

    def cancel_run(self, run_id: str) -> None:
        for operation_id, waiter in list(self.waiters.items()):
            operation = self.repository.get(operation_id)
            if operation["run_id"] != run_id:
                continue
            if operation["status"] in ACTIVE_TOOL_STATES:
                self.repository.transition(
                    operation_id, {operation["status"]}, "cancelled",
                    error_code="cancelled_by_user",
                )
            if not waiter.done():
                waiter.cancel()
        self.web_result_characters.pop(run_id, None)

    def interrupt_run(self, run_id: str) -> None:
        """Release unfinished feedback without asserting effects were rolled back."""
        for operation in self.repository.list_for_run(run_id):
            if operation["status"] in ACTIVE_TOOL_STATES:
                self.repository.transition(
                    operation["id"], {operation["status"]}, "interrupted",
                    error_code="tool_execution_interrupted",
                )
            waiter = self.waiters.pop(operation["id"], None)
            if waiter is not None and not waiter.done():
                waiter.cancel()

    def shutdown(self) -> None:
        for waiter in self.waiters.values():
            if not waiter.done():
                waiter.cancel()
        self.waiters.clear()
        self.web_result_characters.clear()
        self.repository.interrupt_active()

    def budget_snapshot(
        self, run_id: str, attempt_id: str, budget_attempt_ids: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        """Live accounting shared by admission and model-visible observations."""
        operations = self.repository.list_for_run(run_id)
        charged_ids = {attempt_id, *budget_attempt_ids}
        current = [item for item in operations if item["attempt_id"] in charged_ids]
        mutations = sum(
            len(item["arguments"].get("mutations", [])) for item in operations
            if item["tool_name"] == "fs.propose_changes" and item["status"] != "rejected"
            and not (item["status"] == "failed" and item["error_code"] == "missing_parent_dependency")
        )
        def quota(limit: int, used: int, scope: str) -> dict[str, Any]:
            return {"limit": limit, "used": used, "remaining": max(0, limit-used), "scope": scope}
        mutations += sum(item["tool_name"] in {"skills.save", "document.fill_template", "document.export"} for item in operations if item["status"] != "rejected")
        return {
            "calls": quota(self.MAX_ROUNDS, len(current), "attempt"),
            "reads": quota(self.MAX_READ_OPERATIONS, sum(item["tool_name"] in {"fs.list", "fs.read", "fs.search", "skills.load", "skills.read_resource", "rag.search", "rag.read", "document.inspect_template"} for item in current), "attempt"),
            "mutations": quota(self.MAX_FILE_MUTATIONS, mutations, "run"),
            "web_searches": quota(self.MAX_WEB_SEARCHES, sum(item["tool_name"] == "web.search" for item in operations), "run"),
            "web_opens": quota(self.MAX_WEB_OPENS, sum(item["tool_name"] == "web.open" for item in operations), "run"),
            "web_characters": quota(self.MAX_WEB_RESULT_CHARACTERS, self.web_result_characters.get(run_id, 0), "run"),
            "max_result_bytes": self.MAX_RESULT_BYTES,
        }

    def _enforce_limits(
        self, run_id: str, attempt_id: str, call: ToolCall,
        budget_attempt_ids: tuple[str, ...] = (),
    ) -> None:
        budget = self.budget_snapshot(run_id, attempt_id, budget_attempt_ids)
        if not budget["calls"]["remaining"]:
            raise CoreDataError(
                "tool_call_limit_exceeded",
                f"This attempt reached its {self.MAX_ROUNDS}-call tool limit. Retry starts a new allowance.",
                {key: budget["calls"][key] for key in ("limit", "used", "scope")},
            )
        if call.name in {"fs.list", "fs.search", "fs.read", "skills.load", "skills.read_resource", "rag.search", "rag.read", "document.inspect_template"} and not budget["reads"]["remaining"]:
            raise CoreDataError(
                "tool_read_limit_exceeded",
                f"This attempt reached its {self.MAX_READ_OPERATIONS}-operation read/search limit.",
                {key: budget["reads"][key] for key in ("limit", "used", "scope")},
            )
        if call.name in {"skills.save", "document.fill_template", "document.export"} and not budget["mutations"]["remaining"]:
            raise CoreDataError("tool_mutation_limit_exceeded", "The run has no remaining mutation allowance; explain this and continue answering.")
        if call.name == "fs.propose_changes":
            # A rejected proposal never executes. Host missing-parent failures
            # occur before writes or escape execution only after verified rollback.
            # Other failed/interrupted outcomes may have effects and remain charged.
            mutations = budget["mutations"]["used"]
            requested = len(call.arguments.get("mutations", []))
            rendered, _ = canonical_arguments(call.arguments)
            if len(rendered.encode("ascii")) > self.MAX_RESULT_BYTES:
                raise CoreDataError(
                    "tool_data_limit_exceeded", f"A change set exceeds the {self.MAX_RESULT_BYTES} byte content budget."
                )
            if mutations + requested > self.MAX_FILE_MUTATIONS:
                raise CoreDataError(
                    "tool_mutation_limit_exceeded",
                    f"The run exceeded its file mutation limit ({self.MAX_FILE_MUTATIONS}).",
                    {"limit": self.MAX_FILE_MUTATIONS, "used": mutations,
                     "requested": requested, "scope": "run"},
                )
        if call.name == "web.search" and not budget["web_searches"]["remaining"]:
            raise CoreDataError("search_budget_exceeded", "The run exceeded its web-search limit.")
        if call.name == "web.open" and not budget["web_opens"]["remaining"]:
            raise CoreDataError("search_budget_exceeded", "The run exceeded its web-source limit.")
