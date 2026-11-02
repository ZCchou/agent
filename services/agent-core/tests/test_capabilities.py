from __future__ import annotations

from agent.execution.capabilities import (
    CAPABILITY_TEMPLATES,
    ADAPTER_MANIFESTS,
    CapabilityInvocation,
    capability_template,
    canonical_capability,
    normalize_capability_error_code,
)
from agent.execution.domain import CapabilityRequirements
from agent.execution.adapters import (
    ModelCapabilityState,
    RouteCapabilityState,
    normalize_provider_event,
)


def test_first_class_capability_templates_cover_core_scope() -> None:
    assert set(CAPABILITY_TEMPLATES) >= {
        "text_generation",
        "vision_input",
        "reasoning",
        "function_calling",
        "web_search",
        "structured_web_evidence",
        "file_read",
        "file_write",
    }
    assert capability_template("structured_web_evidence").required_verification is True


def test_adapter_manifests_separate_opaque_and_structured_search() -> None:
    assert "web_search" in ADAPTER_MANIFESTS["kimi.web.builtin"].capabilities
    assert "structured_web_evidence" not in ADAPTER_MANIFESTS["kimi.web.builtin"].capabilities
    assert "web_search" in ADAPTER_MANIFESTS["kimi.web.formula"].capabilities
    assert "structured_web_evidence" not in ADAPTER_MANIFESTS["kimi.web.formula"].capabilities
    assert "structured_web_evidence" in ADAPTER_MANIFESTS["deepseek.anthropic_web"].capabilities
    assert "structured_web_evidence" in ADAPTER_MANIFESTS["host.search"].capabilities


def test_legacy_capability_names_are_canonicalized_without_breaking_routes() -> None:
    assert canonical_capability("text") == "text_generation"
    assert canonical_capability("vision") == "vision_input"
    assert canonical_capability("function_tools") == "function_calling"
    assert canonical_capability("native_web_search") == "web_search"
    assert canonical_capability("native_web_search") != "structured_web_evidence"


def test_capability_states_are_scoped_to_model_or_route() -> None:
    model = ModelCapabilityState("kimi", "kimi-k3", "text_generation", verified_status="verified")
    route = RouteCapabilityState(
        "kimi", "kimi-k3", "kimi.web.formula", "kimi.web.formula",
        "web_search", status="temporary_error", health_status="cooling_down",
    )
    assert model.capability_id == "text_generation"
    assert route.capability_id == "web_search"
    assert model.verified_status == "verified"
    assert route.status == "temporary_error"


def test_provider_events_are_normalized_with_execution_identity() -> None:
    class LegacyEvent:
        kind = "text_delta"
        text = "ok"
        tool_call = usage = finish_reason = web_search = diagnostic = None

    event = normalize_provider_event(
        LegacyEvent(), CapabilityInvocation(
            "native_web_search", "kimi", "kimi-k3", "kimi.web.formula",
            run_id="run-1", step_id="step-1",
        ), adapter_id="kimi.web.formula", sequence=4,
    )
    assert event.event_type == "capability.text_delta"
    assert event.capability_id == "web_search"
    assert (event.run_id, event.step_id, event.route_id, event.sequence) == (
        "run-1", "step-1", "kimi.web.formula", 4,
    )


def test_capability_map_round_trips_to_legacy_requirements() -> None:
    requirements = CapabilityRequirements(
        vision="required", reasoning="preferred", tools="required",
        web="required", web_evidence="required", files="write",
    )
    restored = CapabilityRequirements.from_capability_map(requirements.to_capability_map())
    assert restored == requirements


def test_provider_error_aliases_are_capability_scoped() -> None:
    assert normalize_capability_error_code("authentication_failed") == "credential_invalid"
    assert normalize_capability_error_code("connection_reset") == "provider_unavailable"
    assert normalize_capability_error_code("invalid_provider_response") == "provider_invalid_response"
