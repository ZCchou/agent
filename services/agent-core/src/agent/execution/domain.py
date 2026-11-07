from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any, Literal

SelectionMode = Literal["fixed", "automatic"]
RoutingPreset = Literal["quality", "balanced", "economy", "local_first"]
StepType = Literal[
    "intent_analysis", "vision_analysis", "web_research", "reasoning_analysis",
    "final_response", "grounding_fallback",
]
CapabilityKey = Literal[
    "text", "vision", "function_tools", "native_web_search", "reasoning",
    "structured_web_evidence", "file_read", "file_write",
]
CapabilityAdvisoryId = Literal[
    "vision_input", "web_search", "structured_web_evidence", "function_calling",
    "file_read", "file_write", "reasoning",
]
ToolTransport = Literal["native_function_call", "structured_json", "none"]


@dataclass(frozen=True)
class CapabilityAdvisory:
    """A safe, provider-independent explanation for a degraded plan."""

    capability_id: CapabilityAdvisoryId
    requested_level: str
    status: Literal["unavailable", "not_authorized", "unverified", "route_failed"]
    reason_code: str
    fallback_mode: str
    user_message_key: str

    def to_dict(self) -> dict[str, str]:
        return {
            "capability_id": self.capability_id,
            "requested_level": self.requested_level,
            "status": self.status,
            "reason_code": self.reason_code,
            "fallback_mode": self.fallback_mode,
            "user_message_key": self.user_message_key,
        }


def invocation_limits_for(
    capability_signature: tuple[str, ...] | list[str],
) -> tuple[int, int, int]:
    """Return deterministic connect, activity and total limits for one spec.

    Server-side native search can legitimately spend longer between visible
    model deltas than an ordinary generation.  This policy is frozen before
    dispatch as part of the immutable InvocationSpec; adapters cannot extend it.
    """
    if {"native_web_search", "function_tools"} & set(capability_signature):
        return 10_000, 60_000, 180_000
    return 10_000, 30_000, 120_000


def stable_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CapabilityRequirements:
    text: bool = True
    vision: Literal["none", "required"] = "none"
    web: Literal["off", "optional", "required"] = "off"
    web_evidence: Literal["optional", "required"] = "optional"
    files: Literal["none", "available", "read", "write"] = "none"
    tools: Literal["none", "available", "required"] = "none"
    reasoning: Literal["standard", "preferred", "required"] = "standard"
    minimum_context_tokens: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def hash(self) -> str:
        return stable_hash(self.to_dict())

    def to_capability_map(self) -> dict[str, str]:
        """Expose the provider-independent requirement contract.

        The legacy fields remain serialized for compatibility with existing
        plans and IPC clients; this view is the canonical vocabulary consumed
        by new adapters and route diagnostics.
        """
        return {
            "text_generation": "required" if self.text else "none",
            "vision_input": self.vision,
            "reasoning": "none" if self.reasoning == "standard" else self.reasoning,
            "function_calling": self.tools,
            "web_search": "none" if self.web == "off" else self.web,
            "structured_web_evidence": self.web_evidence if self.web != "off" else "none",
            "file_read": "required" if self.files == "read" else (
                "preferred" if self.files in {"available", "write"} else "none"
            ),
            "file_write": "required" if self.files == "write" else "none",
        }

    @classmethod
    def from_capability_map(cls, values: dict[str, str]) -> "CapabilityRequirements":
        """Build legacy-compatible requirements from the new capability map."""
        web_level = values.get("web_search", "none")
        evidence_level = values.get("structured_web_evidence", "none")
        file_read = values.get("file_read", "none")
        file_write = values.get("file_write", "none")
        files: Literal["none", "available", "read", "write"] = "none"
        if file_write in {"required", "preferred"}:
            files = "write"
        elif file_read == "required":
            files = "read"
        elif file_read == "preferred":
            files = "available"
        return cls(
            text=values.get("text_generation", "required") != "none",
            vision="required" if values.get("vision_input", "none") == "required" else "none",
            web=("required" if web_level == "required" else "optional" if web_level == "preferred" else "off"),
            web_evidence=("required" if evidence_level == "required" else "optional"),
            files=files,
            tools=("required" if values.get("function_calling") == "required" else "available" if values.get("function_calling") == "preferred" else "none"),
            reasoning=("required" if values.get("reasoning") == "required" else "preferred" if values.get("reasoning") == "preferred" else "standard"),
        )


@dataclass(frozen=True)
class ProtocolCapabilityManifest:
    protocol_id: str
    image_input: Literal["yes", "no", "model_dependent"]
    function_tools: bool
    native_search: bool
    image_with_function_tools: bool | Literal["unknown"]
    image_with_native_search: bool | Literal["unknown"]
    reasoning_with_native_search: bool | Literal["unknown"]
    version: str = "execution-routing-v1"


@dataclass(frozen=True)
class InvocationTarget:
    provider_profile_id: str
    model_id: str
    route_id: str
    route_role: Literal["primary", "vision", "native_search"]
    protocol: str
    adapter_id: str
    capability_signature: tuple[CapabilityKey, ...]
    route_manifest_version: str
    route_config_fingerprint: str
    source_provenance: Literal["structured", "opaque", "none", "unknown"] = "unknown"
    capability_status: Literal[
        "declared", "verified", "unsupported", "unknown", "temporary_error",
        "configuration_blocked",
    ] = "unknown"
    health_status: Literal[
        "ready", "degraded", "cooling_down", "configuration_blocked"
    ] = "ready"
    consecutive_failures: int = 0
    health_error_code: str | None = None
    health_retry_at: str | None = None
    capability_id: str | None = None
    evidence_kind: Literal["structured", "opaque", "none", "unknown"] = "unknown"
    last_verified_at: str | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        return self.provider_profile_id, self.model_id, self.route_id


@dataclass(frozen=True)
class ModelCandidate:
    provider_profile_id: str
    model_id: str
    protocol_id: str
    location: Literal["local", "online"]
    capabilities: dict[str, Any]
    tool_mode: str = "unknown"
    native_search_status: str = "not_applicable"
    health: str = "available"
    quality_score: int = 50
    routing_priority: int = 0
    estimated_input_cost_per_million: Decimal | None = None
    estimated_output_cost_per_million: Decimal | None = None
    invocation_target: InvocationTarget | None = None
    capability_states: tuple[dict[str, Any], ...] = ()

    @property
    def key(self) -> tuple[str, str]:
        return self.provider_profile_id, self.model_id

    @property
    def target_key(self) -> tuple[str, ...]:
        if self.invocation_target is None:
            return self.provider_profile_id, self.model_id, self.protocol_id
        return (*self.invocation_target.key, ",".join(self.invocation_target.capability_signature))


@dataclass(frozen=True)
class ExecutionBudget:
    max_model_calls: int = 3
    max_specialist_steps: int = 2
    max_extra_online_cost_usd: Decimal = Decimal("0.20")
    allow_unknown_price_automatic: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_model_calls": self.max_model_calls,
            "max_specialist_steps": self.max_specialist_steps,
            "max_extra_online_cost_usd": str(self.max_extra_online_cost_usd),
            "allow_unknown_price_automatic": self.allow_unknown_price_automatic,
        }


@dataclass(frozen=True)
class ExecutionStepSpec:
    step_id: str
    phase: Literal["analysis", "specialist", "synthesis"]
    step_type: StepType
    provider_profile_id: str
    model_id: str
    protocol_id: str
    order_index: int
    required: bool = True
    activation_condition: Literal[
        "always", "when_web_required", "when_vision_required",
        "when_web_evidence_missing",
    ] = "always"
    input_source_ids: tuple[str, ...] = ()
    invocation_route_id: str = "legacy.primary"
    adapter_id: str = "legacy"
    capability_signature: tuple[str, ...] = ("text",)
    route_config_fingerprint: str = "legacy"

    @classmethod
    def create(cls, **values: Any) -> "ExecutionStepSpec":
        return cls(step_id=str(uuid.uuid4()), **values)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["input_source_ids"] = list(self.input_source_ids)
        result["capability_signature"] = list(self.capability_signature)
        return result


@dataclass(frozen=True)
class ExecutionPlan:
    plan_id: str
    coordinator_provider_profile_id: str
    coordinator_model_id: str
    generation_protocol: str
    selection_mode: SelectionMode
    routing_preset: RoutingPreset
    selection_source: str
    selection_reason_code: str
    web_route: str
    web_adapter_or_backend_id: str | None
    web_backend_kind: Literal["tavily", "searxng"] | None
    reasoning_effort: Literal["none", "low", "medium", "high"]
    requirements_hash: str
    policy_version: str
    steps: tuple[ExecutionStepSpec, ...]
    budget: ExecutionBudget
    estimated_extra_online_cost_usd: Decimal | None = Decimal("0")
    requires_budget_confirmation: bool = False
    route_catalog_hash: str = "legacy"
    route_policy_version: str = "invocation-routing-policy-v2"
    authority_generation: int = 0
    degraded_capabilities: tuple[CapabilityAdvisory, ...] = ()
    plan_hash: str = field(default="")

    def __post_init__(self) -> None:
        if not self.plan_hash:
            object.__setattr__(self, "plan_hash", stable_hash(self.hash_payload()))

    def hash_payload(self) -> dict[str, Any]:
        stable_steps = []
        for step in self.steps:
            value = step.to_dict()
            value.pop("step_id", None)
            stable_steps.append(value)
        return {
            "coordinator": [self.coordinator_provider_profile_id, self.coordinator_model_id],
            "protocol": self.generation_protocol,
            "selection_mode": self.selection_mode,
            "routing_preset": self.routing_preset,
            "selection_source": self.selection_source,
            "reason": self.selection_reason_code,
            "web_route": self.web_route,
            "web_adapter_or_backend_id": self.web_adapter_or_backend_id,
            "web_backend_kind": self.web_backend_kind,
            "reasoning_effort": self.reasoning_effort,
            "requirements_hash": self.requirements_hash,
            "policy_version": self.policy_version,
            "steps": stable_steps,
            "budget": self.budget.to_dict(),
            "route_catalog_hash": self.route_catalog_hash,
            "route_policy_version": self.route_policy_version,
            "authority_generation": self.authority_generation,
            "degraded_capabilities": [
                advisory.to_dict() for advisory in self.degraded_capabilities
            ],
            "estimated_extra_online_cost_usd": (
                None if self.estimated_extra_online_cost_usd is None
                else str(self.estimated_extra_online_cost_usd)
            ),
        }

    def to_dict(self) -> dict[str, Any]:
        return {"plan_id": self.plan_id, **self.hash_payload(), "plan_hash": self.plan_hash,
                "requires_budget_confirmation": self.requires_budget_confirmation}


InvocationKind = Literal["model", "host_search"]
InvocationStatus = Literal[
    "prepared", "dispatched", "streaming", "completed", "failed", "cancelled",
    "interrupted", "unknown_outcome", "stale",
]


@dataclass(frozen=True)
class InvocationSpec:
    spec_id: str
    plan_id: str
    step_id: str
    round_no: int
    attempt_no: int
    kind: InvocationKind
    provider_or_backend_id: str
    model_id: str | None
    route_id: str
    protocol_id: str
    adapter_id: str
    endpoint_role: str
    route_fingerprint: str
    capability_signature: tuple[str, ...]
    context_snapshot_id: str | None = None
    message_id: str | None = None
    artifact_ids: tuple[str, ...] = ()
    rejection_receipt_ids: tuple[str, ...] = ()
    tool_result_ids: tuple[str, ...] = ()
    tool_contract_version: str = "tool-contract-v3"
    allowed_tool_names: tuple[str, ...] = ()
    tool_transport: ToolTransport = "none"
    tool_schema_hash: str = ""
    native_search_mode: Literal["off", "auto", "required"] = "off"
    reasoning_effort: Literal["none", "low", "medium", "high"] = "none"
    max_output_tokens: int = 4096
    response_format: Literal["text", "json_object"] = "text"
    connect_timeout_ms: int = 10_000
    activity_timeout_ms: int = 30_000
    total_timeout_ms: int = 120_000
    serialization_version: str = "unified-invocation-v3.4"
    run_id: str | None = None
    authority_generation: int = 0
    status: InvocationStatus = "prepared"
    spec_hash: str = ""

    def __post_init__(self) -> None:
        if self.round_no < 0 or self.attempt_no < 1 or self.authority_generation < 0:
            raise ValueError("Invocation sequence values are invalid")
        if self.max_output_tokens < 1:
            raise ValueError("Invocation output limit is invalid")
        if not self.spec_hash:
            object.__setattr__(self, "spec_hash", stable_hash(self.hash_payload()))

    def hash_payload(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "step_id": self.step_id,
            "round_no": self.round_no,
            "attempt_no": self.attempt_no,
            "kind": self.kind,
            "target": {
                "provider_or_backend_id": self.provider_or_backend_id,
                "model_id": self.model_id,
                "route_id": self.route_id,
                "protocol_id": self.protocol_id,
                "adapter_id": self.adapter_id,
                "endpoint_role": self.endpoint_role,
                "route_fingerprint": self.route_fingerprint,
            },
            "capability_signature": list(self.capability_signature),
            "inputs": {
                "context_snapshot_id": self.context_snapshot_id,
                "message_id": self.message_id,
                "artifact_ids": list(self.artifact_ids),
                "rejection_receipt_ids": list(self.rejection_receipt_ids),
                "tool_result_ids": list(self.tool_result_ids),
            },
            "tools": {
                "contract_version": self.tool_contract_version,
                "allowed_names": list(self.allowed_tool_names),
                "transport": self.tool_transport,
                "schema_hash": self.tool_schema_hash,
                "native_search_mode": self.native_search_mode,
            },
            "generation": {
                "reasoning_effort": self.reasoning_effort,
                "max_output_tokens": self.max_output_tokens,
                "response_format": self.response_format,
            },
            "limits": {
                "connect_timeout_ms": self.connect_timeout_ms,
                "activity_timeout_ms": self.activity_timeout_ms,
                "total_timeout_ms": self.total_timeout_ms,
            },
            "serialization_version": self.serialization_version,
            "authority_generation": self.authority_generation,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "spec_id": self.spec_id,
            **self.hash_payload(),
            "status": self.status,
            "spec_hash": self.spec_hash,
        }
