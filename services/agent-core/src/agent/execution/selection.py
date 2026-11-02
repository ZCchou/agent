from __future__ import annotations

from agent.execution.domain import ModelCandidate, RoutingPreset


def select_candidate(
    candidates: list[ModelCandidate], *, preset: RoutingPreset,
    coordinator_key: tuple[str, str] | None = None,
    preferred_key: tuple[str, str] | None = None,
) -> ModelCandidate:
    if not candidates:
        raise ValueError("no_compatible_execution_plan")

    def key(candidate: ModelCandidate) -> tuple[object, ...]:
        bound = candidate.key == coordinator_key
        preferred = candidate.key == preferred_key
        verified = any(
            value == "verified" for value in (
                candidate.capabilities.get("vision_status"), candidate.native_search_status,
                candidate.invocation_target.capability_status
                if candidate.invocation_target is not None else None,
            )
        )
        local = candidate.location == "local"
        price_known = candidate.estimated_input_cost_per_million is not None \
            and candidate.estimated_output_cost_per_million is not None
        estimated_price = (
            (candidate.estimated_input_cost_per_million or 0)
            + (candidate.estimated_output_cost_per_million or 0)
        )
        if preset == "quality":
            policy = (-candidate.quality_score, -candidate.routing_priority, not verified)
        elif preset == "economy":
            policy = (not price_known, estimated_price, -candidate.routing_priority)
        elif preset == "local_first":
            policy = (not local, -candidate.routing_priority, -candidate.quality_score)
        else:
            policy = (-candidate.routing_priority, not verified, -candidate.quality_score, not local)
        route_role = (
            candidate.invocation_target.route_role if candidate.invocation_target is not None
            else "primary"
        )
        route_priority = {"primary": 0, "vision": 1, "native_search": 2}.get(route_role, 3)
        health_priority = {
            "ready": 0,
            "degraded": 1,
            # An expired cooling record is admitted by compatibility as a
            # half-open probe and remains behind healthy/degraded routes.
            "cooling_down": 2,
            "configuration_blocked": 3,
        }.get(
            candidate.invocation_target.health_status
            if candidate.invocation_target is not None else "ready",
            3,
        )
        return (
            health_priority, not bound, not preferred, *policy, route_priority,
            candidate.provider_profile_id, candidate.model_id, candidate.target_key,
        )

    return sorted(candidates, key=key)[0]
