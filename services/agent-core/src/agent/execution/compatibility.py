from __future__ import annotations

from datetime import datetime, timezone

from agent.execution.domain import (
    CapabilityRequirements, ModelCandidate, ProtocolCapabilityManifest,
)

PROTOCOL_MANIFESTS: dict[str, ProtocolCapabilityManifest] = {
    "memory": ProtocolCapabilityManifest("memory", "yes", True, False, True, False, False),
    "ollama": ProtocolCapabilityManifest("ollama", "model_dependent", True, False, True, False, False),
    "openai_chat": ProtocolCapabilityManifest(
        "openai_chat", "model_dependent", True, False, True, False, False,
    ),
    "openai_responses": ProtocolCapabilityManifest(
        "openai_responses", "model_dependent", True, True, True, "unknown", "unknown",
    ),
    "anthropic_messages": ProtocolCapabilityManifest(
        "anthropic_messages", "model_dependent", True, True, True, "unknown", "unknown",
    ),
}


def incompatibility_reason(
    candidate: ModelCandidate, requirements: CapabilityRequirements, *, automatic: bool,
) -> str | None:
    if candidate.health != "available":
        return "model_unavailable"
    capabilities = candidate.capabilities
    capability_states = {
        str(item.get("capability_id")): item
        for item in candidate.capability_states
        if isinstance(item, dict) and isinstance(item.get("capability_id"), str)
    }

    def model_capability_status(capability_id: str, fallback: str) -> str:
        state = capability_states.get(capability_id)
        if not state:
            return fallback
        return str(
            state.get("verified_status")
            if state.get("verified_status") not in {None, "unknown"}
            else state.get("declared_status") or fallback
        )

    text_status = model_capability_status("text_generation", capabilities.get("text", "yes"))
    if capabilities.get("text", "yes") == "no" or text_status in {"unsupported", "no"}:
        return "model_capability_unsupported"
    if automatic and text_status == "unknown":
        return "model_capability_unverified"
    context_length = capabilities.get("context_length")
    if requirements.minimum_context_tokens:
        if isinstance(context_length, int) and context_length < requirements.minimum_context_tokens:
            return "model_context_too_small"
        if automatic and not isinstance(context_length, int):
            return "model_capability_unverified"
    manifest = PROTOCOL_MANIFESTS.get(candidate.protocol_id)
    if manifest is None:
        return "multimodal_protocol_unsupported"
    target = candidate.invocation_target
    if target is not None:
        if target.capability_status == "configuration_blocked":
            return "route_configuration_blocked"
        if target.capability_status == "temporary_error":
            return "route_temporarily_cooling_down"
        if target.capability_status == "unsupported":
            return "capability_signature_unsupported"
        if target.capability_status == "unknown":
            return "capability_signature_unknown"
        if target.health_status == "configuration_blocked":
            return "route_configuration_blocked"
        if target.health_status == "cooling_down" and _retry_is_future(target.health_retry_at):
            return "route_temporarily_cooling_down"
    target_signature = set(
        target.capability_signature if target is not None else ()
    )
    if requirements.vision == "required":
        status = model_capability_status(
            "vision_input", capabilities.get("vision_status", "unknown")
        )
        value = capabilities.get("vision", "unknown")
        if value == "no" or status == "unsupported" or manifest.image_input == "no":
            return "model_vision_unsupported"
        if automatic and (value != "yes" or status == "unknown"):
            return "model_vision_unverified"
        if target_signature and "vision" not in target_signature:
            return "capability_signature_unsupported"
    if requirements.tools == "required":
        tool_status = model_capability_status("function_calling", candidate.tool_mode)
        if not manifest.function_tools or candidate.tool_mode in {"text_only", "unsupported"}:
            return "model_tool_unsupported"
        if tool_status in {"unsupported", "no"}:
            return "model_tool_unsupported"
        if automatic and tool_status == "unknown":
            return "model_tool_unverified"
        if requirements.vision == "required" and manifest.image_with_function_tools != True:
            return "capability_combination_unsupported"
        if target_signature and "function_tools" not in target_signature:
            return "capability_signature_unsupported"
    if requirements.web == "required":
        if candidate.native_search_status in {"unsupported"}:
            return "native_web_search_unsupported"
        if not target_signature and candidate.native_search_status not in {"declared", "verified"}:
            return "native_web_search_unsupported"
        if target_signature and not ({
            "native_web_search", "structured_web_evidence",
        } & target_signature):
            return "capability_signature_unsupported"
        if automatic and candidate.native_search_status not in {"declared", "verified"}:
            return "capability_signature_unknown"
        if requirements.web_evidence == "required":
            # Legacy candidates without a route target have no verifiable
            # provenance. They remain usable for opaque/fixed search, but can
            # never satisfy an automatically grounded request.
            if target is None or (
                target.source_provenance != "structured"
                and target.evidence_kind != "structured"
            ):
                return "web_source_route_unavailable"
    if requirements.web == "required" and requirements.vision == "required" \
            and candidate.native_search_status in {"declared", "verified"} \
            and manifest.image_with_native_search != True:
        return "capability_combination_unsupported"
    if requirements.reasoning == "required":
        reasoning = model_capability_status("reasoning", capabilities.get("reasoning", "unknown"))
        if reasoning == "no":
            return "model_capability_unsupported"
        if automatic and reasoning != "yes":
            return "model_capability_unverified"
        if target_signature and "reasoning" not in target_signature:
            return "capability_signature_unsupported"
    return None


def _retry_is_future(value: str | None) -> bool:
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed > datetime.now(timezone.utc)


def compatible_candidates(
    candidates: list[ModelCandidate], requirements: CapabilityRequirements, *, automatic: bool,
) -> tuple[list[ModelCandidate], dict[tuple[str, ...], str]]:
    accepted: list[ModelCandidate] = []
    rejected: dict[tuple[str, ...], str] = {}
    for candidate in candidates:
        reason = incompatibility_reason(candidate, requirements, automatic=automatic)
        if reason is None:
            accepted.append(candidate)
        else:
            rejected[candidate.target_key] = reason
    return accepted, rejected
