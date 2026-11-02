"""Adapter registration and capability-scoped health state.

Adapters are deliberately small: providers own transport details while the
planner only sees manifests and capability states.  Existing provider streams
can be wrapped incrementally through :func:`normalize_provider_event`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol

from agent.execution.capabilities import (
    ADAPTER_MANIFESTS, AdapterManifest, CapabilityEvent, CapabilityInvocation,
    canonical_capability,
)


@dataclass(frozen=True)
class ModelCapabilityState:
    provider_id: str
    model_id: str
    capability_id: str
    declared_status: str = "unknown"
    verified_status: str = "unknown"
    limits: dict[str, Any] = field(default_factory=dict)
    last_verified_at: str | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class RouteCapabilityState:
    provider_id: str
    model_id: str
    route_id: str
    adapter_id: str
    capability_id: str
    status: str = "unknown"
    evidence_kind: str = "none"
    health_status: str = "ready"
    error_code: str | None = None
    config_fingerprint: str = ""
    last_verified_at: str | None = None


class CapabilityAdapter(Protocol):
    manifest: AdapterManifest

    async def invoke(self, request: CapabilityInvocation) -> AsyncIterator[CapabilityEvent]:
        """Invoke one capability and yield only normalized events."""


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, CapabilityAdapter] = {}
        self._manifests: dict[str, AdapterManifest] = dict(ADAPTER_MANIFESTS)

    def register(self, adapter: CapabilityAdapter) -> None:
        self._adapters[adapter.manifest.adapter_id] = adapter
        self._manifests[adapter.manifest.adapter_id] = adapter.manifest

    def manifest(self, adapter_id: str) -> AdapterManifest | None:
        return self._manifests.get(adapter_id)

    def adapter(self, adapter_id: str) -> CapabilityAdapter | None:
        return self._adapters.get(adapter_id)

    def manifests(self) -> tuple[AdapterManifest, ...]:
        return tuple(self._manifests.values())


ADAPTER_REGISTRY = AdapterRegistry()


_EVENT_TYPES = {
    "started": "capability.started",
    "text_delta": "capability.text_delta",
    "reasoning_delta": "capability.reasoning_delta",
    "tool_call": "capability.tool_call",
    "web_search_started": "capability.web_search_started",
    "web_search_result": "capability.web_search_result",
    "source": "capability.source",
    "file_read": "capability.file_read",
    "file_write": "capability.file_write",
    "usage": "capability.usage",
    "completed": "capability.completed",
    "finish": "capability.completed",
    "failed": "capability.failed",
    "provider_error": "capability.failed",
}


def normalize_provider_event(
    event: Any, request: CapabilityInvocation, *, adapter_id: str,
    sequence: int,
) -> CapabilityEvent:
    """Normalize a legacy ``ProviderEvent`` without importing providers.

    The duck-typed boundary avoids a dependency cycle and ensures provider-only
    event names never cross into the runtime event protocol.
    """
    kind = str(getattr(event, "kind", "provider_error"))
    event_type = _EVENT_TYPES.get(kind, "capability.failed")
    payload: dict[str, Any] = {}
    for field_name in ("text", "tool_call", "usage", "finish_reason", "web_search", "diagnostic"):
        value = getattr(event, field_name, None)
        if value not in (None, "", {}):
            payload[field_name] = value
    return CapabilityEvent(
        event_type=event_type,
        run_id=request.run_id or "unknown",
        step_id=request.step_id or "unknown",
        capability_id=canonical_capability(request.capability_id),
        adapter_id=adapter_id,
        route_id=request.route_id,
        sequence=sequence,
        payload=payload or None,
    )
