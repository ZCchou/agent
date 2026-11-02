from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any

from agent.execution.capabilities import adapter_manifest, canonical_capability
from agent.execution.domain import InvocationTarget, ModelCandidate

ROUTE_MANIFEST_VERSION = "invocation-routes-v5"


def normalize_signature(values: Any) -> tuple[str, ...]:
    allowed = {
        "text", "vision", "function_tools", "native_web_search", "reasoning",
        "text_generation", "vision_input", "function_calling", "web_search",
        "structured_web_evidence", "file_read", "file_write",
    }
    if not isinstance(values, (list, tuple)):
        return ("text",)
    normalized_values = {
        canonical_capability(value) for value in values
        if isinstance(value, str) and value in allowed
    }
    # Keep the legacy route signature internally so persisted plans and the
    # existing provider adapters remain wire-compatible. Structured evidence
    # is intentionally a separate capability and is never implied by search.
    legacy_names = {
        "text_generation": "text",
        "vision_input": "vision",
        "function_calling": "function_tools",
        "web_search": "native_web_search",
    }
    normalized = tuple(sorted(
        legacy_names.get(value, value) for value in normalized_values
    ))
    return normalized if "text" in normalized else ("text", *normalized)


def route_fingerprint(route: dict[str, Any]) -> str:
    payload = {
        key: route.get(key)
        for key in (
            "route_id", "route_role", "protocol", "adapter_id",
            "endpoint_base_url", "manifest_version", "source_provenance",
        )
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def candidate_for_route(
    base: ModelCandidate, route: dict[str, Any], capability_state: dict[str, Any] | None = None,
    health_state: dict[str, Any] | None = None,
) -> ModelCandidate:
    signature = normalize_signature(route.get("capabilities"))
    state = capability_state or {}
    status = str(
        state.get("support_status") or state.get("status") or route.get("status") or "unknown"
    )
    if status == "temporary_error":
        status = "unknown"
    health = health_state or {}
    adapter_id = str(route.get("adapter_id") or route.get("protocol") or "legacy")
    manifest = adapter_manifest(adapter_id)
    target = InvocationTarget(
        provider_profile_id=base.provider_profile_id,
        model_id=base.model_id,
        route_id=str(route.get("route_id") or "legacy.primary"),
        route_role=str(route.get("route_role") or route.get("role") or "primary"),  # type: ignore[arg-type]
        protocol=str(route.get("protocol") or base.protocol_id),
        adapter_id=adapter_id,
        capability_signature=signature,  # type: ignore[arg-type]
        route_manifest_version=str(
            route.get("manifest_version") or route.get("version") or ROUTE_MANIFEST_VERSION
        ),
        route_config_fingerprint=str(
            route.get("config_fingerprint") or route.get("fingerprint")
            or state.get("route_config_fingerprint")
            or route_fingerprint(route)
        ),
        source_provenance=str(route.get("source_provenance") or "unknown"),  # type: ignore[arg-type]
        capability_status=status,  # type: ignore[arg-type]
        health_status=str(health.get("health_status") or "ready"),  # type: ignore[arg-type]
        consecutive_failures=int(health.get("consecutive_failures") or 0),
        health_error_code=(
            str(health["last_error_code"]) if health.get("last_error_code") else None
        ),
        health_retry_at=(str(health["retry_at"]) if health.get("retry_at") else None),
        capability_id=(
            str(state.get("capability_id")) if state.get("capability_id") else None
        ),
        evidence_kind=str(
            state.get("evidence_kind") or route.get("evidence_kind")
            or (manifest.evidence_kind if manifest is not None else "unknown")
        ),
        last_verified_at=(
            str(state["last_verified_at"]) if state.get("last_verified_at") else None
        ),
    )
    native_status = status if {
        "native_web_search", "structured_web_evidence",
    } & set(signature) else "not_applicable"
    return replace(
        base, protocol_id=target.protocol, native_search_status=native_status,
        invocation_target=target,
    )


def legacy_routes(primary_protocol: str, native_search: dict[str, Any] | None) -> list[dict[str, Any]]:
    routes = [{
        "route_id": "legacy.primary", "role": "primary", "protocol": primary_protocol,
        "adapter_id": primary_protocol, "capabilities": ["text", "vision", "function_tools", "reasoning"],
        "status": "declared", "version": "legacy-v1",
        "source_provenance": "none",
    }]
    binding = native_search or {}
    adapter = binding.get("adapter_id")
    if isinstance(adapter, str) and adapter != "none":
        routes.append({
            "route_id": "legacy.native_search", "role": "native_search",
            "protocol": binding.get("protocol") or primary_protocol,
            "adapter_id": adapter, "endpoint_base_url": binding.get("endpoint_base_url"),
            "capabilities": ["text", "native_web_search"],
            "status": "declared", "version": binding.get("adapter_version") or "legacy-v1",
            "source_provenance": "unknown",
        })
    return routes


class InvocationRouteResolver:
    """Pure route enumerator; it cannot add models or read authority state."""

    @staticmethod
    def enumerate(
        base: ModelCandidate,
        routes: tuple[dict[str, Any], ...],
        states: tuple[dict[str, Any], ...],
        *,
        legacy_native_status: str,
        legacy_native_search: dict[str, Any] | None,
        health_states: tuple[dict[str, Any], ...] = (),
    ) -> tuple[ModelCandidate, ...]:
        configured = list(routes) or legacy_routes(base.protocol_id, legacy_native_search)
        candidates: list[ModelCandidate] = []
        for route in configured:
            route_id = route.get("route_id")
            route_states = [item for item in states if item.get("route_id") == route_id]
            if not route_states:
                signature = tuple(sorted(route.get("capabilities", [])))
                fallback_state = (
                    {"status": legacy_native_status}
                    if "native_web_search" in signature else None
                )
                candidates.append(candidate_for_route(
                    base, route, fallback_state,
                    InvocationRouteResolver._health_for(route, signature, health_states),
                ))
                continue
            for state in route_states:
                state_route = dict(route)
                state_route["capabilities"] = state.get("capability_signature", ["text"])
                state_signature = normalize_signature(state_route.get("capabilities"))
                candidates.append(candidate_for_route(
                    base, state_route, state,
                    InvocationRouteResolver._health_for(route, state_signature, health_states),
                ))
        return tuple(candidates)

    @staticmethod
    def _health_for(
        route: dict[str, Any], signature: tuple[str, ...],
        health_states: tuple[dict[str, Any], ...],
    ) -> dict[str, Any] | None:
        route_id = str(route.get("route_id") or "legacy.primary")
        fingerprint = str(
            route.get("config_fingerprint") or route.get("fingerprint") or route_fingerprint(route)
        )
        for item in health_states:
            key = item.get("key") if isinstance(item.get("key"), dict) else item
            if (
                key.get("route_id") == route_id
                and key.get("route_fingerprint") == fingerprint
                and normalize_signature(key.get("capability_signature")) == signature
            ):
                return item
        return None
