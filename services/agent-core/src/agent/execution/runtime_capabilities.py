"""Live, observational model context, separate from execution authority."""
from __future__ import annotations

import asyncio
import json
import uuid
from hashlib import sha256
from typing import Any, Awaitable, Callable

from agent.storage import CoreDataError, _utc_now
from agent.tool_contracts import TOOL_NAMES, ToolContractRegistry


MARKER = "<runtime-capabilities>\n"
STATUSES = {"available", "unavailable", "unknown", "requires_approval"}
REASONS = {"workspace_not_authorized", "workspace_suspended", "executor_unavailable",
           "powershell_unavailable", "host_unavailable", "not_project_work", "approval_required"}


def choice(value: Any, values: set[str], default: str = "unknown") -> str:
    return value if isinstance(value, str) and value in values else default


def clean_report(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1 \
            or len(json.dumps(value, ensure_ascii=True)) > 65536:
        raise CoreDataError("invalid_capability_report", "Host capability observation is invalid.")
    result: dict[str, Any] = {"version": 1, "status": "observed", "tools": {}}
    workspace = value.get("workspace", {})
    if isinstance(workspace, dict):
        result["workspace"] = {"status": choice(workspace.get("status"), STATUSES)}
        generation = workspace.get("root_generation")
        if type(generation) is int and generation >= 0:
            result["workspace"]["root_generation"] = generation
    mode = value.get("approval_mode")
    result["approval_mode"] = choice(mode, {"open", "human", "ai_review"})
    environment = value.get("environment", {})
    if isinstance(environment, dict):
        result["environment"] = {
            key: environment[key] for key in ("platform", "shell")
            if isinstance(environment.get(key), str) and environment[key] in {"windows", "unsupported", "powershell", "unknown"}
        }
        result["environment"].update({key: environment[key] for key in ("interactive_stdin", "detached_processes")
                                      if type(environment.get(key)) is bool})
    tools = value.get("tools", {})
    if isinstance(tools, dict):
        for name in sorted(TOOL_NAMES & tools.keys()):
            item = tools[name]
            if not isinstance(item, dict):
                continue
            observation = {"status": choice(item.get("status"), STATUSES)}
            if isinstance(item.get("reason_code"), str) and item["reason_code"] in REASONS:
                observation["reason_code"] = item["reason_code"]
            limits = item.get("limits", {})
            if isinstance(limits, dict):
                observation["limits"] = {key: val for key, val in limits.items()
                    if key in {"max_read_bytes", "max_change_bytes", "max_mutations", "max_timeout_ms", "max_output_bytes"}
                    and type(val) is int and val >= 0}
            result["tools"][name] = observation
    return result


class RuntimeCapabilityService:
    def __init__(self, emit: Callable[[str, str, dict[str, Any]], Awaitable[None]]) -> None:
        self.emit = emit
        self.host_reporting = False
        self.timeout_seconds = 3.0
        self.waiters: dict[str, tuple[str, asyncio.Future[dict[str, Any]]]] = {}

    def configure(self, *, host_reporting: bool) -> None:
        self.host_reporting = host_reporting

    async def observe(self, run_id: str, conversation_id: str, project_id: str | None) -> dict[str, Any]:
        if not self.host_reporting:
            return {"status": "unknown", "reason_code": "host_not_reporting"}
        request_id = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        self.waiters[request_id] = run_id, future
        try:
            async with asyncio.timeout(self.timeout_seconds):
                await self.emit(run_id, "run.capabilities_requested", {
                    "request_id": request_id, "conversation_id": conversation_id, "project_id": project_id,
                })
                return await future
        except Exception:
            # Observation failure cannot disable answering. Cancellation remains
            # a BaseException and propagates instead of starting a model call.
            return {"status": "unknown", "reason_code": "host_observation_unavailable"}
        finally:
            self.waiters.pop(request_id, None)
            if not future.done():
                future.cancel()

    def resolve(self, run_id: str, request_id: str, report: Any) -> bool:
        pending = self.waiters.get(request_id)
        if pending is None or pending[1].done():
            return False
        if pending[0] != run_id:
            raise CoreDataError("capability_scope_mismatch", "The observation belongs to another run.")
        pending[1].set_result(clean_report(report))
        return True


def render_snapshot(*, budgets: dict[str, Any], host: dict[str, Any], allowed_tools: tuple[str, ...],
                    signature: tuple[str, ...], max_output_tokens: int, web_used: bool,
                    evidence_status: str, response_only: bool, model_details: dict[str, Any] | None = None) -> str:
    tools = {}
    for definition in ToolContractRegistry.definitions(allowed_tools):
        name = definition["function"]["name"]
        observed = host.get("tools", {}).get(name, {})
        status = observed.get("status", "unknown") if name.startswith(("fs.", "terminal.")) else "available"
        reason = observed.get("reason_code")
        quota_names = ["calls"]
        if name in {"fs.list", "fs.read", "fs.search", "skills.load", "skills.read_resource", "rag.search", "rag.read"}: quota_names.append("reads")
        if name in {"fs.propose_changes", "skills.save"}: quota_names.append("mutations")
        if name == "web.search": quota_names.extend(("web_searches", "web_characters"))
        if name == "web.open": quota_names.extend(("web_opens", "web_characters"))
        exhausted = [key for key in quota_names if budgets[key]["remaining"] == 0]
        if exhausted:
            status, reason = "unavailable", "resource_exhausted"
        limits = ToolContractRegistry.execution_limits(name)
        for key, value in observed.get("limits", {}).items():
            limits[key] = min(limits[key], value) if key in limits else value
        tools[name] = {"status": status, "reason_code": reason, "resources": quota_names,
                       **({"limits": limits} if limits else {})}
    snapshot = {
        "version": 1, "tools": tools, "budgets": budgets, "host": host,
        "model": {"capability_signature": list(signature), "response_output_tokens": max_output_tokens, **(model_details or {})},
        "web": {"used": web_used, "evidence_status": evidence_status},
        "response_only": response_only, "tool_schema_hash": ToolContractRegistry.schema_hash(allowed_tools),
    }
    snapshot["revision"] = sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    snapshot["observed_at"] = _utc_now()
    return MARKER + json.dumps(snapshot, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + (
        "\n</runtime-capabilities>\n"
        "This is a current runtime observation, refreshed before each model invocation, not a permanent capability ban. "
        "Tool schemas describe request formats; this snapshot describes present readiness, approval and remaining resources. "
        "Use these facts to choose a workable method before calling a tool, budget batches across all calls, or give a useful "
        "answer explaining what remains unavailable. Unknown means unobserved, not absent. "
        "Do not claim an operation or verification happened without its result."
    )


def replace_snapshot(messages: list[dict[str, Any]], content: str) -> None:
    messages[:] = [m for m in messages if not (m.get("role") == "system"
                   and isinstance(m.get("content"), str) and m["content"].startswith(MARKER))]
    # Runtime metadata belongs with the leading instructions. Keep the user or
    # tool result at the conversation tail for providers that use that boundary.
    index = next((i for i, message in enumerate(messages) if message.get("role") != "system"), len(messages))
    messages.insert(index, {"role": "system", "content": content})
