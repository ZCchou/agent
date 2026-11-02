from __future__ import annotations

from typing import Any

from agent.execution.domain import CapabilityRequirements
from agent.execution.intent import IntentResolution
from agent.execution.web_intent import WebIntentResolver

from .requirements import (
    _FILE_READ_MARKERS,
    _FILE_WRITE_MARKERS,
    needs_reasoning_intent_analysis,
    resolve_project_file_requirement,
)


class RuleIntentResolver:
    """Resolve explicit, safety-relevant capability signals without a model."""

    def __init__(self, web_resolver: WebIntentResolver | None = None) -> None:
        self.web_resolver = web_resolver or WebIntentResolver()

    def resolve(
        self, *, text: str, conversation_kind: str, request_mode: str,
        attachments: list[dict[str, Any]], recent: list[dict[str, Any]],
        minimum_context_tokens: int | None,
    ) -> IntentResolution:
        web = self.web_resolver.resolve(
            content=text, request_mode=request_mode, recent=recent,
        )
        files, tools = resolve_project_file_requirement(text, conversation_kind)
        has_image = any(
            item.get("kind") == "image" for item in attachments
            if isinstance(item, dict)
        )
        requirements = CapabilityRequirements(
            vision="required" if has_image else "none",
            web=web.requirement,
            web_evidence=web.evidence,
            files=files,
            tools=tools,
            reasoning="standard",
            minimum_context_tokens=minimum_context_tokens,
        )
        normalized = text.casefold()
        signals: list[str] = []
        if has_image:
            signals.append("vision_input")
        if web.requirement == "required":
            signals.append("explicit_web_request")
        if any(marker in normalized for marker in _FILE_WRITE_MARKERS):
            signals.append("file_write")
        elif any(marker in normalized for marker in _FILE_READ_MARKERS):
            signals.append("file_read")
        if needs_reasoning_intent_analysis(text):
            signals.append("reasoning_candidate")
        if web.reason_code == "user_explicitly_disabled_web":
            signals.append("web_disabled")
        confidence = "high" if any((
            has_image,
            web.requirement == "required",
            files in {"read", "write"},
        )) else "medium" if conversation_kind == "project_work" else "low"
        return IntentResolution(
            requirements=requirements,
            source="rules",
            confidence=confidence,
            signals=tuple(dict.fromkeys(signals)),
            explanation_code=web.reason_code if web.requirement != "off"
            else "explicit_file_or_capability_rules",
        )
