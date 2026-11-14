from __future__ import annotations

import json
import re
from typing import Any

from agent.execution.domain import ExecutionPlan
from agent.execution.planner import ExecutionPlanner, PlannedWebRoute, PlanningRequest


class ExecutionCoordinator:
    """Boundary for deterministic planning and persisted plan ownership."""

    def __init__(self, repository: Any, planner: ExecutionPlanner | None = None) -> None:
        self.repository = repository
        self.planner = planner or ExecutionPlanner()

    def plan(self, request: PlanningRequest) -> ExecutionPlan:
        return self.planner.plan(request)

    def resolve_web_route(self, **values: Any) -> PlannedWebRoute:
        return self.planner.resolve_web_route(**values)

    def persist(
        self, run_id: str, plan: ExecutionPlan, requirements: dict[str, Any],
        resolved_intent: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        persisted_requirements = dict(requirements)
        if plan.degraded_capabilities:
            persisted_requirements["capability_advisories"] = [
                advisory.to_dict() for advisory in plan.degraded_capabilities
            ]
        return self.repository.save_plan(
            run_id, plan, persisted_requirements, resolved_intent,
        )

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        return self.repository.get_execution_plan(plan_id)

    def get_for_run(self, run_id: str) -> dict[str, Any] | None:
        return self.repository.get_for_run(run_id)

    @staticmethod
    def parse_routing_intent(
        content: str, *, pending_action: bool = False,
    ) -> dict[str, Any]:
        """Validate the advisor's bounded contract without accepting prose or model IDs."""
        raw = content.strip()
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            # OpenAI-compatible providers do not all honor JSON mode. Recover
            # the first object from a prose/code-fence wrapper, then apply the
            # same strict field and authority validation below.
            value = None
            decoder = json.JSONDecoder()
            for index, character in enumerate(raw):
                if character != "{":
                    continue
                try:
                    candidate, _ = decoder.raw_decode(raw[index:])
                except json.JSONDecodeError:
                    continue
                value = candidate
                break
            if value is None:
                raise
        if not isinstance(value, dict):
            raise ValueError("routing intent must be an object")
        allowed_fields = {
            "files", "tools", "web", "web_evidence", "reasoning",
            "task_kinds", "explanation_code", "follow_up", "rag_requirement",
            "read_attachment_ids", "read_requests", "task_scope", "retrieval_scope", "allow_simulated_data", "clarification",
            "document_workflow",
        }
        if set(value) - allowed_fields:
            raise ValueError("routing intent contains unsupported fields")
        files = value.get("files", "available")
        tools = value.get("tools", "available")
        web_evidence = value.get("web_evidence", "optional")
        web = value.get("web")
        # Accept the provider-independent v1 vocabulary while retaining the
        # legacy spellings emitted by older intent-analysis sidecars.
        if web == "off":
            web = "not_needed"
        reasoning = value.get("reasoning")
        if reasoning in {"preferred", "required"}:
            reasoning = "high"
        task_kinds = value.get("task_kinds")
        explanation = value.get("explanation_code")
        follow_up = value.get("follow_up", "none")
        allowed_kinds = {"vision", "research", "file_work", "reasoning", "general", "airworthiness", "document_export", "attachment_summary", "attachment_extract"}
        rag_requirement = value.get("rag_requirement", "uncertain")
        if not isinstance(rag_requirement, str) or rag_requirement not in {"required", "not_needed", "uncertain"}:
            raise ValueError("routing intent evidence requirement is invalid")
        if files not in {"none", "read", "write", "available"} \
                or tools not in {"none", "preferred", "required", "available"} \
                or web_evidence not in {"optional", "required"} \
                or web not in {"not_needed", "optional", "helpful", "required"} \
                or reasoning not in {"standard", "high"} \
                or not isinstance(task_kinds, list) \
                or any(item not in allowed_kinds for item in task_kinds) \
                or not isinstance(explanation, str) \
                or not re.fullmatch(r"[a-z0-9_.-]{1,80}", explanation):
            raise ValueError("routing intent fields are invalid")
        if follow_up not in {"none", "confirm", "reject"}:
            raise ValueError("routing intent follow-up is invalid")
        read_ids = value.get('read_attachment_ids', [])
        read_requests = value.get('read_requests', [])
        from agent.document_contracts import validate_document_call
        if not isinstance(read_requests, list) or len(read_requests) > 8:
            raise ValueError('routing intent attachment read requests are invalid')
        try:
            for request in read_requests:
                if not isinstance(request, dict):
                    raise ValueError('invalid read request')
                validate_document_call('document.read_attachment', request)
        except Exception as error:
            raise ValueError('routing intent attachment read requests are invalid') from error
        scope = value.get('task_scope', 'unspecified')
        retrieval_scope = value.get('retrieval_scope', 'broad')
        simulated = value.get('allow_simulated_data', False)
        document_workflow = value.get('document_workflow', 'none')
        if (not isinstance(read_ids, list) or len(read_ids) > 8
                or any(not isinstance(identity, str) or not 1 <= len(identity) <= 200 for identity in read_ids)
                or len(set(read_ids)) != len(read_ids)
                or scope not in {'unspecified', 'full', 'section'}
                or retrieval_scope not in {'focused', 'broad'} or type(simulated) is not bool
                or document_workflow not in {'none', 'review_nonempty'}):
            raise ValueError('routing intent attachment plan is invalid')
        clarification = value.get('clarification')
        if clarification is not None:
            from agent.execution.intent_session import IntentClarification
            try:
                if not isinstance(clarification, dict) or set(clarification) != {'summary', 'questions'}:
                    raise ValueError('invalid clarification')
                clarification = IntentClarification(clarification['summary'], clarification['questions']).value
            except Exception as error:
                raise ValueError('routing intent clarification is invalid') from error
        return {
            "files": files,
            "tools": tools,
            "web": web,
            "web_evidence": web_evidence,
            "reasoning": reasoning,
            "task_kinds": list(dict.fromkeys(task_kinds)),
            "explanation_code": explanation,
            "follow_up": follow_up,
            "rag_requirement": rag_requirement,
            'read_attachment_ids': list(dict.fromkeys([*read_ids, *(item['attachment_id'] for item in read_requests)])),
            'read_requests': read_requests, 'task_scope': scope, 'retrieval_scope': retrieval_scope,
            'allow_simulated_data': simulated, 'clarification': clarification,
            'document_workflow': document_workflow,
        }

    @staticmethod
    def constrain_follow_up(
        intent: dict[str, Any], *, pending_action: bool,
    ) -> dict[str, Any]:
        """Keep advisor output descriptive unless runtime has a pending action."""
        if pending_action or intent.get("follow_up", "none") == "none":
            return intent
        constrained = dict(intent)
        constrained["follow_up"] = "none"
        constrained["advisor_ignored_without_pending_action"] = True
        return constrained

    @staticmethod
    def fallback_routing_intent(
        content: str, attachment_names: list[str],
    ) -> dict[str, Any]:
        """Conservative deterministic fallback when the optional advisor is invalid."""
        normalized = content.casefold()
        web_markers = (
            "最新", "近期", "最近", "今天", "现在", "当前", "本周", "本月", "今年",
            "近几天", "近一周", "近两周", "最近两周", "过去两周", "截至", "联网", "搜索", "查找", "新闻",
            "latest", "recent", "today", "current", "last week", "last two weeks", "past two weeks",
            "search", "news", "online",
        )
        reasoning_markers = (
            "深入", "严谨", "证明", "权衡", "复杂", "推导",
            "deep", "rigorous", "prove", "trade-off", "complex", "derive",
        )
        needs_web = any(marker in normalized for marker in web_markers)
        needs_reasoning = any(marker in normalized for marker in reasoning_markers)
        kinds = ["general"]
        if attachment_names:
            kinds.insert(0, "vision")
        if needs_web:
            kinds.insert(0, "research")
        if needs_reasoning:
            kinds.insert(0, "reasoning")
        return {
            "web": "helpful" if needs_web else "not_needed",
            "reasoning": "high" if needs_reasoning else "standard",
            "task_kinds": kinds,
            "explanation_code": "deterministic_conservative_fallback",
            "follow_up": "none",
        }
