from __future__ import annotations

from dataclasses import dataclass

from agent.web_search.domain import WebAccessPolicy, WebSearchPolicyError, WebSearchRequestMode


@dataclass(frozen=True)
class WebPermissionContext:
    global_enabled: bool
    request_mode: WebSearchRequestMode
    conversation_kind: str
    project_policy: WebAccessPolicy = WebAccessPolicy.DISABLED


def enforce_web_permission(context: WebPermissionContext) -> None:
    if context.request_mode is WebSearchRequestMode.OFF:
        return
    if not context.global_enabled:
        raise WebSearchPolicyError(
            "web_privacy_consent_required",
            "Confirm web-search privacy before searching.",
        )
    if context.conversation_kind == "project_work" \
            and context.project_policy is not WebAccessPolicy.ALLOW_SEARCH:
        raise WebSearchPolicyError(
            "project_web_consent_required", "Allow web search for this project first."
        )
