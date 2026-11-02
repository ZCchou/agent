from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from decimal import Decimal

from agent.execution.compatibility import compatible_candidates
from agent.execution.domain import (
    CapabilityAdvisory, CapabilityRequirements, ExecutionBudget, ExecutionPlan, ExecutionStepSpec,
    ModelCandidate, RoutingPreset, stable_hash,
)
from agent.execution.selection import select_candidate
from agent.execution.output_budget import resolve_output_budget


def _route_failure_code(rejected: dict[tuple[str, ...], str], fallback: str) -> str:
    reasons = set(rejected.values())
    if reasons and reasons <= {"route_temporarily_cooling_down"}:
        return "route_temporarily_cooling_down"
    if reasons and reasons <= {"route_configuration_blocked"}:
        return "route_configuration_blocked"
    if reasons and reasons <= {
        "route_temporarily_cooling_down", "route_configuration_blocked",
    }:
        return (
            "route_temporarily_cooling_down"
            if "route_temporarily_cooling_down" in reasons
            else "route_configuration_blocked"
        )
    return fallback


def _prefer_available_tools(
    candidates: list[ModelCandidate], requirements: CapabilityRequirements, *, automatic: bool,
) -> list[ModelCandidate]:
    if requirements.tools != "available":
        return candidates
    usable, _ = compatible_candidates(
        candidates, replace(requirements, tools="required"), automatic=automatic,
    )
    # Available means expose a usable tool route when one exists, without
    # making a text-only model incapable of answering a project question.
    return usable or candidates

POLICY_VERSION = "web-intent-evidence-policy-v1"


@dataclass(frozen=True)
class PlanningRequest:
    selection_mode: str
    routing_preset: RoutingPreset
    requirements: CapabilityRequirements
    candidates: tuple[ModelCandidate, ...]
    budget: ExecutionBudget
    fixed_key: tuple[str, str] | None = None
    coordinator_key: tuple[str, str] | None = None
    global_default_key: tuple[str, str] | None = None
    web_route: str = "none"
    web_adapter_or_backend_id: str | None = None
    web_backend_kind: str | None = None
    intent_analysis_needed: bool = False
    degraded_capabilities: tuple[CapabilityAdvisory, ...] = ()


class PlanningError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PlannedWebRoute:
    route: str
    adapter_or_backend_id: str | None
    backend_kind: str | None
    reason_code: str


class ExecutionPlanner:
    """Pure deterministic planner. It never calls a provider or reads persistence."""

    def resolve_web_route(
        self, *, requirements: CapabilityRequirements,
        candidates: tuple[ModelCandidate, ...], coordinator_key: tuple[str, str],
        selection_mode: str, host_backend_id: str | None,
        host_backend_kind: str | None, request_mode: str,
    ) -> PlannedWebRoute:
        if requirements.web == "off":
            return PlannedWebRoute("none", None, None, "web_not_required_for_request")

        coordinator_candidates = [
            item for item in candidates if item.key == coordinator_key
        ]
        native_requirements = CapabilityRequirements(
            web="required",
            web_evidence=requirements.web_evidence,
            reasoning=requirements.reasoning,
            minimum_context_tokens=requirements.minimum_context_tokens,
        )
        native_same_model, _ = compatible_candidates(
            coordinator_candidates, native_requirements,
            automatic=selection_mode == "automatic",
        )
        # Source provenance gates grounded claims, not whether a search may run.
        # Keep structured routes preferred, but retain opaque native routes as a
        # deterministic last resort when no structured route is available.
        opaque_native_requirements = replace(native_requirements, web_evidence="optional")
        native_same_model_any, _ = compatible_candidates(
            coordinator_candidates, opaque_native_requirements,
            automatic=selection_mode == "automatic",
        )
        available_tool_routes, _ = compatible_candidates(
            coordinator_candidates, replace(requirements, web="off", tools="required"),
            automatic=selection_mode == "automatic",
        )
        needs_separate_final = (
            requirements.vision == "required"
            or requirements.tools == "required"
            or requirements.files in {"read", "write"}
            or requirements.tools == "available" and bool(available_tool_routes)
        )
        if native_same_model and not needs_separate_final \
                and requirements.web != "optional" \
                and requirements.web_evidence != "required":
            target = native_same_model[0].invocation_target
            return PlannedWebRoute(
                "native", target.adapter_id if target is not None else None, None,
                "same_model_native_search",
            )

        if native_same_model and requirements.web_evidence == "required":
            return PlannedWebRoute(
                "specialist", None, None, "same_model_grounded_search_specialist",
            )

        tool_final = any(
            item.invocation_target is not None
            and "function_tools" in item.invocation_target.capability_signature
            for item in coordinator_candidates
        )
        if host_backend_id and requirements.web_evidence == "required":
            return PlannedWebRoute(
                "host_presearch", host_backend_id, host_backend_kind,
                "grounded_host_presearch",
            )
        if selection_mode == "automatic" and requirements.web_evidence == "required":
            structured_any, _ = compatible_candidates(
                list(candidates), native_requirements, automatic=True,
            )
            if structured_any:
                return PlannedWebRoute(
                    "specialist", None, None, "authorized_native_search_specialist",
                )
        # Opaque search is a user-authorized fixed-route fallback only. An
        # automatic request that promises structured evidence must fail
        # explicitly instead of silently producing unverifiable sources.
        if selection_mode != "automatic" and requirements.web_evidence == "required" \
                and native_same_model_any:
            target = native_same_model_any[0].invocation_target
            return PlannedWebRoute(
                "specialist", target.adapter_id if target is not None else None, None,
                "same_model_opaque_search_fallback",
            )
        if host_backend_id and tool_final:
            return PlannedWebRoute(
                "host_tool", host_backend_id, host_backend_kind,
                "host_search_with_coordinator_tools",
            )
        if host_backend_id and request_mode == "force_once" and not tool_final:
            return PlannedWebRoute(
                "host_presearch", host_backend_id, host_backend_kind,
                "user_forced_host_presearch",
            )
        if native_same_model:
            return PlannedWebRoute(
                "specialist", None, None, "same_model_native_search_specialist",
            )

        if selection_mode == "automatic":
            native_any, _ = compatible_candidates(
                list(candidates), native_requirements, automatic=True,
            )
            if native_any:
                return PlannedWebRoute(
                    "specialist", None, None, "authorized_native_search_specialist",
                )
        raise PlanningError(
            "structured_search_route_unavailable"
            if requirements.web_evidence == "required"
            else "no_compatible_execution_plan"
        )

    def plan(self, request: PlanningRequest) -> ExecutionPlan:
        automatic = request.selection_mode == "automatic"
        candidates = list(request.candidates)
        coordinator_requirements = request.requirements
        if request.web_route in {"host_tool", "host_presearch", "specialist"}:
            coordinator_requirements = replace(request.requirements, web="off")
        if automatic and request.routing_preset == "economy":
            candidates = [
                item for item in candidates
                if item.location == "local" or (
                    item.estimated_input_cost_per_million is not None
                    and item.estimated_output_cost_per_million is not None
                )
            ]
        if not automatic:
            candidates = [item for item in candidates if item.key == request.fixed_key]
        accepted, coordinator_rejected = compatible_candidates(
            candidates, coordinator_requirements, automatic=automatic,
        )
        accepted = _prefer_available_tools(
            accepted, coordinator_requirements, automatic=automatic,
        )
        primary_accepted = [
            item for item in accepted
            if item.invocation_target is None
            or item.invocation_target.route_role == "primary"
        ]
        if primary_accepted:
            accepted = primary_accepted
        coordinator = None
        if request.coordinator_key is not None:
            bound_candidates = [
                item for item in candidates if item.key == request.coordinator_key
            ]
            bound = next((item for item in accepted if item.key == request.coordinator_key), None)
            if bound is None:
                base_requirements = CapabilityRequirements(
                    files=request.requirements.files, tools=request.requirements.tools,
                    minimum_context_tokens=request.requirements.minimum_context_tokens,
                )
                bound_choices, _ = compatible_candidates(
                    bound_candidates, base_requirements, automatic=True,
                )
                bound_choices = _prefer_available_tools(
                    bound_choices, base_requirements, automatic=True,
                )
                bound_primary = [item for item in bound_choices if item.invocation_target is None
                                 or item.invocation_target.route_role == "primary"]
                if bound_primary:
                    bound_choices = bound_primary
                if bound_choices:
                    bound = select_candidate(bound_choices, preset=request.routing_preset)
            if bound is None:
                raise PlanningError("coordinator_model_unavailable")
            base_requirements = CapabilityRequirements(
                files=request.requirements.files, tools=request.requirements.tools,
                minimum_context_tokens=request.requirements.minimum_context_tokens,
            )
            if compatible_candidates([bound], base_requirements, automatic=True)[0]:
                coordinator = bound
            elif automatic:
                raise PlanningError("coordinator_model_unavailable")
        if coordinator is None and accepted:
            coordinator = select_candidate(
                accepted, preset=request.routing_preset,
                coordinator_key=request.coordinator_key,
                preferred_key=request.fixed_key or request.global_default_key,
            )
        if coordinator is None:
            base_requirements = CapabilityRequirements(
                files=request.requirements.files, tools=request.requirements.tools,
                minimum_context_tokens=request.requirements.minimum_context_tokens,
            )
            base_candidates, base_rejected = compatible_candidates(
                candidates, base_requirements, automatic=automatic,
            )
            base_candidates = _prefer_available_tools(
                base_candidates, base_requirements, automatic=automatic,
            )
            if not base_candidates:
                raise PlanningError(_route_failure_code(
                    base_rejected or coordinator_rejected, "no_compatible_execution_plan",
                ))
            coordinator = select_candidate(
                base_candidates, preset=request.routing_preset,
                preferred_key=request.global_default_key,
            )
        final_candidate = coordinator
        if request.web_route == "native":
            # Vision may be handled by an immutable specialist step, but the
            # synthesis target itself must be the coordinator model's exact
            # native-search route. A primary text route must never be upgraded
            # to search later by GenerationOptions.
            # Native search is an exact route requirement. File tools and vision
            # belong to their own steps and must not be inferred on this route.
            native_requirements = CapabilityRequirements(
                web="required",
                web_evidence=request.requirements.web_evidence,
                reasoning=request.requirements.reasoning,
                minimum_context_tokens=request.requirements.minimum_context_tokens,
            )
            native_choices, _ = compatible_candidates(
                [item for item in candidates if item.key == coordinator.key],
                native_requirements,
                automatic=automatic,
            )
            if not native_choices:
                raise PlanningError("capability_combination_unsupported")
            final_candidate = select_candidate(
                native_choices, preset=request.routing_preset,
                coordinator_key=coordinator.key,
            )
        steps: list[ExecutionStepSpec] = []
        specialist_cost = Decimal("0")
        unknown_specialist_price = False
        degraded_route_selected = (
            final_candidate.invocation_target is not None
            and final_candidate.invocation_target.health_status in {"degraded", "cooling_down"}
        )

        def signature_for(candidate: ModelCandidate) -> tuple[str, ...]:
            target = candidate.invocation_target
            if target is not None:
                return target.capability_signature
            signature = {"text"}
            if candidate.capabilities.get("vision") == "yes":
                signature.add("vision")
            if candidate.tool_mode not in {"text_only", "unsupported"}:
                signature.add("function_tools")
            if candidate.native_search_status in {"declared", "verified"}:
                signature.add("native_web_search")
            if candidate.capabilities.get("reasoning") == "yes":
                signature.add("reasoning")
            return tuple(sorted(signature))

        def step_values(candidate: ModelCandidate) -> dict[str, object]:
            target = candidate.invocation_target
            if target is None:
                return {
                    "invocation_route_id": "legacy.primary",
                    "adapter_id": candidate.protocol_id,
                    "capability_signature": signature_for(candidate),
                    "route_config_fingerprint": "legacy",
                }
            return {
                "invocation_route_id": target.route_id,
                "adapter_id": target.adapter_id,
                "capability_signature": target.capability_signature,
                "route_config_fingerprint": target.route_config_fingerprint,
            }

        def account_model_call(candidate: ModelCandidate, input_tokens: int, output_tokens: int) -> None:
            nonlocal specialist_cost, unknown_specialist_price
            if candidate.location != "online":
                return
            if candidate.estimated_input_cost_per_million is None \
                    or candidate.estimated_output_cost_per_million is None:
                unknown_specialist_price = True
                return
            specialist_cost += (
                candidate.estimated_input_cost_per_million * Decimal(input_tokens)
                + candidate.estimated_output_cost_per_million * Decimal(output_tokens)
            ) / Decimal(1_000_000)

        def add_specialist(
            step_type: str, requirements: CapabilityRequirements, *, required: bool = True,
            allow_same_model_route: bool = False,
            allow_same_target: bool = False,
        ) -> None:
            nonlocal degraded_route_selected
            choices, rejected_choices = compatible_candidates(
                candidates, requirements, automatic=automatic,
            )
            choices = [
                item for item in choices
                if (allow_same_target or item.target_key != final_candidate.target_key)
                and (allow_same_model_route or item.key != coordinator.key)
            ]
            # Specialist calls are read-only and receive no tool definitions.
            # A model route can expose several independently tested signatures;
            # select the least-privileged signature that satisfies this step
            # instead of accidentally choosing a wider vision+tools variant.
            tool_free = [
                item for item in choices
                if "function_tools" not in signature_for(item)
            ]
            if tool_free:
                choices = tool_free
            required_signature = {"text"}
            if requirements.vision == "required":
                required_signature.add("vision")
            if requirements.web == "required":
                required_signature.add("native_web_search")
            if requirements.reasoning == "required":
                required_signature.add("reasoning")
            if choices:
                minimum_excess = min(
                    len(set(signature_for(item)) - required_signature)
                    for item in choices
                )
                choices = [
                    item for item in choices
                    if len(set(signature_for(item)) - required_signature) == minimum_excess
                ]
            if not choices:
                raise PlanningError(_route_failure_code(
                    rejected_choices, "no_compatible_execution_plan",
                ))
            # A specialist route on the already-bound coordinator model is
            # preferred over an equally capable route on another model.  This
            # is part of the topology contract (same-model official routes
            # before cross-model delegation), not a provider-specific rule.
            chosen = select_candidate(
                choices, preset=request.routing_preset,
                coordinator_key=coordinator.key,
            )
            degraded_route_selected |= (
                chosen.invocation_target is not None
                and chosen.invocation_target.health_status in {"degraded", "cooling_down"}
            )
            steps.append(ExecutionStepSpec.create(
                phase="specialist", step_type=step_type,
                provider_profile_id=chosen.provider_profile_id, model_id=chosen.model_id,
                protocol_id=chosen.protocol_id, order_index=len(steps), required=required,
                activation_condition=(
                    "when_web_required"
                    if step_type == "web_research" and not required else "always"
                ),
                **step_values(chosen),
            ))
            specialist_budget = resolve_output_budget(
                step_type,  # type: ignore[arg-type]
                model_limit=chosen.capabilities.get("max_output_tokens"),
            )
            account_model_call(
                chosen, 2048, specialist_budget.maximum_billed_output_tokens,
            )

        needs_intent = request.intent_analysis_needed or (
            request.web_route == "specialist" and request.requirements.web == "optional"
        )
        if automatic and needs_intent:
            steps.append(ExecutionStepSpec.create(
                phase="analysis", step_type="intent_analysis",
                provider_profile_id=coordinator.provider_profile_id,
                model_id=coordinator.model_id, protocol_id=coordinator.protocol_id,
                order_index=len(steps),
                **step_values(coordinator),
            ))
            account_model_call(coordinator, 2048, 256)

        final_signature = set(signature_for(final_candidate))
        vision_must_precede_search = (
            request.requirements.vision == "required"
            and request.web_route == "specialist"
        )
        if request.requirements.vision == "required" \
                and ("vision" not in final_signature or vision_must_precede_search):
            same_model_vision = any(
                item.key == coordinator.key
                and item.invocation_target is not None
                and "vision" in item.invocation_target.capability_signature
                for item in candidates
            )
            if automatic or same_model_vision:
                add_specialist(
                    "vision_analysis", CapabilityRequirements(vision="required"),
                    allow_same_model_route=True,
                    allow_same_target=vision_must_precede_search,
                )
            else:
                raise PlanningError("no_compatible_execution_plan")
        if request.requirements.reasoning in {"preferred", "required"} \
                and coordinator.capabilities.get("reasoning") != "yes":
            if automatic:
                add_specialist("reasoning_analysis", CapabilityRequirements(reasoning="required"))
            elif request.requirements.reasoning == "required":
                raise PlanningError("no_compatible_execution_plan")
        if request.web_route == "specialist":
            web_candidates = [
                item for item in candidates
                if item.native_search_status in {"declared", "verified"}
            ]
            original_candidates = candidates
            candidates = web_candidates
            try:
                web_requirements = CapabilityRequirements(
                    web="required", web_evidence=request.requirements.web_evidence,
                )
                structured_web, _ = compatible_candidates(
                    web_candidates, web_requirements, automatic=automatic,
                )
                if not structured_web and request.requirements.web_evidence == "required":
                    if automatic:
                        raise PlanningError("structured_search_route_unavailable")
                    # Explicitly fixed routes may still use opaque search, but
                    # the route remains visibly unstructured to the caller.
                    web_requirements = replace(web_requirements, web_evidence="optional")
                else:
                    # When evidence is required, never let an opaque coordinator
                    # route win merely because it is the coordinator. Structured
                    # source routes are the preferred candidates for this step.
                    web_candidates = structured_web
                    candidates = web_candidates
                add_specialist(
                    "web_research", web_requirements,
                    required=request.requirements.web == "required",
                    allow_same_model_route=True,
                    allow_same_target=not bool(structured_web),
                )
            finally:
                candidates = original_candidates
        if automatic and request.intent_analysis_needed \
                and request.requirements.reasoning == "standard" \
                and coordinator.capabilities.get("reasoning") != "yes" \
                and sum(step.phase == "specialist" for step in steps) < 2:
            reasoning_choices, _ = compatible_candidates(
                candidates, CapabilityRequirements(reasoning="required"), automatic=True,
            )
            if any(item.key != coordinator.key for item in reasoning_choices):
                add_specialist(
                    "reasoning_analysis", CapabilityRequirements(reasoning="required"),
                    required=False,
                )
        if sum(step.phase == "specialist" for step in steps) > 2:
            raise PlanningError("no_compatible_execution_plan")
        specialist_count = sum(step.phase == "specialist" for step in steps)
        steps.append(ExecutionStepSpec.create(
            phase="synthesis", step_type="final_response",
            provider_profile_id=final_candidate.provider_profile_id,
            model_id=final_candidate.model_id, protocol_id=final_candidate.protocol_id,
            order_index=len(steps),
            **step_values(final_candidate),
        ))
        if request.requirements.web_evidence == "required" \
                and request.web_route in {"specialist", "host_presearch"}:
            steps.append(ExecutionStepSpec.create(
                phase="synthesis", step_type="grounding_fallback",
                provider_profile_id=final_candidate.provider_profile_id,
                model_id=final_candidate.model_id,
                protocol_id=final_candidate.protocol_id,
                order_index=len(steps), required=False,
                activation_condition="when_web_evidence_missing",
                **step_values(final_candidate),
            ))
        if len(steps) > 6:
            raise PlanningError("no_compatible_execution_plan")
        source = "router" if automatic else "conversation_override"
        reason = "coordinator_reused" if request.coordinator_key == coordinator.key else (
            "automatic_single_model" if automatic else "fixed_selection"
        )
        if degraded_route_selected:
            reason = "degraded_route_last_resort"
        reasoning_effort = (
            "high" if request.requirements.reasoning == "required" else
            "medium" if request.requirements.reasoning == "preferred" else "none"
        )
        return ExecutionPlan(
            plan_id=str(uuid.uuid4()),
            coordinator_provider_profile_id=coordinator.provider_profile_id,
            coordinator_model_id=coordinator.model_id,
            generation_protocol=final_candidate.protocol_id,
            selection_mode="automatic" if automatic else "fixed",
            routing_preset=request.routing_preset,
            selection_source=source,
            selection_reason_code=reason,
            web_route=request.web_route,
            web_adapter_or_backend_id=request.web_adapter_or_backend_id,
            web_backend_kind=request.web_backend_kind,  # type: ignore[arg-type]
            reasoning_effort=reasoning_effort,
            requirements_hash=request.requirements.hash,
            policy_version=POLICY_VERSION,
            steps=tuple(steps), budget=request.budget,
            estimated_extra_online_cost_usd=None if unknown_specialist_price else specialist_cost,
            requires_budget_confirmation=(
                (unknown_specialist_price and not request.budget.allow_unknown_price_automatic)
                or specialist_cost > request.budget.max_extra_online_cost_usd
                or specialist_count > request.budget.max_specialist_steps
                or (
                    len(steps) - sum(
                        step.step_type == "grounding_fallback" for step in steps
                    )
                ) > request.budget.max_model_calls
            ),
            route_catalog_hash=stable_hash([
                candidate.target_key for candidate in candidates
            ]),
            degraded_capabilities=request.degraded_capabilities,
        )
