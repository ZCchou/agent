from __future__ import annotations

from collections.abc import AsyncIterator
import asyncio
import time
import logging
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

from agent.execution.compiler import PreparedRequest
from agent.execution.domain import InvocationSpec
from agent.infrastructure.logging import events as log_events, log_event
from agent.providers import (
    closing_provider_stream,
    GenerationOptions, ProviderError, ProviderEvent, validate_provider_completion,
)
from agent.storage import CoreDataError
from agent.local_provider import ManagedLocalProvider


_LOGGER = logging.getLogger("agent.core")


class ModelInvocationService:
    """Single provider-call boundary shared by chat and future specialist steps."""

    @staticmethod
    @asynccontextmanager
    async def provider_session(provider):
        if isinstance(provider, ManagedLocalProvider):
            async with provider.request_session() as inner:
                yield inner
        else:
            yield provider

    @staticmethod
    def stream(
        provider: Any, model_id: str, messages: list[dict[str, Any]], *, tools: bool,
        options: GenerationOptions,
    ) -> AsyncIterator[ProviderEvent]:
        return provider.stream_events(model_id, messages, tools=tools, options=options)

    @staticmethod
    def stream_prepared(
        provider: Any, spec: InvocationSpec, prepared: PreparedRequest,
    ) -> AsyncIterator[ProviderEvent]:
        """Dispatch exactly the adapter/protocol already frozen in ``spec``."""
        if prepared.spec_id != spec.spec_id or prepared.model_id != spec.model_id:
            raise CoreDataError(
                "invocation_spec_invalid", "Prepared request does not belong to its spec.",
            )
        profile = getattr(provider, "profile", None)
        if profile is None:
            raise CoreDataError(
                "invocation_protocol_mismatch", "Provider has no bound invocation profile.",
            )
        actual_protocol = "memory" if profile.kind == "fake" else profile.primary_protocol
        if actual_protocol != prepared.protocol_id:
            raise CoreDataError(
                "invocation_protocol_mismatch",
                "Provider protocol differs from the immutable invocation specification.",
            )
        stream = provider.stream_events(
            prepared.model_id,
            list(prepared.messages),
            tools=prepared.tools_enabled,
            options=prepared.options,
        )
        return stream


    async def complete_prepared(
        self, provider: Any, spec: InvocationSpec, prepared: PreparedRequest,
    ) -> tuple[str, dict[str, int], list[ProviderEvent]]:
        content = ""
        usage: dict[str, int] = {}
        events: list[ProviderEvent] = []
        event_counts: dict[str, int] = {}
        diagnostic: dict[str, Any] = {}
        saw_activity = False
        try:
            async with self.provider_session(provider) as ready_provider:
                started = time.monotonic()
                async with asyncio.timeout(spec.total_timeout_ms / 1000):
                    stream = self.stream_prepared(ready_provider, spec, prepared).__aiter__()
                    async with closing_provider_stream(stream):
                        while True:
                            try:
                                async with asyncio.timeout(spec.stream_wait_seconds(saw_activity, time.monotonic()-started)):
                                    event = await anext(stream)
                            except StopAsyncIteration:
                                break
                            except TimeoutError as error:
                                raise ProviderError(
                                    "stream_stalled" if saw_activity else "request_timeout",
                                    "The model stopped producing activity."
                                    if saw_activity else "The model did not start responding in time.",
                                ) from error
                            if event.kind not in {"provider_diagnostic", "usage"}:
                                saw_activity = True
                            capability_id = (
                                "structured_web_evidence"
                                if "structured_web_evidence" in spec.capability_signature
                                else "web_search"
                                if "native_web_search" in spec.capability_signature
                                else "vision_input"
                                if "vision" in spec.capability_signature
                                else "function_calling"
                                if "function_tools" in spec.capability_signature
                                else "reasoning"
                                if "reasoning" in spec.capability_signature
                                else "text_generation"
                            )
                            event = replace(
                                event, run_id=spec.run_id or spec.plan_id, step_id=spec.step_id,
                                capability_id=capability_id, adapter_id=spec.adapter_id,
                                route_id=spec.route_id, sequence=len(events),
                            )
                            event_counts[event.kind] = event_counts.get(event.kind, 0) + 1
                            if event.kind == "provider_diagnostic":
                                diagnostic.update(event.diagnostic or {})
                                continue
                            events.append(event)
                            if event.kind == "text_delta" and event.text:
                                content += event.text
                            elif event.kind == "usage" and event.usage:
                                usage.update(event.usage)
                    validate_provider_completion(
                        content, next((event.finish_reason for event in reversed(events)
                                       if event.kind == "finish"), None), usage,
                        has_semantic_output=any(event.kind in {
                            "tool_call", "native_search_call", "web_search_result",
                        } for event in events),
                    )
        except ProviderError as error:
            error.usage = dict(usage)
            diagnostic_fields = dict(error.details)
            diagnostic_fields.update({
                "normalized_event_types": event_counts,
                "text_character_count": len(content),
            })
            log_event(
                _LOGGER, logging.WARNING, log_events.MODEL_RESPONSE_DIAGNOSTIC,
                "Provider invocation ended without a valid model result",
                spec_id=spec.spec_id, plan_id=spec.plan_id, step_id=spec.step_id,
                provider_profile_id=spec.provider_or_backend_id, model_id=spec.model_id,
                route_id=spec.route_id, protocol_id=spec.protocol_id,
                adapter_id=spec.adapter_id, error_code=error.code,
                **diagnostic_fields,
            )
            raise
        except TimeoutError as error:
            raise ProviderError("request_timeout", "The model invocation timed out.") from error
        diagnostic_fields = dict(diagnostic)
        diagnostic_fields.update({
            "normalized_event_types": event_counts,
            "text_character_count": len(content),
        })
        log_event(
            _LOGGER, logging.DEBUG, log_events.MODEL_RESPONSE_DIAGNOSTIC,
            "Provider invocation response shape accepted",
            spec_id=spec.spec_id, plan_id=spec.plan_id, step_id=spec.step_id,
            provider_profile_id=spec.provider_or_backend_id, model_id=spec.model_id,
            route_id=spec.route_id, protocol_id=spec.protocol_id,
            adapter_id=spec.adapter_id, **diagnostic_fields,
        )
        return content, usage, events

    async def complete_text(
        self, provider: Any, model_id: str, messages: list[dict[str, Any]], *,
        max_output_tokens: int = 1024,
    ) -> tuple[str, dict[str, int]]:
        content, usage, _ = await self.complete_with_events(
            provider, model_id, messages,
            options=GenerationOptions(purpose="chat", max_output_tokens=max_output_tokens),
        )
        return content, usage

    async def complete_with_events(
        self, provider: Any, model_id: str, messages: list[dict[str, Any]], *,
        options: GenerationOptions,
    ) -> tuple[str, dict[str, int], list[ProviderEvent]]:
        """Run a bounded hidden invocation while preserving normalized audit events."""
        content = ""
        usage: dict[str, int] = {}
        events: list[ProviderEvent] = []
        try:
            async with asyncio.timeout(90):
                stream = self.stream(provider, model_id, messages, tools=False, options=options).__aiter__()
                async with closing_provider_stream(stream):
                    while True:
                        try:
                            async with asyncio.timeout(30):
                                event = await anext(stream)
                        except StopAsyncIteration:
                            break
                        except TimeoutError as error:
                            raise ProviderError(
                                "stream_stalled", "The specialist model stopped producing activity."
                            ) from error
                        events.append(event)
                        if event.kind == "text_delta" and event.text:
                            content += event.text
                        elif event.kind == "usage" and event.usage:
                            usage.update(event.usage)
                validate_provider_completion(
                    content, next((event.finish_reason for event in reversed(events)
                                   if event.kind == "finish"), None), usage,
                    has_semantic_output=any(event.kind in {
                        "tool_call", "native_search_call", "web_search_result",
                    } for event in events),
                )
        except ProviderError as error:
            error.usage = dict(usage)
            raise
        except TimeoutError as error:
            raise ProviderError("request_timeout", "The specialist model timed out.") from error
        return content, usage, events
