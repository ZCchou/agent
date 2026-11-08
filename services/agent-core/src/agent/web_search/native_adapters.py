from __future__ import annotations

from contextlib import aclosing

import json
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx

from agent.multimodal import anthropic_content


@dataclass(frozen=True)
class NativeAdapterManifest:
    adapter_id: str
    protocol: str
    capabilities: tuple[str, ...] = ("web_search",)
    evidence_kind: str = "opaque"
    transport: str = "provider"
    input_contract_version: str = "capability-v1"
    output_contract_version: str = "capability-v1"
    verification_method: str = "minimal_request"


ADAPTERS: dict[str, NativeAdapterManifest] = {
    item.adapter_id: item for item in (
        NativeAdapterManifest(
            "openai_responses_web_search", "openai_responses",
            ("web_search", "structured_web_evidence"), "structured", "responses",
        ),
        # Kept separate from the legacy setting name so new manifests can
        # declare the current Formula transport without changing the route
        # selection code. The compatibility alias is handled by the provider.
        NativeAdapterManifest("kimi_formula_web_search", "openai_chat"),
        NativeAdapterManifest("kimi_builtin_web_search", "openai_chat"),
        NativeAdapterManifest(
            "anthropic_messages_web_search", "anthropic_messages",
            ("web_search", "structured_web_evidence"), "structured", "anthropic_messages",
        ),
        NativeAdapterManifest(
            "host_search_bridge", "host", ("web_search", "structured_web_evidence"),
            "structured", "host",
        ),
    )
}


def _anthropic_search_tool_choice(force_search: bool) -> dict[str, str] | None:
    return {"type": "tool", "name": "web_search"} if force_search else None


class NativeAdapterFailure(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retry_after = retry_after


def _classify_anthropic_image_error(
    body: str, *, has_images: bool, has_web_search: bool, has_function_tools: bool,
) -> tuple[str, str] | None:
    lowered = body.lower()
    if not has_images or not any(
        marker in lowered for marker in ("image", "vision", "multimodal")
    ):
        return None
    if any(marker in lowered for marker in (
        "invalid image", "decode image", "invalid base64",
        "unsupported media type", "invalid media type", "mime type",
    )):
        return "image_format_unsupported", "The provider rejected the normalized image payload."
    if any(marker in lowered for marker in (
        "model does not support", "model doesn't support",
        "vision is not supported", "text-only model", "text only model",
    )):
        return "model_vision_unsupported", "The selected model does not support image input."
    if any(marker in lowered for marker in (
        "too large", "payload too large", "size limit", "maximum image",
    )):
        return "image_payload_too_large", "The provider rejected the image payload as too large."
    if has_web_search or has_function_tools:
        return (
            "multimodal_route_unsupported",
            "The selected native-search route cannot combine images with search or tools.",
        )
    return (
        "multimodal_protocol_unsupported",
        "The selected provider protocol cannot carry image input.",
    )


def _is_structured_native_search_unsupported(body: str) -> bool:
    """Only a structured capability error may permanently reject this route."""
    try:
        payload = json.loads(body)
    except ValueError:
        return False
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return False
    code = str(error.get("code") or error.get("type") or "").casefold()
    parameter = str(error.get("param") or error.get("parameter") or "").casefold()
    capability = str(error.get("capability") or "").casefold()
    explicit_target = any(
        marker in value
        for value in (code, parameter, capability)
        for marker in ("web_search", "web-search", "server_tool_use")
    )
    explicit_rejection = any(
        marker in code for marker in ("unsupported", "not_supported", "unknown_tool")
    ) or capability in {"unsupported", "not_supported"}
    return explicit_target and explicit_rejection


def _anthropic_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    system: list[str] = []
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if role == "system":
            if isinstance(content, str) and content:
                system.append(content)
            continue
        if role not in {"user", "assistant", "tool"}:
            continue
        if role == "tool":
            result_block = {
                "type": "tool_result",
                "tool_use_id": str(message.get("tool_call_id") or "tool"),
                "content": content if isinstance(content, str) else json.dumps(content),
            }
            # Anthropic expects all results for one assistant tool-use turn in
            # the following user message. The runtime stores one OpenAI-style
            # tool message per result, so merge adjacent results here.
            if converted and converted[-1].get("role") == "user" \
                    and isinstance(converted[-1].get("content"), list) \
                    and all(
                        isinstance(item, dict) and item.get("type") == "tool_result"
                        for item in converted[-1]["content"]
                    ):
                converted[-1]["content"].append(result_block)
            else:
                converted.append({"role": "user", "content": [result_block]})
            continue
        calls = message.get("tool_calls") if role == "assistant" else None
        if isinstance(calls, list) and calls:
            blocks: list[dict[str, Any]] = []
            if isinstance(content, str) and content:
                blocks.append({"type": "text", "text": content})
            elif isinstance(content, list):
                blocks.extend(item for item in content if isinstance(item, dict))
            for call in calls:
                function = call.get("function") if isinstance(call, dict) else None
                if not isinstance(function, dict):
                    continue
                arguments = function.get("arguments")
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except ValueError:
                        arguments = {}
                blocks.append({
                    "type": "tool_use",
                    "id": str(call.get("id") or "tool"),
                    "name": str(function.get("name") or ""),
                    "input": arguments if isinstance(arguments, dict) else {},
                })
            normalized: str | list[dict[str, Any]] = blocks
        else:
            normalized = anthropic_content(content)
        converted.append({"role": role, "content": normalized})
    if not converted:
        converted.append({"role": "user", "content": "Search the web."})
    return "\n\n".join(system), converted


def _tool_call_from_block(block: dict[str, Any]) -> dict[str, Any] | None:
    if block.get("type") != "tool_use":
        return None
    arguments = block.get("input")
    partial = block.get("_partial_json")
    if isinstance(partial, str) and partial:
        try:
            arguments = json.loads(partial)
        except ValueError as error:
            raise NativeAdapterFailure(
                "model_tool_protocol_failed",
                "The model returned invalid file-tool arguments.",
            ) from error
    if not isinstance(arguments, dict):
        raise NativeAdapterFailure(
            "model_tool_protocol_failed",
            "The model returned invalid file-tool arguments.",
        )
    name = block.get("name")
    if not isinstance(name, str) or not name:
        raise NativeAdapterFailure(
            "model_tool_protocol_failed",
            "The model returned an invalid file-tool name.",
        )
    return {
        "id": str(block.get("id") or "anthropic-tool"),
        "name": name,
        "arguments": arguments,
    }


def _source_from_block(block: dict[str, Any], position: int) -> dict[str, Any] | None:
    url = block.get("url") or block.get("source")
    if not isinstance(url, str) or not url.startswith("https://"):
        return None
    title = block.get("title")
    return {
        "provider_source_id": str(block.get("id") or position),
        "title": title if isinstance(title, str) and title else url,
        "url": url,
        # encrypted_content must remain in the in-memory Anthropic continuation
        # block, but it is opaque protocol state rather than a user-readable
        # snippet and must not be copied into the business database.
        "snippet": None,
        "published_at": block.get("page_age") if isinstance(block.get("page_age"), str) else None,
    }


def _citation_source(citation: dict[str, Any]) -> dict[str, Any] | None:
    url = citation.get("url")
    if not isinstance(url, str) or not url.startswith("https://"):
        return None
    return {
        "provider_source_id": str(citation.get("id") or url),
        "title": str(citation.get("title") or url),
        "url": url,
        "snippet": (
            citation.get("cited_text")[:500]
            if isinstance(citation.get("cited_text"), str) else None
        ),
    }


async def stream_anthropic_web_search(
    *,
    endpoint_base_url: str,
    api_key: str,
    model_id: str,
    messages: list[dict[str, Any]],
    max_output_tokens: int | None,
    force_search: bool,
    function_tools: list[dict[str, Any]] | None = None,
    enable_web_search: bool = True,
    local_strict: bool = False,
    _continuation_count: int = 0,
) -> AsyncIterator[dict[str, Any]]:
    """Normalize Anthropic Messages web-search streaming into provider-neutral events."""
    has_images = any(
        isinstance(message.get("content"), list)
        and any(isinstance(part, dict) and part.get("type") == "image"
                for part in message["content"])
        for message in messages
    )
    system, request_messages = _anthropic_messages(messages)
    tool_choice = _anthropic_search_tool_choice(force_search)
    if tool_choice is not None:
        system = (
            system + "\n\n" if system else ""
        ) + "The user explicitly requested current web information. Use web_search before answering."
    endpoint = endpoint_base_url.rstrip("/")
    if not endpoint.endswith("/v1"):
        endpoint += "/v1"
    endpoint += "/messages"
    payload: dict[str, Any] = {
        "model": model_id,
        "messages": request_messages,
        "max_tokens": max_output_tokens or 1024,
        "stream": True,
        "tools": [
            *([{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]
              if enable_web_search else []),
            *(function_tools or []),
        ],
    }
    if force_search:
        # A required search is a protocol constraint, not prompt advice.
        # DeepSeek's Anthropic-compatible endpoint supports named tool choice;
        # Anthropic Messages uses the same tool-choice representation.
        payload["tool_choice"] = tool_choice
    if system:
        payload["system"] = system
    headers = {
        "x-api-key": api_key,
        "authorization": f"Bearer {api_key}",
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    timeout = httpx.Timeout(600, connect=15, read=None)
    content_blocks: dict[int, dict[str, Any]] = {}
    pause_turn = False
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=not local_strict) as client:
            async with client.stream("POST", endpoint, headers=headers, json=payload) as response:
                if response.is_error:
                    body = (await response.aread()).decode("utf-8", errors="replace")
                    retry_after = response.headers.get("retry-after")
                    try:
                        retry_seconds = float(retry_after) if retry_after else None
                    except ValueError:
                        retry_seconds = None
                    lowered = body.lower()
                    image_error = _classify_anthropic_image_error(
                        body, has_images=has_images, has_web_search=enable_web_search,
                        has_function_tools=bool(function_tools),
                    ) if response.status_code in {400, 415, 422} else None
                    if image_error is not None:
                        raise NativeAdapterFailure(
                            image_error[0], image_error[1], status_code=response.status_code,
                        )
                    if response.status_code in {400, 404, 422} \
                            and _is_structured_native_search_unsupported(body):
                        raise NativeAdapterFailure(
                            "native_search_protocol_unsupported",
                            "The provider does not support the configured Anthropic web-search protocol.",
                            status_code=response.status_code,
                        )
                    code = "invalid_provider_response"
                    if response.status_code in {401, 403}:
                        code = "authentication_failed"
                    elif response.status_code == 429:
                        code = "rate_limited"
                    elif response.status_code >= 500:
                        code = "provider_unavailable"
                    raise NativeAdapterFailure(
                        code, f"Provider returned HTTP {response.status_code}.",
                        status_code=response.status_code, retry_after=retry_seconds,
                    )
                event_name = ""
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    if line.startswith(":"):
                        # Anthropic-compatible endpoints may use SSE comments as
                        # keepalives while the server-side search is running.
                        # Preserve activity without exposing or persisting data.
                        yield {"kind": "activity"}
                        continue
                    if line.startswith("event:"):
                        event_name = line[6:].strip()
                        continue
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    try:
                        event = json.loads(raw)
                    except ValueError as error:
                        raise NativeAdapterFailure(
                            "invalid_provider_response", "Anthropic search returned invalid stream data."
                        ) from error
                    kind = str(event.get("type") or event_name)
                    if kind == "ping":
                        yield {"kind": "activity"}
                    elif kind == "message_start":
                        usage = (event.get("message") or {}).get("usage") or {}
                        yield {"kind": "usage", "usage": {
                            "input_tokens": int(usage.get("input_tokens") or 0),
                            "output_tokens": int(usage.get("output_tokens") or 0),
                            "web_search_requests": int(
                                (usage.get("server_tool_use") or {}).get("web_search_requests") or 0
                            ),
                        }}
                    elif kind == "content_block_start":
                        block = event.get("content_block") or {}
                        index = event.get("index")
                        if isinstance(index, int) and isinstance(block, dict):
                            content_blocks[index] = dict(block)
                        block_type = block.get("type")
                        if block_type == "server_tool_use" and block.get("name") == "web_search":
                            yield {"kind": "web_search_started", "web_search": {
                                "provider_call_id": str(block.get("id") or "")
                            }}
                        elif block_type == "web_search_tool_result":
                            sources = []
                            for index, result in enumerate(block.get("content") or []):
                                if isinstance(result, dict):
                                    source = _source_from_block(result, index)
                                    if source:
                                        sources.append(source)
                            yield {"kind": "web_search_result", "web_search": {
                                "provider_call_id": str(block.get("tool_use_id") or ""),
                                "query": "",
                                "sources": sources,
                                "usage": {},
                            }}
                        else:
                            yield {"kind": "activity"}
                    elif kind == "content_block_delta":
                        delta = event.get("delta") or {}
                        index = event.get("index")
                        stored = content_blocks.get(index) if isinstance(index, int) else None
                        if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                            if stored is not None:
                                stored["text"] = str(stored.get("text") or "") + delta["text"]
                            yield {"kind": "text_delta", "text": delta["text"]}
                        elif delta.get("type") == "input_json_delta" and isinstance(delta.get("partial_json"), str):
                            if stored is not None:
                                stored["_partial_json"] = str(stored.get("_partial_json") or "") + delta["partial_json"]
                            yield {"kind": "activity"}
                        elif delta.get("type") == "citations_delta" and isinstance(delta.get("citation"), dict):
                            source = _citation_source(delta["citation"])
                            if source:
                                yield {"kind": "web_search_result", "web_search": {
                                    "provider_call_id": f"citation:{source['provider_source_id']}",
                                    "query": "",
                                    "sources": [source],
                                    "usage": {},
                                }}
                    elif kind == "content_block_stop":
                        index = event.get("index")
                        stored = content_blocks.get(index) if isinstance(index, int) else None
                        if stored is not None and stored.get("type") == "tool_use":
                            tool_call = _tool_call_from_block(stored)
                            if tool_call is not None:
                                yield {"kind": "tool_call", "tool_call": tool_call}
                        if stored is not None and stored.get("type") == "server_tool_use" \
                                and stored.get("name") == "web_search":
                            partial = stored.pop("_partial_json", None)
                            if isinstance(partial, str) and partial:
                                try:
                                    stored["input"] = json.loads(partial)
                                except ValueError:
                                    stored["input"] = {}
                            input_value = stored.get("input")
                            query = input_value.get("query") if isinstance(input_value, dict) else None
                            if isinstance(query, str) and query.strip():
                                yield {"kind": "web_search_started", "web_search": {
                                    "provider_call_id": str(stored.get("id") or ""),
                                    "query": query.strip(),
                                }}
                        yield {"kind": "activity"}
                    elif kind == "message_delta":
                        usage = event.get("usage") or {}
                        yield {"kind": "usage", "usage": {
                            "input_tokens": 0,
                            "output_tokens": int(usage.get("output_tokens") or 0),
                            "web_search_requests": int(
                                (usage.get("server_tool_use") or {}).get("web_search_requests") or 0
                            ),
                        }}
                        stop_reason = (event.get("delta") or {}).get("stop_reason")
                        if stop_reason == "pause_turn":
                            pause_turn = True
                            yield {"kind": "activity"}
                        elif stop_reason:
                            yield {"kind": "finish", "finish_reason": str(stop_reason)}
                    elif kind == "message_stop":
                        yield {"kind": "finish", "finish_reason": "end_turn"}
        if pause_turn:
            if _continuation_count >= 2:
                raise NativeAdapterFailure(
                    "invalid_provider_response",
                    "The provider requested too many web-search continuation turns.",
                )
            continuation = []
            for index in sorted(content_blocks):
                block = content_blocks[index]
                partial = block.pop("_partial_json", None)
                if isinstance(partial, str) and partial:
                    try:
                        block["input"] = json.loads(partial)
                    except ValueError:
                        block["input"] = {}
                continuation.append(block)
            async with aclosing(stream_anthropic_web_search(
                endpoint_base_url=endpoint_base_url,
                api_key=api_key,
                model_id=model_id,
                messages=[*messages, {"role": "assistant", "content": continuation}],
                max_output_tokens=max_output_tokens,
                force_search=False,
                function_tools=function_tools,
                enable_web_search=enable_web_search,
                local_strict=local_strict,
                _continuation_count=_continuation_count + 1,
            )) as provider_stream:
                async for event in provider_stream:
                    yield event
    except NativeAdapterFailure:
        raise
    except httpx.TimeoutException as error:
        raise NativeAdapterFailure("stream_stalled", "The provider stopped producing output.") from error
    except httpx.NetworkError as error:
        raise NativeAdapterFailure("connection_reset", "The provider connection was interrupted.") from error
