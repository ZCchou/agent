from __future__ import annotations

from agent.execution.compiler import ADAPTER_DIALECTS, canonical_adapter_id
from agent.execution.domain import CapabilityRequirements, ExecutionPlan
from agent.execution.planner import PlanningError
from agent.storage import CoreDataError


class PlanValidator:
    """Reject an execution topology before a message or paid run is persisted."""

    def validate(
        self, plan: ExecutionPlan, requirements: CapabilityRequirements,
    ) -> None:
        final_steps = [step for step in plan.steps if step.step_type == "final_response"]
        if len(final_steps) != 1:
            raise PlanningError("execution_plan_invalid")

        seen_ids: set[str] = set()
        for step in plan.steps:
            if step.step_id in seen_ids:
                raise PlanningError("execution_plan_invalid")
            seen_ids.add(step.step_id)
            signature = set(step.capability_signature)
            if "text" not in signature:
                raise PlanningError("capability_combination_unsupported")
            try:
                adapter_id = canonical_adapter_id(
                    step.invocation_route_id, step.protocol_id, step.adapter_id,
                )
            except CoreDataError as error:
                raise PlanningError(error.code) from error
            dialect = ADAPTER_DIALECTS.get(adapter_id)
            if dialect is None or dialect.protocol_id != step.protocol_id:
                raise PlanningError("invocation_protocol_mismatch")
            if "native_web_search" in signature \
                    and dialect.native_search_transport == "none":
                raise PlanningError("native_search_continuation_unsupported")
            if step.step_type == "vision_analysis" and "vision" not in signature:
                raise PlanningError("capability_combination_unsupported")
            if step.step_type == "web_research" and "native_web_search" not in signature:
                raise PlanningError("capability_combination_unsupported")
            if step.phase == "specialist" and "function_tools" in signature:
                raise PlanningError("capability_combination_unsupported")

        final_signature = set(final_steps[0].capability_signature)
        all_signatures = [set(step.capability_signature) for step in plan.steps]
        if requirements.vision == "required" \
                and not any("vision" in signature for signature in all_signatures):
            raise PlanningError("no_compatible_execution_plan")
        if requirements.web == "required":
            has_search = plan.web_route in {"host_tool", "host_presearch"} or any(
                "native_web_search" in signature for signature in all_signatures
            )
            if not has_search:
                raise PlanningError("no_compatible_execution_plan")
            if requirements.web_evidence == "required" and plan.web_route == "native":
                raise PlanningError("web_source_route_unavailable")
        if requirements.files in {"read", "write"} \
                and "function_tools" not in final_signature:
            raise PlanningError("no_compatible_execution_plan")
        if plan.web_route == "native" and "native_web_search" not in final_signature:
            raise PlanningError("invocation_protocol_mismatch")
        if plan.web_route == "host_tool" and "function_tools" not in final_signature:
            raise PlanningError("capability_combination_unsupported")
