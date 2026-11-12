from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any

from agent.execution.domain import InvocationSpec
from agent.provider_tool_adapter import structured_messages
from agent.providers import GenerationOptions
from agent.storage import CoreDataError
from agent.tool_contracts import TOOL_CONTRACT_VERSION, ToolContractRegistry


@dataclass(frozen=True)
class AdapterDialect:
    adapter_id: str
    protocol_id: str
    native_search_adapter: str = "none"
    native_search_transport: str = "none"


ADAPTER_DIALECTS: dict[str, AdapterDialect] = {
    "fake.deterministic": AdapterDialect("fake.deterministic", "memory"),
    "ollama.chat": AdapterDialect("ollama.chat", "ollama"),
    "openai.chat": AdapterDialect("openai.chat", "openai_chat"),
    "openai.responses": AdapterDialect(
        "openai.responses", "openai_responses", "openai_responses_web_search",
        "server_managed",
    ),
    "kimi.openai_chat": AdapterDialect(
        "kimi.openai_chat", "openai_chat", "kimi_builtin_web_search",
        "client_continuation",
    ),
    "deepseek.openai_chat": AdapterDialect("deepseek.openai_chat", "openai_chat"),
    "deepseek.responses": AdapterDialect("deepseek.responses", "openai_responses"),
    "deepseek.anthropic_web": AdapterDialect(
        "deepseek.anthropic_web", "anthropic_messages", "anthropic_messages_web_search",
        "server_managed",
    ),
    "anthropic.messages": AdapterDialect(
        "anthropic.messages", "anthropic_messages", "anthropic_messages_web_search",
        "server_managed",
    ),
}

_CAPABILITIES = {
    "text", "vision", "function_tools", "native_web_search", "reasoning",
}


def canonical_adapter_id(route_id: str, protocol_id: str, adapter_id: str) -> str:
    """Translate v2 route manifests once, before an immutable v3 spec is stored."""
    if adapter_id in ADAPTER_DIALECTS:
        return adapter_id
    if route_id.startswith("deepseek."):
        if protocol_id == "anthropic_messages":
            return "deepseek.anthropic_web"
        if protocol_id == "openai_responses":
            return "deepseek.responses"
        return "deepseek.openai_chat"
    if route_id.startswith("kimi.") or adapter_id == "kimi_builtin_web_search":
        return "kimi.openai_chat"
    if protocol_id == "memory":
        return "fake.deterministic"
    if protocol_id == "ollama":
        return "ollama.chat"
    if protocol_id == "anthropic_messages":
        return "anthropic.messages"
    if protocol_id == "openai_responses":
        return "openai.responses"
    if protocol_id == "openai_chat":
        return "openai.chat"
    raise CoreDataError(
        "invocation_spec_invalid", f"No v3 adapter exists for protocol {protocol_id!r}.",
    )


def bind_profile_to_target(profile: Any, target: dict[str, Any]) -> Any:
    """Bind transport configuration to one planned target through the adapter registry."""
    protocol = target.get("protocol_id")
    if not isinstance(protocol, str) or not protocol:
        raise CoreDataError("invocation_route_unavailable", "The planned protocol is missing.")
    route_id = target.get("invocation_route_id") or "legacy.primary"
    route = next((
        item for item in profile.invocation_routes
        if item.get("route_id") == route_id
    ), None)
    if route is None or route_id == "legacy.primary":
        return replace(profile, primary_protocol=protocol)
    adapter_id = canonical_adapter_id(
        route_id, protocol, str(target.get("adapter_id") or route.get("adapter_id") or ""),
    )
    dialect = ADAPTER_DIALECTS.get(adapter_id)
    if dialect is None or dialect.protocol_id != protocol:
        raise CoreDataError(
            "invocation_protocol_mismatch", "The planned route has no matching adapter dialect.",
        )
    endpoint = route.get("endpoint_base_url")
    native_search = profile.native_search
    role = route.get("route_role") or route.get("role")
    if role == "native_search":
        native_search = {
            "adapter_id": dialect.native_search_adapter,
            "protocol": protocol,
            "endpoint_base_url": endpoint,
            "source": route.get("source", "official_preset"),
            "adapter_version": route.get("manifest_version", "unified-invocation-v3"),
        }
    return replace(
        profile,
        base_url=endpoint if isinstance(endpoint, str) and endpoint else profile.base_url,
        primary_protocol=protocol,
        native_search=native_search,
    )


@dataclass(frozen=True)
class PreparedRequest:
    spec_id: str
    adapter_id: str
    protocol_id: str
    model_id: str
    messages: tuple[dict[str, Any], ...]
    tools_enabled: bool
    tool_transport: str
    tool_schema_hash: str
    options: GenerationOptions
    request_fingerprint: str
    input_budget: dict[str, int] | None = None


class InvocationCompiler:
    """Pure compiler from one immutable spec to one provider request dialect."""

    def compile_model(
        self, spec: InvocationSpec, messages: list[dict[str, Any]],
        *, context_window: int | None = None,
    ) -> PreparedRequest:
        if spec.kind != "model" or not spec.model_id:
            raise CoreDataError("invocation_spec_invalid", "A model invocation requires a model.")
        dialect = ADAPTER_DIALECTS.get(spec.adapter_id)
        if dialect is None:
            raise CoreDataError(
                "invocation_spec_invalid", "The invocation adapter is not registered.",
            )
        if dialect.protocol_id != spec.protocol_id:
            raise CoreDataError(
                "invocation_protocol_mismatch",
                "The immutable invocation protocol does not match its adapter.",
            )
        signature = tuple(sorted(set(spec.capability_signature)))
        if not signature or any(item not in _CAPABILITIES for item in signature):
            raise CoreDataError(
                "invocation_spec_invalid", "The capability signature is invalid.",
            )
        has_function_tools = bool(spec.allowed_tool_names)
        if has_function_tools and "function_tools" not in signature:
            raise CoreDataError(
                "capability_combination_unsupported",
                "The specification contains tools without the function-tools capability.",
            )
        if spec.tool_transport not in {"native_function_call", "structured_json", "none"}:
            raise CoreDataError("tool_transport_unavailable", "The tool transport is not registered.")
        if has_function_tools and spec.tool_transport == "none":
            raise CoreDataError(
                "tool_transport_unavailable", "The specification has tools but no tool transport.",
            )
        if not has_function_tools and spec.tool_transport != "none":
            raise CoreDataError(
                "invocation_spec_invalid", "A tool transport requires an allowed tool set.",
            )
        if spec.tool_contract_version != TOOL_CONTRACT_VERSION and has_function_tools:
            raise CoreDataError("tool_contract_unverified", "The tool contract version is unsupported.")
        schema_hash = (
            ToolContractRegistry.schema_hash(spec.allowed_tool_names)
            if has_function_tools else ""
        )
        if has_function_tools and spec.tool_schema_hash and spec.tool_schema_hash != schema_hash:
            raise CoreDataError("tool_schema_mismatch", "The tool schema hash does not match the contract.")
        has_native_search = "native_web_search" in signature
        if has_native_search != (spec.native_search_mode != "off"):
            raise CoreDataError(
                "invocation_spec_invalid",
                "Native-search capability and invocation mode disagree.",
            )
        if has_native_search and (
            dialect.native_search_adapter == "none"
            or dialect.native_search_transport == "none"
        ):
            raise CoreDataError(
                "native_search_continuation_unsupported",
                "The selected adapter has no executable native-search transport.",
            )
        if not isinstance(messages, list) or not messages:
            raise CoreDataError("invocation_payload_invalid", "The model request has no messages.")
        compiled_messages = (
            structured_messages(messages, set(spec.allowed_tool_names))
            if spec.tool_transport == "structured_json" else messages
        )
        options = GenerationOptions(
            purpose=spec.endpoint_role,
            max_output_tokens=spec.max_output_tokens,
            reasoning_effort=spec.reasoning_effort,
            response_format=(
                spec.response_format if spec.response_format in {"json_object", "json_schema"} else None
            ),
            response_schema=json.loads(spec.response_schema_json) if spec.response_schema_json else None,
            allowed_tools=spec.allowed_tool_names,
            invocation_adapter_id=spec.adapter_id,
            capability_signature=signature,
            native_search_mode=spec.native_search_mode,
        )
        input_budget = None
        if context_window and all(message.get('content') is None or isinstance(message.get('content'), str) for message in compiled_messages):
            from agent.context_engine.policy import estimate_tokens, resolve_budget
            window, _, reserve, input_limit = resolve_budget(context_window=context_window, max_output_tokens=spec.max_output_tokens)
            envelope = {'model': spec.model_id, 'messages': compiled_messages,
                        'response_schema': options.response_schema,
                        'tools': ToolContractRegistry.definitions(spec.allowed_tool_names) if has_function_tools else []}
            request_tokens = estimate_tokens(envelope)
            message_tokens = estimate_tokens(compiled_messages)
            input_budget = {'context_window': window, 'output_reserve': reserve, 'input_hard_limit': input_limit,
                            'request_tokens': request_tokens, 'message_tokens': message_tokens,
                            'input_overhead_tokens': max(0, request_tokens - estimate_tokens(messages)),
                            'remaining_input_tokens': input_limit - request_tokens}
            if request_tokens > input_limit:
                raise CoreDataError('provider_context_limit_exceeded', 'The compiled request exceeds its input capacity.', {
                    'input_overhead_tokens': max(0, request_tokens - estimate_tokens(messages)),
                    'request_tokens': request_tokens, 'input_limit': input_limit,
                    'input_budget': input_budget,
                })
        fingerprint_payload = {
            "spec_hash": spec.spec_hash,
            "adapter": spec.adapter_id,
            "protocol": spec.protocol_id,
            "message_hashes": [
                hashlib.sha256(
                    json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    .encode("utf-8")
                ).hexdigest()
                for item in compiled_messages
            ],
        }
        fingerprint = hashlib.sha256(
            json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return PreparedRequest(
            input_budget=input_budget,
            spec_id=spec.spec_id,
            adapter_id=spec.adapter_id,
            protocol_id=spec.protocol_id,
            model_id=spec.model_id,
            messages=tuple(compiled_messages),
            tools_enabled=has_function_tools and spec.tool_transport == "native_function_call",
            tool_transport=spec.tool_transport,
            tool_schema_hash=schema_hash,
            options=options,
            request_fingerprint=fingerprint,
        )
