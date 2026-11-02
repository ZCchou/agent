"""Provider-independent capability and adapter contracts.

The execution planner still accepts the legacy ``text``/``native_web_search``
signature used by persisted plans.  This module is the stable vocabulary for
new routes and provides the compatibility mapping at the boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

CapabilityStatus = Literal[
    "unknown", "declared", "verified", "unsupported",
    "temporary_error", "configuration_blocked",
]
EvidenceKind = Literal["structured", "opaque", "none"]
CAPABILITY_CONTRACT_VERSION = "capability-v1"
ROUTE_MANIFEST_CONTRACT_VERSION = "route-manifest-v5"


@dataclass(frozen=True)
class CapabilityTemplate:
    id: str
    input_contract: str
    output_event_types: tuple[str, ...]
    required_verification: bool = False
    automatic_routing_allowed: bool = True
    evidence_kind: EvidenceKind = "none"
    degradation_policy: str = "route_only"


@dataclass(frozen=True)
class AdapterManifest:
    adapter_id: str
    protocol: str
    capabilities: tuple[str, ...]
    transport: str
    evidence_kind: EvidenceKind = "none"
    input_contract_version: str = "capability-v1"
    output_contract_version: str = "capability-v1"
    verification_method: str = "minimal_request"


@dataclass(frozen=True)
class CapabilityInvocation:
    capability_id: str
    provider_id: str
    model_id: str | None = None
    route_id: str = "legacy.primary"
    messages: tuple[dict[str, Any], ...] = ()
    input_artifacts: tuple[dict[str, Any], ...] = ()
    tool_definitions: tuple[dict[str, Any], ...] = ()
    search_requirements: dict[str, Any] | None = None
    file_requirements: dict[str, Any] | None = None
    output_budget: int | None = None
    run_id: str | None = None
    step_id: str | None = None


@dataclass(frozen=True)
class CapabilityEvent:
    """Normalized runtime event envelope shared by all adapters."""

    event_type: str
    run_id: str
    step_id: str
    capability_id: str
    adapter_id: str
    route_id: str
    sequence: int
    payload: dict[str, Any] | None = None


CAPABILITY_TEMPLATES: dict[str, CapabilityTemplate] = {
    "text_generation": CapabilityTemplate(
        "text_generation", "messages-v1", ("capability.started", "capability.text_delta", "capability.completed"),
    ),
    "vision_input": CapabilityTemplate(
        "vision_input", "messages-with-artifacts-v1", ("capability.started", "capability.text_delta", "capability.completed"),
        required_verification=True,
    ),
    "reasoning": CapabilityTemplate(
        "reasoning", "messages-v1", ("capability.started", "capability.reasoning_delta", "capability.completed"),
        required_verification=True,
    ),
    "function_calling": CapabilityTemplate(
        "function_calling", "messages-with-tools-v1", ("capability.started", "capability.tool_call", "capability.completed"),
        required_verification=True,
    ),
    "web_search": CapabilityTemplate(
        "web_search", "messages-with-search-requirements-v1", (
            "capability.web_search_started", "capability.web_search_result", "capability.completed",
        ),
        required_verification=True,
        evidence_kind="opaque",
    ),
    "structured_web_evidence": CapabilityTemplate(
        "structured_web_evidence", "messages-with-search-requirements-v1", (
            "capability.web_search_started", "capability.web_search_result", "capability.source", "capability.completed",
        ),
        required_verification=True,
        evidence_kind="structured",
    ),
    "file_read": CapabilityTemplate(
        "file_read", "file-request-v1", ("capability.file_read", "capability.completed"),
        required_verification=True,
    ),
    "file_write": CapabilityTemplate(
        "file_write", "file-request-v1", ("capability.file_write", "capability.completed"),
        required_verification=True,
    ),
}


def _manifest(
    adapter_id: str, protocol: str, capabilities: tuple[str, ...], transport: str,
    *, evidence_kind: EvidenceKind = "none", verification_method: str = "minimal_request",
) -> AdapterManifest:
    return AdapterManifest(
        adapter_id=adapter_id, protocol=protocol, capabilities=capabilities,
        transport=transport, evidence_kind=evidence_kind,
        verification_method=verification_method,
    )


_CHAT_CAPABILITIES = (
    "text_generation", "vision_input", "reasoning", "function_calling",
)
ADAPTER_MANIFESTS: dict[str, AdapterManifest] = {
    "kimi.chat.openai": _manifest("kimi.chat.openai", "openai_chat", _CHAT_CAPABILITIES, "openai_chat"),
    "kimi.web.builtin": _manifest(
        "kimi.web.builtin", "openai_chat", ("web_search",), "kimi_builtin", evidence_kind="opaque",
    ),
    "kimi.web.formula": _manifest(
        "kimi.web.formula", "openai_chat", ("web_search",), "kimi_formula", evidence_kind="opaque",
    ),
    "deepseek.chat.openai": _manifest("deepseek.chat.openai", "openai_chat", _CHAT_CAPABILITIES, "openai_chat"),
    "deepseek.anthropic_web": _manifest(
        "deepseek.anthropic_web", "anthropic_messages", (
            "text_generation", "web_search", "structured_web_evidence",
        ), "anthropic_messages", evidence_kind="structured",
    ),
    "openai.responses.web": _manifest(
        "openai.responses.web", "openai_responses", (
            "text_generation", "web_search", "structured_web_evidence",
        ), "responses", evidence_kind="structured",
    ),
    "host.search": _manifest(
        "host.search", "host", ("web_search", "structured_web_evidence"), "host", evidence_kind="structured",
    ),
    "host.file": _manifest(
        "host.file", "host", ("file_read", "file_write"), "host_file",
    ),
    # Persisted route identifiers from the v2/v4 compatibility layer.
    "kimi.openai_chat": _manifest("kimi.openai_chat", "openai_chat", ("web_search",), "kimi_builtin", evidence_kind="opaque"),
    "kimi.formula_search": _manifest("kimi.formula_search", "openai_chat", ("web_search",), "kimi_formula", evidence_kind="opaque"),
    "anthropic_messages_web_search": _manifest(
        "anthropic_messages_web_search", "anthropic_messages", ("text_generation", "web_search", "structured_web_evidence"), "anthropic_messages", evidence_kind="structured",
    ),
    "openai_responses_web_search": _manifest(
        "openai_responses_web_search", "openai_responses", ("text_generation", "web_search", "structured_web_evidence"), "responses", evidence_kind="structured",
    ),
    "host_search_bridge": _manifest(
        "host_search_bridge", "host", ("web_search", "structured_web_evidence"), "host", evidence_kind="structured",
    ),
}


_ALIASES = {
    "text": "text_generation",
    "vision": "vision_input",
    "function_tools": "function_calling",
    "native_web_search": "web_search",
}


def canonical_capability(value: str) -> str:
    return _ALIASES.get(value, value)


def capability_template(capability_id: str) -> CapabilityTemplate:
    try:
        return CAPABILITY_TEMPLATES[canonical_capability(capability_id)]
    except KeyError as exc:
        raise ValueError(f"unknown capability: {capability_id}") from exc


def adapter_manifest(adapter_id: str) -> AdapterManifest | None:
    return ADAPTER_MANIFESTS.get(adapter_id)


def adapter_supports(adapter_id: str, capability_id: str) -> bool:
    manifest = adapter_manifest(adapter_id)
    return manifest is not None and canonical_capability(capability_id) in {
        canonical_capability(item) for item in manifest.capabilities
    }


_ERROR_ALIASES = {
    "unsupported_capability": "capability_unsupported",
    "native_search_protocol_unsupported": "capability_unsupported",
    "authentication_failed": "credential_invalid",
    "invalid_api_key": "credential_invalid",
    "rate_limited": "provider_rate_limited",
    "connection_reset": "provider_unavailable",
    "network_unavailable": "provider_unavailable",
    "request_timeout": "provider_unavailable",
    "invalid_provider_response": "provider_invalid_response",
    "model_tool_protocol_failed": "provider_invalid_response",
    "web_source_route_unavailable": "evidence_missing",
}


def normalize_capability_error_code(code: str) -> str:
    """Map provider/runtime errors onto the capability error contract."""
    return _ERROR_ALIASES.get(code, code)
