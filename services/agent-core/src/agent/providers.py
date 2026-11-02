from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator
from urllib.parse import urlparse

import httpx

from agent.multimodal import (
    contains_images,
    ollama_content,
    openai_chat_content,
    responses_content,
)
from agent.tool_contracts import TOOL_DEFINITIONS, ToolCall, validate_tool_call
from agent.storage import CoreDataError
from agent.web_search.native_adapters import (
    ADAPTERS,
    NativeAdapterFailure,
    stream_anthropic_web_search,
)
from agent.execution.capabilities import normalize_capability_error_code


@dataclass(frozen=True)
class ProviderEvent:
    kind: str
    text: str = ""
    # Some reasoning providers require this opaque state to be returned with
    # an assistant tool-call message. It is never emitted as user-visible text.
    opaque_reasoning_delta: str = ""
    tool_call: ToolCall | None = None
    usage: dict[str, int] | None = None
    finish_reason: str | None = None
    web_search: dict[str, Any] | None = None
    # Safe transport metadata only. Never put prompts, generated text, image
    # bytes, credentials, or full provider payloads in this field.
    diagnostic: dict[str, Any] | None = None
    # Execution identity is attached by ModelInvocationService at the unified
    # boundary. Defaults preserve compatibility for provider-only unit tests.
    run_id: str | None = None
    step_id: str | None = None
    capability_id: str | None = None
    adapter_id: str | None = None
    route_id: str | None = None
    sequence: int | None = None


@dataclass(frozen=True)
class GenerationOptions:
    purpose: str = "chat"
    max_output_tokens: int | None = None
    response_format: str | None = None
    allowed_tools: tuple[str, ...] | None = None
    invocation_adapter_id: str | None = None
    capability_signature: tuple[str, ...] = ("text",)
    native_search_mode: str = "off"
    # None keeps legacy direct callers' transport defaults unchanged.
    reasoning_effort: str | None = None

    @property
    def native_search_enabled(self) -> bool:
        return (
            "native_web_search" in self.capability_signature
            and self.native_search_mode in {"auto", "required"}
        )

    @property
    def native_search_required(self) -> bool:
        return self.native_search_enabled and self.native_search_mode == "required"

    @property
    def native_transport_adapter(self) -> str:
        return {
            "openai.responses": "openai_responses_web_search",
            "openai.responses.web": "openai_responses_web_search",
            "kimi.openai_chat": "kimi_builtin_web_search",
            "kimi.web.builtin": "kimi_builtin_web_search",
            "kimi.formula_search": "kimi_formula_web_search",
            "kimi.web.formula": "kimi_formula_web_search",
            "deepseek.anthropic_web": "anthropic_messages_web_search",
        }.get(self.invocation_adapter_id or "", self.invocation_adapter_id or "none")


@dataclass(frozen=True)
class ProviderProfile:
    id: str
    name: str
    kind: str
    base_url: str
    api_key: str | None = None
    enabled: bool = True
    endpoint_mode: str = "exact"
    allowed_model_ids: tuple[str, ...] = ()
    automatic_model_ids: tuple[str, ...] = ()
    model_capabilities: dict[str, dict[str, Any]] | None = None
    model_capability_states: dict[str, tuple[dict[str, Any], ...]] | None = None
    model_tool_modes: dict[str, str] | None = None
    primary_protocol: str = "openai_chat"
    native_search: dict[str, Any] | None = None
    model_native_web_search: dict[str, str] | None = None
    invocation_routes: tuple[dict[str, Any], ...] = ()
    model_route_capabilities: dict[str, tuple[dict[str, Any], ...]] | None = None
    model_route_health: dict[str, tuple[dict[str, Any], ...]] | None = None

    @property
    def is_local(self) -> bool:
        host = (urlparse(self.base_url).hostname or "").lower()
        return self.kind == "fake" or host in {"localhost", "127.0.0.1", "::1"}

    @property
    def native_web_adapter(self) -> str:
        value = self.native_search or {}
        adapter = value.get("adapter_id")
        return adapter if isinstance(adapter, str) else "none"

    @property
    def native_search_endpoint(self) -> str:
        value = self.native_search or {}
        endpoint = value.get("endpoint_base_url")
        return endpoint if isinstance(endpoint, str) and endpoint else self.base_url


class ProviderError(Exception):
    def __init__(
        self, code: str, message: str, retry_after: float | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.capability_code = normalize_capability_error_code(code)
        self.message = message
        self.retry_after = retry_after
        # Details must remain safe structural diagnostics. They are logged by
        # the invocation boundary and are never returned as model content.
        self.details = details or {}
        # Metered usage survives hidden-call failures without retaining content.
        self.usage: dict[str, int] = {}


def validate_provider_completion(
    content: str, finish_reason: str | None, usage: dict[str, Any], *,
    has_semantic_output: bool = False, require_output: bool = True,
) -> None:
    """Reject unsuccessful termination before accepting or executing output."""
    if finish_reason in {"length", "max_tokens", "max_output_tokens", "incomplete"}:
        raise ProviderError(
            "provider_response_incomplete", "The provider did not complete the response.",
            details={"incomplete_reason": finish_reason, "text_character_count": len(content)},
        )
    if require_output and not content.strip() and not has_semantic_output:
        raise ProviderError(
            "provider_empty_output", "The provider completed without usable output.",
            details={
                "retry_safe": finish_reason in {"stop", "completed"} and not usage,
                "usage_present": bool(usage), "text_character_count": len(content),
            },
        )


def validate_profile(profile: ProviderProfile) -> None:
    if profile.kind not in {"fake", "ollama", "openai_compatible"}:
        raise ProviderError("invalid_provider", "Provider type is not supported.")
    if not set(profile.automatic_model_ids).issubset(profile.allowed_model_ids):
        raise ProviderError(
            "invalid_provider",
            "Automatically routable models must also be enabled for manual use.",
        )
    if profile.kind == "fake":
        return
    if profile.primary_protocol not in {
        "ollama", "openai_chat", "openai_responses", "anthropic_messages",
    }:
        raise ProviderError("invalid_provider", "Primary provider protocol is not supported.")
    if profile.native_web_adapter != "none" and profile.native_web_adapter not in ADAPTERS:
        raise ProviderError("invalid_provider", "Native web-search adapter is not supported.")
    binding = profile.native_search or {}
    if profile.native_web_adapter != "none":
        manifest = ADAPTERS[profile.native_web_adapter]
        if binding.get("protocol") != manifest.protocol:
            raise ProviderError("invalid_provider", "Native web-search protocol does not match its adapter.")
        endpoint = profile.native_search_endpoint
        parsed_endpoint = urlparse(endpoint)
        if parsed_endpoint.scheme != "https" or not parsed_endpoint.hostname \
                or parsed_endpoint.username or parsed_endpoint.password:
            raise ProviderError("invalid_provider_url", "Native search endpoint must use HTTPS.")
    if profile.endpoint_mode not in {"preset", "exact", "legacy_auto_v1"}:
        raise ProviderError("invalid_provider", "Provider endpoint mode is not supported.")
    parsed = urlparse(profile.base_url)
    if not parsed.scheme or not parsed.hostname or parsed.username or parsed.password:
        raise ProviderError("invalid_provider_url", "Provider URL is invalid.")
    local = parsed.hostname.lower() in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (local and parsed.scheme == "http"):
        raise ProviderError("insecure_provider_url", "Remote providers must use HTTPS.")
    if parsed.query or parsed.fragment:
        raise ProviderError("invalid_provider_url", "Provider URL cannot contain a query or fragment.")


def _map_http_error(response: httpx.Response) -> ProviderError:
    status = response.status_code
    lowered = response.text.lower()
    if status == 400 and "response_format" in lowered and any(
        marker in lowered for marker in ("unsupported", "unknown", "unrecognized", "not support")
    ):
        return ProviderError(
            "response_format_unsupported", "The provider does not support JSON response format.",
        )
    if status in {400, 415, 422} and any(
        marker in lowered for marker in ("image", "vision", "multimodal")
    ):
        if any(marker in lowered for marker in (
            "invalid image", "decode image", "invalid base64",
            "unsupported media type", "invalid media type", "mime type",
        )):
            return ProviderError(
                "image_format_unsupported", "The provider rejected the normalized image format.",
            )
        if any(marker in lowered for marker in (
            "model does not support", "model doesn't support", "vision is not supported",
            "text-only model", "text only model",
        )):
            return ProviderError(
                "model_vision_unsupported", "The selected model does not support image input.",
                details={"support_evidence": "provider_explicit"},
            )
        if any(marker in lowered for marker in ("too large", "payload", "size limit")):
            return ProviderError(
                "image_payload_too_large", "The provider rejected the image payload as too large.",
            )
        return ProviderError(
            "invalid_provider_response",
            "The provider rejected the image request without an explicit capability reason.",
        )
    if any(marker in lowered for marker in (
        "context length", "maximum context", "max context", "too many tokens",
        "prompt is too long", "context window",
    )):
        return ProviderError(
            "provider_context_limit_exceeded", "The provider rejected the request as too large."
        )
    if any(marker in lowered for marker in ("out of memory", "oom", "resource exhausted")):
        return ProviderError("resource_exhausted", "The model provider ran out of resources.")
    if status in {401, 403}:
        return ProviderError("authentication_failed", "Provider authentication failed.")
    if status == 404:
        return ProviderError("model_not_found", "The selected model or endpoint was not found.")
    if status == 429:
        retry_after = response.headers.get("retry-after")
        try:
            seconds = float(retry_after) if retry_after else None
        except ValueError:
            seconds = None
        return ProviderError("rate_limited", "The provider rate limit was reached.", seconds)
    if status in {402, 422} and "quota" in lowered:
        return ProviderError("quota_exhausted", "The provider quota is exhausted.")
    if status >= 500:
        return ProviderError("provider_unavailable", "The provider is temporarily unavailable.")
    return ProviderError("invalid_provider_response", f"Provider returned HTTP {status}.")


class ModelProvider:
    def __init__(self, profile: ProviderProfile) -> None:
        validate_profile(profile)
        self.profile = profile

    async def list_models(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def stream(self, model_id: str, messages: list[dict[str, str]]) -> AsyncIterator[str]:
        raise NotImplementedError

    async def stream_events(
        self, model_id: str, messages: list[dict[str, Any]], *, tools: bool,
        options: GenerationOptions | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        async for text in self.stream(model_id, messages):
            yield ProviderEvent("text_delta", text=text)


class FakeModelProvider(ModelProvider):
    async def list_models(self) -> list[dict[str, Any]]:
        return [{"id": "demo", "name": "Built-in Demo", "location": "local"}]

    async def stream(self, model_id: str, messages: list[dict[str, str]]) -> AsyncIterator[str]:
        last_content = next((message["content"] for message in reversed(messages)
                     if message["role"] == "user"), "")
        last = _plain_content(last_content)
        system = " ".join(
            _plain_content(message["content"]) for message in messages if message["role"] == "system"
        )
        policy = next((category for marker, category in (
            ("evidence, sources", "research"), ("outcomes, steps", "work"),
            ("audience, structure", "writing"), ("correctness, tests", "coding"),
            ("mechanics and strategy", "gaming"), ("natural and lightweight", "casual"),
        ) if marker in system), "other")
        if model_id == "fault:timeout":
            raise ProviderError("request_timeout", "The test model timed out.")
        if model_id == "fault:oom":
            raise ProviderError("resource_exhausted", "The test model exhausted available memory.")
        if model_id == "fault:invalid":
            raise ProviderError("invalid_provider_response", "The test model returned invalid data.")
        image_count = sum(
            1 for message in messages for part in (
                message.get("content") if isinstance(message.get("content"), list) else []
            ) if isinstance(part, dict) and part.get("type") == "image"
        )
        image_note = f"\n\nDetected {image_count} normalized image(s)." if image_count else ""
        reply = f"Built-in Demo · Local · {policy}\n\n{last}{image_note}"
        midpoint = max(1, len(reply) // 2)
        yield reply[:midpoint]
        if model_id == "fault:disconnect":
            raise ProviderError("connection_reset", "The test stream was interrupted.")
        if model_id == "fault:stall":
            await asyncio.sleep(35)
        await asyncio.sleep(0)
        yield reply[midpoint:]

    async def stream_events(
        self, model_id: str, messages: list[dict[str, Any]], *, tools: bool,
        options: GenerationOptions | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        if options and options.purpose == "intent_analysis":
            request = next((
                str(message.get("content") or "")
                for message in reversed(messages) if message.get("role") == "user"
            ), "")
            try:
                request_value = json.loads(request)
                request_text = str(request_value.get("request") or "")
            except (TypeError, json.JSONDecodeError):
                request_text = request
            lowered = request_text.casefold()
            file_work = any(marker in lowered for marker in (
                "空目录", "空文件夹", "empty directory", "empty folder",
            ))
            payload = {
                "files": "write" if file_work else "available",
                "tools": "required" if file_work else "available",
                "web": "not_needed",
                "web_evidence": "optional",
                "reasoning": "standard",
                "task_kinds": ["file_work"] if file_work else ["general"],
                "explanation_code": "file_cleanup" if file_work else "general_request",
            }
            yield ProviderEvent("text_delta", text=json.dumps(payload, ensure_ascii=True))
            return
        if any(
            message.get("role") == "system"
            and "auditable conversation checkpoint" in str(message.get("content", ""))
            for message in messages
        ):
            try:
                request = json.loads(str(next(
                    message["content"] for message in messages if message.get("role") == "user"
                )))
                sources = request["sources"]
                first = sources[0]
                source_id = first["source_id"]
                trust = first["trust"]
                text = str(first.get("content", ""))[:1000]
                payload = {
                    "goal": [{"text": text or "Continue the conversation", "source_ids": [source_id],
                              "trust": trust}],
                    "success_criteria": [], "constraints": [], "decisions": [], "completed": [],
                    "relative_files": [], "failed_approaches": [], "open_tasks": [],
                    "next_step": [{"text": "Continue from the preserved context",
                                   "source_ids": [source_id], "trust": trust}],
                }
            except (StopIteration, KeyError, TypeError, json.JSONDecodeError):
                raise ProviderError("invalid_provider_response", "The demo compaction input was invalid.")
            yield ProviderEvent("text_delta", text=json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
            return
        allowed_tools = set(options.allowed_tools or ()) if options and options.allowed_tools else None
        if tools and messages and messages[-1].get("role") == "tool":
            tool_name = messages[-1].get("name", "project operation")
            output = str(messages[-1].get("content", ""))
            original = next((str(message.get("content", "")) for message in reversed(messages)
                             if message.get("role") == "user"), "")
            if tool_name == "fs.read" and any(marker in original.casefold() for marker in (
                    "补充安装", "添加安装", "update installation", "add installation")):
                try:
                    read_result = json.loads(output)
                    path = str(read_result["path"])
                    content = str(read_result["content"])
                except (json.JSONDecodeError, KeyError, TypeError):
                    pass
                else:
                    suffix = "\n\n## Installation\n\nFollow the project setup instructions.\n"
                    yield ProviderEvent("tool_call", tool_call=ToolCall(
                        f"fake-change-{len(messages)}", "fs.propose_changes", {
                            "title": f"Add installation notes to {path}",
                            "mutations": [{
                                "kind": "overwrite_text", "path": path,
                                "content": content.rstrip() + suffix,
                                "expected_hash": read_result.get("sha256"),
                            }],
                        },
                    ))
                    return
            reply = f"Built-in Demo · Local\n\nCompleted {tool_name}: {output[:500]}"
            yield ProviderEvent("text_delta", text=reply)
            return
        last = next((str(message.get("content", "")) for message in reversed(messages)
                     if message.get("role") == "user"), "")
        lowered = last.casefold()
        if tools and (allowed_tools is None or "web.search" in allowed_tools) and any(
            marker in lowered for marker in ("搜索", "联网", "最新", "search the web", "look up")
        ):
            yield ProviderEvent("tool_call", tool_call=ToolCall(
                f"fake-web-{len(messages)}", "web.search", {"query": last[:500], "max_results": 5},
            ))
            return
        if tools and (allowed_tools is None or "fs.list" in allowed_tools) and any(
            marker in lowered for marker in ("列出", "查看目录", "list files", "list the files")
        ):
            yield ProviderEvent("tool_call", tool_call=ToolCall(
                f"fake-list-{len(messages)}", "fs.list", {"path": ".", "limit": 200},
            ))
            return
        read_path = _fake_path_after(last, ("读取", "read")) \
            if tools and (allowed_tools is None or "fs.read" in allowed_tools) else None
        if read_path:
            yield ProviderEvent("tool_call", tool_call=ToolCall(
                f"fake-read-{len(messages)}", "fs.read", {"path": read_path, "offset": 0, "length": 1048576},
            ))
            return
        write = _fake_write_request(last) \
            if tools and (allowed_tools is None or "fs.propose_changes" in allowed_tools) else None
        if write:
            path, content = write
            yield ProviderEvent("tool_call", tool_call=ToolCall(
                f"fake-change-{len(messages)}", "fs.propose_changes", {
                    "title": f"Create {path}",
                    "mutations": [{"kind": "create_text", "path": path, "content": content}],
                },
            ))
            return
        async for text in self.stream(model_id, messages):
            yield ProviderEvent("text_delta", text=text)


class OllamaProvider(ModelProvider):
    async def list_models(self) -> list[dict[str, Any]]:
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
                response = await client.get(f"{self.profile.base_url.rstrip('/')}/api/tags")
            if response.is_error:
                raise _map_http_error(response)
            try:
                payload = response.json()
                items = payload.get("models") if isinstance(payload, dict) else None
            except ValueError as error:
                raise ProviderError(
                    "invalid_provider_response", "Ollama returned an invalid model catalog.",
                ) from error
            if not isinstance(items, list):
                raise ProviderError("invalid_provider_response", "Ollama returned an invalid model catalog.")
            return [
                {"id": item["name"], "name": item.get("name", item["name"]), "location": "local"}
                for item in items if isinstance(item, dict) and isinstance(item.get("name"), str)
            ]
        except httpx.TimeoutException as error:
            raise ProviderError("request_timeout", "Ollama did not respond in time.") from error
        except httpx.NetworkError as error:
            raise ProviderError("network_unavailable", "Ollama is unavailable.") from error

    async def stream(self, model_id: str, messages: list[dict[str, str]]) -> AsyncIterator[str]:
        try:
            timeout = httpx.Timeout(600, connect=15, read=None)
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                async with client.stream("POST", f"{self.profile.base_url.rstrip('/')}/api/chat",
                                         json={"model": model_id, "messages": messages, "stream": True}) as response:
                    if response.is_error:
                        await response.aread()
                        raise _map_http_error(response)
                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        try:
                            payload = json.loads(line)
                            delta = payload.get("message", {}).get("content", "")
                        except (ValueError, AttributeError) as error:
                            raise ProviderError("invalid_provider_response", "Ollama returned invalid stream data.") from error
                        if delta:
                            yield delta
        except httpx.TimeoutException as error:
            raise ProviderError("stream_stalled", "Ollama stopped producing output.") from error
        except httpx.NetworkError as error:
            raise ProviderError("connection_reset", "The Ollama connection was interrupted.") from error

    async def stream_events(
        self, model_id: str, messages: list[dict[str, Any]], *, tools: bool,
        options: GenerationOptions | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        try:
            timeout = httpx.Timeout(600, connect=15, read=None)
            payload: dict[str, Any] = {
                "model": model_id, "messages": _ollama_messages(messages), "stream": True,
            }
            if options and options.max_output_tokens:
                payload["options"] = {"num_predict": options.max_output_tokens}
            if options and options.response_format == "json_object":
                payload["format"] = "json"
            if tools:
                payload["tools"] = _external_tool_definitions(options.allowed_tools if options else None)
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                async with client.stream(
                    "POST", f"{self.profile.base_url.rstrip('/')}/api/chat", json=payload,
                ) as response:
                    if response.is_error:
                        await response.aread()
                        raise _map_http_error(response)
                    seen_calls: set[str] = set()
                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        try:
                            value = json.loads(line)
                            message = value.get("message", {})
                            delta = message.get("content", "")
                            thinking = message.get("thinking", "")
                            calls = message.get("tool_calls", [])
                        except (ValueError, AttributeError) as error:
                            raise ProviderError("invalid_provider_response", "Ollama returned invalid stream data.") from error
                        if delta:
                            yield ProviderEvent("text_delta", text=delta)
                        elif thinking:
                            yield ProviderEvent("activity")
                        if value.get("done") and (
                            isinstance(value.get("prompt_eval_count"), int)
                            or isinstance(value.get("eval_count"), int)
                        ):
                            yield ProviderEvent("usage", usage={
                                "input_tokens": int(value.get("prompt_eval_count") or 0),
                                "output_tokens": int(value.get("eval_count") or 0),
                            })
                        if value.get("done") and value.get("done_reason"):
                            yield ProviderEvent(
                                "finish", finish_reason=str(value["done_reason"]),
                            )
                        for index, raw_call in enumerate(calls if isinstance(calls, list) else []):
                            function = raw_call.get("function", {}) if isinstance(raw_call, dict) else {}
                            external_name = function.get("name")
                            arguments = function.get("arguments", {})
                            call_id = raw_call.get("id") or f"ollama-{index}-{external_name}"
                            if call_id in seen_calls:
                                continue
                            seen_calls.add(call_id)
                            if isinstance(arguments, str):
                                arguments = json.loads(arguments)
                            call = validate_tool_call(ToolCall(
                                str(call_id), _internal_tool_name(external_name), arguments,
                            ))
                            yield ProviderEvent("tool_call", tool_call=call)
        except httpx.TimeoutException as error:
            raise ProviderError("stream_stalled", "Ollama stopped producing output.") from error
        except (httpx.NetworkError, json.JSONDecodeError) as error:
            code = "connection_reset" if isinstance(error, httpx.NetworkError) else "model_tool_protocol_failed"
            raise ProviderError(code, "Ollama tool output was interrupted or invalid.") from error


class OpenAICompatibleProvider(ModelProvider):
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.profile.api_key}"} if self.profile.api_key else {}

    def _endpoint(self, resource: str) -> str:
        base = self.profile.base_url.rstrip("/")
        if self.profile.endpoint_mode == "legacy_auto_v1" and not urlparse(base).path.rstrip("/").endswith("/v1"):
            return f"{base}/v1/{resource}"
        return f"{base}/{resource}"

    async def list_models(self) -> list[dict[str, Any]]:
        models, _ = await self.discover_models()
        return models

    async def discover_models(self) -> tuple[list[dict[str, Any]], str]:
        base = self.profile.base_url.rstrip("/")
        candidates = model_list_base_candidates(base, self.profile.endpoint_mode)
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
                for index, candidate in enumerate(candidates):
                    response = await client.get(f"{candidate}/models", headers=self._headers())
                    if response.status_code == 404 and index + 1 < len(candidates):
                        continue
                    if response.is_error:
                        raise _map_http_error(response)
                    try:
                        payload = response.json()
                        data = payload.get("data") if isinstance(payload, dict) else None
                    except ValueError as error:
                        raise ProviderError(
                            "invalid_provider_response", "Provider returned an invalid model catalog.",
                        ) from error
                    if not isinstance(data, list):
                        raise ProviderError(
                            "invalid_provider_response", "Provider returned an invalid model catalog.",
                        )
                    models = [
                        {
                            "id": item["id"], "name": item["id"], "location": "online",
                            "capabilities": {
                                "text": "yes",
                                "vision": _known_capability(
                                    item.get("supports_vision", item.get("supports_image_in"))
                                ),
                                "reasoning": _known_capability(item.get("supports_reasoning")),
                                "tools": _known_capability(
                                    item.get("supports_tools", item.get("supports_tool_calls"))
                                ),
                                "context_length": item.get("context_length")
                                if isinstance(item.get("context_length"), int) else None,
                            },
                        }
                        for item in data if isinstance(item, dict) and isinstance(item.get("id"), str)
                    ]
                    return models, candidate
            raise ProviderError("model_not_found", "The model-list endpoint was not found.")
        except httpx.TimeoutException as error:
            raise ProviderError("request_timeout", "The model provider did not respond in time.") from error
        except httpx.NetworkError as error:
            raise ProviderError("network_unavailable", "The model provider is unavailable.") from error

    async def stream(self, model_id: str, messages: list[dict[str, str]]) -> AsyncIterator[str]:
        try:
            timeout = httpx.Timeout(600, connect=15, read=None)
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                async with client.stream(
                    "POST", self._endpoint("chat/completions"),
                    headers=self._headers(), json={"model": model_id, "messages": messages, "stream": True},
                ) as response:
                    if response.is_error:
                        await response.aread()
                        raise _map_http_error(response)
                    async for line in response.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            payload = json.loads(data)
                            delta = payload["choices"][0].get("delta", {}).get("content", "")
                        except (ValueError, KeyError, IndexError, AttributeError) as error:
                            raise ProviderError("invalid_provider_response", "Provider returned invalid stream data.") from error
                        if delta:
                            yield delta
        except httpx.TimeoutException as error:
            raise ProviderError("stream_stalled", "The provider stopped producing output.") from error
        except httpx.NetworkError as error:
            raise ProviderError("connection_reset", "The provider connection was interrupted.") from error

    async def stream_events(
        self, model_id: str, messages: list[dict[str, Any]], *, tools: bool,
        options: GenerationOptions | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        if options and options.native_search_enabled \
                and options.native_transport_adapter == "anthropic_messages_web_search":
            if not self.profile.api_key:
                raise ProviderError("authentication_failed", "Provider credentials are required.")
            try:
                async for raw in stream_anthropic_web_search(
                    endpoint_base_url=self.profile.native_search_endpoint,
                    api_key=self.profile.api_key,
                    model_id=model_id,
                    # Normalize internal dotted tool names before translating
                    # the OpenAI-style continuation into Anthropic blocks.
                    messages=_provider_messages(messages),
                    max_output_tokens=options.max_output_tokens,
                    force_search=options.native_search_required,
                    function_tools=(
                        _anthropic_function_tools(options.allowed_tools) if tools else []
                    ),
                    enable_web_search=True,
                ):
                    tool_call = None
                    raw_tool_call = raw.get("tool_call")
                    if isinstance(raw_tool_call, dict):
                        try:
                            candidate = ToolCall(
                                str(raw_tool_call.get("id") or "anthropic-tool"),
                                _internal_tool_name(raw_tool_call.get("name")),
                                raw_tool_call.get("arguments"),
                            )
                            tool_call = validate_tool_call(candidate)
                        except (CoreDataError, TypeError) as error:
                            raise ProviderError(
                                "model_tool_protocol_failed",
                                "Model returned an invalid file-tool call.",
                            ) from error
                    yield ProviderEvent(
                        kind=raw["kind"],
                        text=raw.get("text", ""),
                        tool_call=tool_call,
                        usage=raw.get("usage"),
                        finish_reason=raw.get("finish_reason"),
                        web_search=raw.get("web_search"),
                    )
            except NativeAdapterFailure as error:
                raise ProviderError(
                    error.code, error.message, error.retry_after,
                    details=(
                        {"support_evidence": "provider_explicit"}
                        if error.code in {
                            "native_search_protocol_unsupported", "model_vision_unsupported",
                            "multimodal_protocol_unsupported",
                        } else None
                    ),
                ) from error
            return
        if options and options.native_search_enabled \
                and options.native_transport_adapter == "kimi_formula_web_search":
            if not self.profile.api_key:
                raise ProviderError("authentication_failed", "Provider credentials are required.")
            async for event in self._stream_kimi_formula_search(
                model_id, messages, options,
            ):
                yield event
            return
        if self.profile.primary_protocol == "anthropic_messages":
            if not self.profile.api_key:
                raise ProviderError("authentication_failed", "Provider credentials are required.")
            try:
                async for raw in stream_anthropic_web_search(
                    endpoint_base_url=self.profile.base_url,
                    api_key=self.profile.api_key,
                    model_id=model_id,
                    messages=messages,
                    max_output_tokens=options.max_output_tokens if options else None,
                    force_search=False,
                    function_tools=(
                        _anthropic_function_tools(options.allowed_tools) if tools and options else []
                    ),
                    enable_web_search=False,
                ):
                    raw_tool_call = raw.get("tool_call")
                    tool_call = None
                    if isinstance(raw_tool_call, dict):
                        try:
                            candidate = ToolCall(
                                str(raw_tool_call.get("id") or "anthropic-tool"),
                                _internal_tool_name(raw_tool_call.get("name")),
                                raw_tool_call.get("arguments"),
                            )
                            tool_call = validate_tool_call(candidate)
                        except (CoreDataError, TypeError) as error:
                            raise ProviderError(
                                "model_tool_protocol_failed",
                                "Model returned an invalid file-tool call.",
                            ) from error
                    yield ProviderEvent(
                        kind=raw["kind"], text=raw.get("text", ""), tool_call=tool_call,
                        usage=raw.get("usage"), finish_reason=raw.get("finish_reason"),
                    )
            except NativeAdapterFailure as error:
                raise ProviderError(
                    error.code, error.message, error.retry_after,
                    details=(
                        {"support_evidence": "provider_explicit"}
                        if error.code in {
                            "native_search_protocol_unsupported", "model_vision_unsupported",
                            "multimodal_protocol_unsupported",
                        } else None
                    ),
                ) from error
            return
        if options and options.native_search_enabled \
                and options.native_transport_adapter == "openai_responses_web_search":
            async for event in self._stream_responses(
                model_id, messages, options, include_web_search=True,
                include_function_tools=tools,
            ):
                yield event
            return
        if self.profile.primary_protocol == "openai_responses":
            async for event in self._stream_responses(
                model_id, messages, options or GenerationOptions(), include_web_search=False,
                include_function_tools=tools,
            ):
                yield event
            return
        calls: dict[int, dict[str, str]] = {}
        finish_reason: str | None = None
        streamed_text = ""
        response_opened = False
        try:
            timeout = httpx.Timeout(600, connect=15, read=None)
            payload: dict[str, Any] = {
                "model": model_id, "messages": _provider_messages(messages), "stream": True,
            }
            if options and options.max_output_tokens:
                payload["max_tokens"] = options.max_output_tokens
            if options and options.response_format == "json_object":
                payload["response_format"] = {"type": "json_object"}
            if options and options.invocation_adapter_id == "deepseek.openai_chat" \
                    and options.reasoning_effort is not None:
                payload["thinking"] = {
                    "type": "disabled" if options.reasoning_effort == "none" else "enabled",
                }
                if options.reasoning_effort != "none":
                    payload["reasoning_effort"] = (
                        "high" if options.reasoning_effort == "medium" else options.reasoning_effort
                    )
            if tools:
                payload["tools"] = _external_tool_definitions(options.allowed_tools if options else None)
            if options and options.native_search_enabled \
                    and options.native_transport_adapter == "kimi_builtin_web_search":
                payload.setdefault("tools", []).append({
                    "type": "builtin_function", "function": {"name": "$web_search"},
                })
                payload["thinking"] = {"type": "disabled"}
                payload["tool_choice"] = (
                    "required" if options.native_search_required else "auto"
                )
                if options.native_search_required:
                    payload["messages"] = [{
                        "role": "system",
                        "content": (
                            "The user explicitly requested a web search. "
                            "Use $web_search before answering."
                        ),
                    }, *payload["messages"]]
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                async with client.stream(
                    "POST", self._endpoint("chat/completions"), headers=self._headers(), json=payload,
                ) as response:
                    response_opened = True
                    if response.is_error:
                        await response.aread()
                        raise _map_http_error(response)
                    async for line in response.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            value = json.loads(data)
                            usage = value.get("usage")
                            if isinstance(usage, dict):
                                yield ProviderEvent("usage", usage={
                                    "input_tokens": int(usage.get("prompt_tokens") or 0),
                                    "output_tokens": int(usage.get("completion_tokens") or 0),
                                    "cached_input_tokens": int(
                                        (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
                                    ),
                                })
                            choices = value.get("choices") or []
                            if not choices:
                                continue
                            choice = choices[0]
                            delta = choice.get("delta", {})
                        except (ValueError, KeyError, IndexError, AttributeError) as error:
                            raise ProviderError("invalid_provider_response", "Provider returned invalid stream data.") from error
                        text = delta.get("content", "")
                        if text:
                            streamed_text += text
                            yield ProviderEvent("text_delta", text=text)
                        else:
                            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                            opaque_reasoning = reasoning if isinstance(reasoning, str) else ""
                            if reasoning:
                                # Activity keeps first-token/stall timers alive without exposing
                                # or persisting private chain-of-thought content.
                                yield ProviderEvent(
                                    "activity", opaque_reasoning_delta=opaque_reasoning,
                                )
                        if choice.get("finish_reason"):
                            finish_reason = str(choice["finish_reason"])
                            yield ProviderEvent(
                                "finish", finish_reason=finish_reason,
                            )
                        for raw_call in delta.get("tool_calls", []) or []:
                            if not isinstance(raw_call, dict):
                                raise ProviderError(
                                    "model_tool_protocol_failed",
                                    "Model returned an invalid tool call.",
                                )
                            index = raw_call.get("index", 0)
                            if not isinstance(index, int) or isinstance(index, bool) or index < 0:
                                raise ProviderError(
                                    "model_tool_protocol_failed",
                                    "Model returned an invalid tool call.",
                                )
                            current = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                            _merge_stable_tool_fragment(current, "id", raw_call.get("id"))
                            function = raw_call.get("function", {})
                            if not isinstance(function, dict):
                                raise ProviderError(
                                    "model_tool_protocol_failed",
                                    "Model returned an invalid tool call.",
                                )
                            _merge_stable_tool_fragment(current, "name", function.get("name"))
                            arguments = function.get("arguments", "")
                            if isinstance(arguments, str):
                                current["arguments"] += arguments
                            elif isinstance(arguments, dict) and not current["arguments"]:
                                current["arguments"] = json.dumps(
                                    arguments, ensure_ascii=True, separators=(",", ":"),
                                )
                            elif arguments not in (None, ""):
                                raise ProviderError(
                                    "model_tool_protocol_failed",
                                    "Model returned an invalid tool call.",
                                )
            # Usage can arrive after the finish frame. Drain it before failing,
            # and never parse or yield tools from a truncated response.
            validate_provider_completion(streamed_text, finish_reason, {}, require_output=False)
            for index in sorted(calls):
                raw = calls[index]
                try:
                    arguments = json.loads(raw["arguments"] or "{}")
                    internal_name = _internal_tool_name(raw["name"])
                    candidate = ToolCall(raw["id"] or f"openai-{index}", internal_name, arguments)
                    call = candidate if internal_name == "native.web_search" else validate_tool_call(candidate)
                except (json.JSONDecodeError, CoreDataError) as error:
                    raise ProviderError(
                        "model_tool_protocol_failed", "Model returned an invalid tool call.",
                        details={
                            "tool_validation_reason": (
                                "invalid_json" if isinstance(error, json.JSONDecodeError) else "invalid_schema"
                            ),
                            # Early fragment failures and ambiguous batches
                            # cannot certify a safe model correction.
                            "native_tool_correction_eligible": (
                                len(calls) == 1 and finish_reason in {"stop", "tool_calls"}
                            ),
                        },
                    ) from error
                yield ProviderEvent(
                    "native_search_call" if call.name == "native.web_search" else "tool_call",
                    tool_call=call,
                )
        except httpx.ConnectError as error:
            # With redirects disabled, a failed initial connection proves that
            # this generation request was not sent. Do not infer this from an
            # HTTP status or an error after a response was already opened.
            if not response_opened:
                raise ProviderError(
                    "provider_request_not_sent", "The connection failed before the request was sent.",
                    details={"request_outcome": "not_sent"},
                ) from error
            raise ProviderError("connection_reset", "The provider connection was interrupted.") from error
        except httpx.TimeoutException as error:
            raise ProviderError("stream_stalled", "The provider stopped producing output.") from error
        except httpx.NetworkError as error:
            raise ProviderError("connection_reset", "The provider connection was interrupted.") from error

    async def _stream_kimi_formula_search(
        self, model_id: str, messages: list[dict[str, Any]], options: GenerationOptions,
    ) -> AsyncIterator[ProviderEvent]:
        """Run Kimi's current Formula web-search tool flow.

        ``builtin_function.$web_search`` is a legacy path and is ignored by
        current models such as K3.  Formula tools use the ordinary OpenAI
        function-call contract: fetch the declaration, let the model emit a
        function call, execute the Fiber, then continue the same conversation.
        The Fiber result is intentionally kept opaque; it is passed back to
        Kimi but never persisted as a source or exposed in diagnostics.
        """
        formula_uri = "moonshot/web-search:latest"
        timeout = httpx.Timeout(600, connect=15, read=None)
        headers = self._headers()
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                tools_response = await client.get(
                    self._endpoint(f"formulas/{formula_uri}/tools"), headers=headers,
                )
                if tools_response.is_error:
                    await tools_response.aread()
                    if tools_response.status_code in {400, 404, 422}:
                        raise ProviderError(
                            "native_search_protocol_unsupported",
                            "Kimi did not expose the Formula web-search protocol.",
                            details={"support_evidence": "provider_explicit"},
                        )
                    raise _map_http_error(tools_response)
                try:
                    tools_payload = tools_response.json()
                    formula_tools = tools_payload.get("tools") if isinstance(tools_payload, dict) else None
                except ValueError as error:
                    raise ProviderError(
                        "invalid_provider_response", "Kimi returned invalid Formula tool declarations.",
                    ) from error
                if not isinstance(formula_tools, list) or not formula_tools:
                    raise ProviderError(
                        "native_search_protocol_unsupported",
                        "Kimi did not expose the Formula web-search tool.",
                        details={"support_evidence": "provider_explicit"},
                    )

                conversation = _provider_messages(messages)
                if options.native_search_required:
                    conversation = [{
                        "role": "system",
                        "content": (
                            "The user explicitly requested a web search. "
                            "Call the web_search tool before answering."
                        ),
                    }, *conversation]
                for round_no in range(1, 4):
                    payload: dict[str, Any] = {
                        "model": model_id,
                        "messages": conversation,
                        "tools": formula_tools,
                    }
                    if options.max_output_tokens:
                        payload["max_tokens"] = options.max_output_tokens
                    if round_no == 1 and options.native_search_required:
                        payload["tool_choice"] = "required"
                    response = await client.post(
                        self._endpoint("chat/completions"), headers=headers, json=payload,
                    )
                    if response.is_error:
                        await response.aread()
                        raise _map_http_error(response)
                    try:
                        body = response.json()
                        choice = (body.get("choices") or [])[0]
                        message = choice.get("message") or {}
                    except (ValueError, KeyError, IndexError, AttributeError) as error:
                        raise ProviderError(
                            "invalid_provider_response", "Kimi returned an invalid Formula response.",
                        ) from error
                    usage = body.get("usage")
                    if isinstance(usage, dict):
                        yield ProviderEvent("usage", usage={
                            "input_tokens": int(usage.get("prompt_tokens") or 0),
                            "output_tokens": int(usage.get("completion_tokens") or 0),
                            "cached_input_tokens": int(
                                (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
                            ),
                        })
                    tool_calls = message.get("tool_calls") or []
                    if not isinstance(tool_calls, list):
                        raise ProviderError(
                            "model_tool_protocol_failed", "Kimi returned invalid Formula tool calls.",
                        )
                    if not tool_calls:
                        content = message.get("content")
                        if isinstance(content, str) and content:
                            yield ProviderEvent("text_delta", text=content)
                        yield ProviderEvent(
                            "finish", finish_reason=str(choice.get("finish_reason") or "stop"),
                        )
                        return

                    # Keep the assistant tool-call message verbatim enough for
                    # Kimi's continuation contract, while never logging it.
                    assistant_message = {
                        "role": "assistant",
                        "content": message.get("content"),
                        "tool_calls": tool_calls,
                    }
                    conversation.append(assistant_message)
                    for raw_call in tool_calls:
                        if not isinstance(raw_call, dict):
                            raise ProviderError(
                                "model_tool_protocol_failed", "Kimi returned an invalid Formula tool call.",
                            )
                        function = raw_call.get("function")
                        if not isinstance(function, dict) or function.get("name") != "web_search":
                            raise ProviderError(
                                "model_tool_protocol_failed",
                                "Kimi returned an unexpected Formula tool.",
                            )
                        arguments = function.get("arguments")
                        if isinstance(arguments, dict):
                            arguments_text = json.dumps(arguments, ensure_ascii=True, separators=(",", ":"))
                            arguments_value = arguments
                        elif isinstance(arguments, str):
                            arguments_text = arguments
                            try:
                                arguments_value = json.loads(arguments)
                            except ValueError as error:
                                raise ProviderError(
                                    "model_tool_protocol_failed", "Kimi returned invalid web-search arguments.",
                                ) from error
                        else:
                            raise ProviderError(
                                "model_tool_protocol_failed", "Kimi returned invalid web-search arguments.",
                            )
                        if not isinstance(arguments_value, dict):
                            raise ProviderError(
                                "model_tool_protocol_failed", "Kimi returned invalid web-search arguments.",
                            )
                        query = arguments_value.get("query")
                        if not isinstance(query, str) or not query.strip():
                            raise ProviderError(
                                "model_tool_protocol_failed", "Kimi web-search query is missing.",
                            )
                        call_id = str(raw_call.get("id") or "web_search:0")
                        yield ProviderEvent("web_search_started", web_search={
                            "provider_call_id": call_id, "query": query[:500],
                        })
                        fiber = await client.post(
                            self._endpoint(f"formulas/{formula_uri}/fibers"), headers=headers,
                            json={"name": function["name"], "arguments": arguments_text},
                        )
                        if fiber.is_error:
                            await fiber.aread()
                            raise _map_http_error(fiber)
                        try:
                            fiber_body = fiber.json()
                            context = fiber_body.get("context") or {}
                            result = context.get("output") or context.get("encrypted_output")
                        except (ValueError, AttributeError) as error:
                            raise ProviderError(
                                "invalid_provider_response", "Kimi returned an invalid Formula result.",
                            ) from error
                        if not isinstance(result, str) or not result:
                            raise ProviderError(
                                "invalid_provider_response", "Kimi Formula returned an empty search result.",
                            )
                        yield ProviderEvent("web_search_result", web_search={
                            "provider_call_id": call_id,
                            "query": query[:500],
                            "sources": [],
                            "usage": {"search_calls": 1},
                            "provider_reference_id": str(fiber_body.get("id") or call_id),
                        })
                        conversation.append({
                            "role": "tool", "tool_call_id": call_id, "content": result,
                        })
                raise ProviderError(
                    "native_search_continuation_exceeded",
                    "Kimi Formula search did not finish within the bounded continuation limit.",
                )
        except httpx.TimeoutException as error:
            raise ProviderError("stream_stalled", "The provider stopped producing output.") from error
        except httpx.NetworkError as error:
            raise ProviderError("connection_reset", "The provider connection was interrupted.") from error

    async def _stream_responses(
        self, model_id: str, messages: list[dict[str, Any]], options: GenerationOptions,
        *, include_web_search: bool, include_function_tools: bool,
    ) -> AsyncIterator[ProviderEvent]:
        payload: dict[str, Any] = {
            "model": model_id,
            "input": _responses_input(messages),
            "stream": True,
            "store": False,
        }
        response_tools = (
            _responses_function_tools(options.allowed_tools) if include_function_tools else []
        )
        if include_web_search:
            response_tools.insert(0, {"type": "web_search"})
            payload["include"] = ["web_search_call.action.sources"]
            payload["max_tool_calls"] = 3
        if response_tools:
            payload["tools"] = response_tools
            payload["tool_choice"] = (
                {"type": "web_search"}
                if include_web_search and options.native_search_required else "auto"
            )
        if options.max_output_tokens:
            payload["max_output_tokens"] = options.max_output_tokens
        calls: dict[str, dict[str, Any]] = {}
        function_call_item_count = 0
        event_counts: dict[str, int] = {}
        unknown_event_types: set[str] = set()
        streamed_text = ""
        terminal_kind: str | None = None
        terminal_status: str | None = None
        output_item_types: tuple[str, ...] = ()
        refusal_present = False
        usage_present = False
        request_id_hash: str | None = None

        def safe_diagnostic() -> dict[str, Any]:
            return {
                "http_status": 200,
                "request_id_hash": request_id_hash,
                "event_count": sum(event_counts.values()),
                "event_types": dict(sorted(event_counts.items())),
                "text_character_count": len(streamed_text),
                "terminal_event": terminal_kind,
                "terminal_status": terminal_status,
                "output_item_types": list(output_item_types),
                "refusal_present": refusal_present,
                "usage_present": usage_present,
                "unknown_event_types": sorted(unknown_event_types),
            }

        def unseen_text(candidate: Any) -> str:
            nonlocal streamed_text
            if not isinstance(candidate, str) or not candidate:
                return ""
            if not streamed_text:
                streamed_text = candidate
                return candidate
            if candidate.startswith(streamed_text):
                suffix = candidate[len(streamed_text):]
                streamed_text = candidate
                return suffix
            # A final aggregate may repeat the already streamed response with
            # provider-specific normalization. Do not duplicate it.
            return ""

        known_lifecycle_events = {
            "response.created", "response.queued", "response.in_progress",
            "response.output_item.added", "response.content_part.added",
            "response.content_part.done", "response.output_text.done",
            "response.reasoning_summary_part.added", "response.reasoning_summary_part.done",
            "response.reasoning_summary_text.delta", "response.reasoning_summary_text.done",
        }
        try:
            timeout = httpx.Timeout(600, connect=15, read=None)
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                async with client.stream(
                    "POST", self._endpoint("responses"), headers=self._headers(), json=payload,
                ) as response:
                    if response.is_error:
                        await response.aread()
                        raise _map_http_error(response)
                    response_headers = getattr(response, "headers", {})
                    request_id = response_headers.get("x-request-id") \
                        or response_headers.get("request-id")
                    if request_id:
                        request_id_hash = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:16]
                    async for line in response.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if raw == "[DONE]":
                            break
                        try:
                            value = json.loads(raw)
                        except ValueError as error:
                            raise ProviderError(
                                "invalid_provider_response", "OpenAI returned invalid response stream data.",
                            ) from error
                        kind = value.get("type")
                        if not isinstance(kind, str):
                            unknown_event_types.add("<missing>")
                            continue
                        event_counts[kind] = event_counts.get(kind, 0) + 1
                        if kind == "response.output_text.delta" and isinstance(value.get("delta"), str):
                            delta = unseen_text(streamed_text + value["delta"])
                            if delta:
                                yield ProviderEvent("text_delta", text=delta)
                        elif kind == "response.output_text.done":
                            delta = unseen_text(value.get("text"))
                            if delta:
                                yield ProviderEvent("text_delta", text=delta)
                        elif kind in {"response.refusal.delta", "response.refusal.done"}:
                            refusal_present = True
                        elif kind in {
                            "response.web_search_call.in_progress", "response.web_search_call.searching",
                        }:
                            item_id = str(value.get("item_id") or "")
                            yield ProviderEvent("web_search_started", web_search={"provider_call_id": item_id})
                        elif kind == "response.output_item.done":
                            item = value.get("item")
                            if not isinstance(item, dict):
                                continue
                            if item.get("type") == "function_call":
                                function_call_item_count += 1
                                call_id = str(item.get("call_id") or item.get("id") or "")
                                calls[call_id] = item
                            elif item.get("type") == "web_search_call":
                                yield ProviderEvent(
                                    "web_search_result", web_search=_normalize_openai_web_item(item),
                                )
                            elif item.get("type") == "message":
                                final_text, refused, item_types = _responses_output_summary({"output": [item]})
                                refusal_present = refusal_present or refused
                                output_item_types = tuple(sorted(set(output_item_types) | set(item_types)))
                                delta = unseen_text(final_text)
                                if delta:
                                    yield ProviderEvent("text_delta", text=delta)
                        elif kind == "response.completed":
                            terminal_kind = kind
                            completed = value.get("response")
                            if isinstance(completed, dict):
                                terminal_status = str(completed.get("status") or "completed")
                                final_text, refused, item_types = _responses_output_summary(completed)
                                refusal_present = refusal_present or refused
                                output_item_types = tuple(sorted(set(output_item_types) | set(item_types)))
                                delta = unseen_text(final_text)
                                if delta:
                                    yield ProviderEvent("text_delta", text=delta)
                                for search in _openai_web_results(completed):
                                    yield ProviderEvent("web_search_result", web_search=search)
                                usage = completed.get("usage")
                                if isinstance(usage, dict):
                                    usage_present = True
                                    yield ProviderEvent("usage", usage={
                                        "input_tokens": int(usage.get("input_tokens") or 0),
                                        "output_tokens": int(usage.get("output_tokens") or 0),
                                        "cached_input_tokens": int(
                                            (usage.get("input_tokens_details") or {}).get("cached_tokens") or 0
                                        ),
                                    })
                                incomplete = completed.get("incomplete_details")
                                if terminal_status == "incomplete" or isinstance(incomplete, dict):
                                    reason = incomplete.get("reason") if isinstance(incomplete, dict) else None
                                    details = safe_diagnostic()
                                    details["incomplete_reason"] = str(reason or "unknown")
                                    raise ProviderError(
                                        "provider_response_incomplete",
                                        "The provider did not complete the response.",
                                        details=details,
                                    )
                                if refusal_present:
                                    raise ProviderError(
                                        "provider_refusal", "The provider refused this request.",
                                        details=safe_diagnostic(),
                                    )
                        elif kind == "response.incomplete":
                            terminal_kind = kind
                            completed = value.get("response")
                            terminal_status = "incomplete"
                            incomplete = completed.get("incomplete_details") \
                                if isinstance(completed, dict) else None
                            details = safe_diagnostic()
                            details["incomplete_reason"] = str(
                                incomplete.get("reason") if isinstance(incomplete, dict) else "unknown"
                            )
                            raise ProviderError(
                                "provider_response_incomplete",
                                "The provider did not complete the response.",
                                details=details,
                            )
                        elif kind in {"response.failed", "error"}:
                            terminal_kind = kind
                            terminal_status = "failed"
                            raise ProviderError(
                                "provider_response_failed", "The provider reported a failed response.",
                                details=safe_diagnostic(),
                            )
                        elif kind not in known_lifecycle_events:
                            unknown_event_types.add(kind)
            for item in calls.values():
                try:
                    arguments = json.loads(str(item.get("arguments") or "{}"))
                    call = validate_tool_call(ToolCall(
                        str(item.get("call_id") or item.get("id") or "openai-function"),
                        _internal_tool_name(item.get("name")), arguments,
                    ))
                except (json.JSONDecodeError, CoreDataError) as error:
                    raise ProviderError(
                        "model_tool_protocol_failed", "OpenAI returned an invalid function call.",
                        details={
                            "tool_validation_reason": (
                                "invalid_json" if isinstance(error, json.JSONDecodeError) else "invalid_schema"
                            ),
                            "native_tool_correction_eligible": (
                                function_call_item_count == 1 and len(calls) == 1
                                and terminal_kind == "response.completed"
                                and terminal_status == "completed"
                            ),
                        },
                    ) from error
                yield ProviderEvent("tool_call", tool_call=call)
            if not streamed_text and not calls:
                details = safe_diagnostic()
                details["retry_safe"] = bool(
                    terminal_kind == "response.completed"
                    and terminal_status in {None, "completed"}
                    and not usage_present
                    and not refusal_present
                )
                if unknown_event_types:
                    raise ProviderError(
                        "provider_event_unsupported",
                        "The provider returned an unsupported response event.",
                        details=details,
                    )
                raise ProviderError(
                    "provider_empty_output", "The provider completed without usable output.",
                    details=details,
                )
            yield ProviderEvent("finish", finish_reason=terminal_status or "stop")
            yield ProviderEvent("provider_diagnostic", diagnostic=safe_diagnostic())
        except httpx.TimeoutException as error:
            raise ProviderError("stream_stalled", "OpenAI Responses stopped producing output.") from error
        except httpx.NetworkError as error:
            raise ProviderError("connection_reset", "The OpenAI Responses connection was interrupted.") from error


def create_provider(profile: ProviderProfile) -> ModelProvider:
    if profile.kind == "fake":
        return FakeModelProvider(profile)
    if profile.kind == "ollama":
        return OllamaProvider(profile)
    return OpenAICompatibleProvider(profile)


def model_list_base_candidates(base_url: str, endpoint_mode: str) -> list[str]:
    """Return same-origin API bases in the only order discovery may probe them."""
    base = base_url.rstrip("/")
    if endpoint_mode == "legacy_auto_v1":
        if urlparse(base).path.rstrip("/").endswith("/v1"):
            return [base]
        return [f"{base}/v1", base]
    if urlparse(base).path in {"", "/"}:
        return [base, f"{base}/v1"]
    return [base]


def _known_capability(value: Any) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "unknown"


def _external_tool_name(name: str) -> str:
    if name == "native.web_search":
        return "$web_search"
    return name.replace(".", "_")


def _internal_tool_name(name: Any) -> str:
    if not isinstance(name, str):
        raise CoreDataError("invalid_tool_call", "Tool name is invalid.")
    if name == "$web_search":
        return "native.web_search"
    name = "terminal.run" if name == "terminal_run" else name
    candidate = name.replace("fs_", "fs.", 1) if name.startswith("fs_") else name
    candidate = candidate.replace("web_", "web.", 1) if candidate.startswith("web_") else candidate
    candidate = candidate.replace("skills_", "skills.", 1) if candidate.startswith("skills_") else candidate
    return candidate


def _merge_stable_tool_fragment(current: dict[str, str], key: str, value: Any) -> None:
    """Accept set-once metadata and tolerate gateways that repeat it unchanged."""
    if value in (None, ""):
        return
    if not isinstance(value, str):
        raise ProviderError("model_tool_protocol_failed", "Model returned an invalid tool call.")
    if current[key] and current[key] != value:
        raise ProviderError("model_tool_protocol_failed", "Model returned conflicting tool metadata.")
    current[key] = value


def _external_tool_definitions(allowed_tools: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    allowed = set(allowed_tools) if allowed_tools is not None else None
    result = json.loads(json.dumps([
        item for item in TOOL_DEFINITIONS
        if allowed is None or item["function"]["name"] in allowed
    ]))
    for item in result:
        item["function"]["name"] = _external_tool_name(item["function"]["name"])
    return result


def _responses_function_tools(allowed_tools: tuple[str, ...] | None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in _external_tool_definitions(allowed_tools):
        function = item["function"]
        result.append({
            "type": "function",
            "name": function["name"],
            "description": function.get("description", ""),
            "parameters": function.get("parameters", {"type": "object", "properties": {}}),
            # Core performs the authoritative schema validation. Several tools
            # intentionally have optional fields, so OpenAI strict mode would
            # reject otherwise valid schemas unless every optional field were
            # rewritten as required-and-nullable.
            "strict": False,
        })
    return result


def _anthropic_function_tools(allowed_tools: tuple[str, ...] | None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in _external_tool_definitions(allowed_tools):
        function = item["function"]
        result.append({
            "name": function["name"],
            "description": function.get("description", ""),
            "input_schema": function.get("parameters", {"type": "object", "properties": {}}),
        })
    return result


def _responses_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw in messages:
        message = json.loads(json.dumps(raw))
        role = message.get("role")
        if role == "tool":
            result.append({
                "type": "function_call_output",
                "call_id": str(message.get("tool_call_id") or ""),
                "output": str(message.get("content") or ""),
            })
            continue
        calls = message.pop("tool_calls", []) or []
        content = message.get("content")
        if content not in (None, ""):
            result.append({"role": role, "content": responses_content(content)})
        for call in calls:
            function = call.get("function", {}) if isinstance(call, dict) else {}
            result.append({
                "type": "function_call",
                "call_id": str(call.get("id") or ""),
                "name": _external_tool_name(str(function.get("name") or "")),
                "arguments": function.get("arguments") if isinstance(function.get("arguments"), str)
                else json.dumps(function.get("arguments") or {}, ensure_ascii=True, separators=(",", ":")),
            })
    return result


def _responses_output_summary(response: dict[str, Any]) -> tuple[str, bool, tuple[str, ...]]:
    """Extract final Responses output without retaining opaque payloads.

    Compatible gateways do not always emit ``response.output_text.delta``.
    Some only place the final message in ``response.completed.response.output``.
    This helper provides the required terminal fallback and reports only safe
    structural metadata to diagnostics.
    """
    texts: list[str] = []
    refusal_present = False
    item_types: set[str] = set()
    output = response.get("output")
    for item in output if isinstance(output, list) else []:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if isinstance(item_type, str):
            item_types.add(item_type)
        if item_type == "refusal":
            refusal_present = True
        content = item.get("content")
        for part in content if isinstance(content, list) else []:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            if isinstance(part_type, str):
                item_types.add(part_type)
            if part_type in {"refusal", "output_refusal"}:
                refusal_present = True
                continue
            text = part.get("text")
            if part_type in {"output_text", "text"} and isinstance(text, str) and text:
                texts.append(text)
    return "".join(texts), refusal_present, tuple(sorted(item_types))


def _normalize_openai_web_item(item: dict[str, Any]) -> dict[str, Any]:
    action = item.get("action") if isinstance(item.get("action"), dict) else {}
    query = action.get("query") if isinstance(action.get("query"), str) else "Provider-managed search"
    sources: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in action.get("sources", []) if isinstance(action.get("sources"), list) else []:
        if not isinstance(source, dict):
            continue
        url = source.get("url")
        if not isinstance(url, str) or not url.startswith("https://") or url in seen:
            continue
        seen.add(url)
        parsed = urlparse(url)
        sources.append({
            "title": source.get("title") if isinstance(source.get("title"), str) else url,
            "url": url,
            "domain": parsed.hostname or "",
            "snippet": None,
            "published_at": None,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "content_hash": None,
            "provider_source_id": source.get("source_id"),
        })
    return {
        "provider_call_id": str(item.get("id") or ""),
        "query": query,
        "sources": sources,
        "usage": {"search_calls": 1},
    }


def _openai_web_results(response: dict[str, Any]) -> list[dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for item in response.get("output", []) if isinstance(response.get("output"), list) else []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "web_search_call":
            normalized = _normalize_openai_web_item(item)
            results[normalized["provider_call_id"] or f"search-{len(results)}"] = normalized
        if item.get("type") != "message":
            continue
        for content in item.get("content", []) if isinstance(item.get("content"), list) else []:
            if not isinstance(content, dict):
                continue
            for annotation in content.get("annotations", []) \
                    if isinstance(content.get("annotations"), list) else []:
                citation = annotation.get("url_citation") if isinstance(annotation, dict) else None
                if not isinstance(citation, dict):
                    continue
                url = citation.get("url")
                if not isinstance(url, str) or not url.startswith("https://"):
                    continue
                target = next(iter(results.values()), None)
                if target is None:
                    target = {
                        "provider_call_id": "response-citations",
                        "query": "Provider-managed search",
                        "sources": [],
                        "usage": {"search_calls": 1},
                    }
                    results["response-citations"] = target
                if any(source["url"] == url for source in target["sources"]):
                    continue
                target["sources"].append({
                    "title": citation.get("title") if isinstance(citation.get("title"), str) else url,
                    "url": url,
                    "domain": urlparse(url).hostname or "",
                    "snippet": None,
                    "published_at": None,
                    "retrieved_at": datetime.now(timezone.utc).isoformat(),
                    "content_hash": None,
                    "provider_source_id": citation.get("source_id"),
                })
    return list(results.values())


def _provider_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = json.loads(json.dumps(messages))
    for message in result:
        message["content"] = openai_chat_content(message.get("content"))
        for call in message.get("tool_calls", []) or []:
            function = call.get("function", {})
            if "name" in function:
                function["name"] = _external_tool_name(function["name"])
    return result


def _ollama_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate canonical multimodal content into Ollama's image fields."""
    result = json.loads(json.dumps(messages))
    for message in result:
        text, images = ollama_content(message.get("content"))
        message["content"] = text
        if images:
            message["images"] = images
        for call in message.get("tool_calls", []) or []:
            function = call.get("function", {})
            if "name" in function:
                function["name"] = _external_tool_name(function["name"])
    return result


def _plain_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    text = "\n".join(
        str(part.get("text")) for part in content
        if isinstance(part, dict) and part.get("type") == "text"
        and isinstance(part.get("text"), str)
    )
    if contains_images(content):
        return f"{text}\n[image attachment]".strip()
    return text


def _fake_path_after(text: str, markers: tuple[str, ...]) -> str | None:
    lowered = text.casefold()
    for marker in markers:
        index = lowered.find(marker.casefold())
        if index < 0:
            continue
        tail = text[index + len(marker):].strip(" ：:，,。.\"'`")
        if tail:
            return tail.split()[0].strip("，,。.\"'`")
    return None


def _fake_write_request(text: str) -> tuple[str, str] | None:
    lowered = text.casefold()
    for marker in ("写入", "保存到", "write to"):
        index = lowered.find(marker.casefold())
        if index < 0:
            continue
        before = text[:index].strip(" 把将：:，,")
        after = text[index + len(marker):].strip(" ：:，,。.\"'`")
        if before and after:
            return after.split()[0].strip("，,。.\"'`"), before
    return None
