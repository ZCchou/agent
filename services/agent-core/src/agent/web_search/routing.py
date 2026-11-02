from __future__ import annotations

from agent.web_search.domain import (
    NativeSearchStatus,
    WebSearchCandidate,
    WebSearchDecision,
    WebSearchPolicyError,
    WebSearchRequestMode,
    WebSearchRoute,
)


def resolve_web_search_route(
    request_mode: WebSearchRequestMode,
    candidate: WebSearchCandidate,
) -> WebSearchDecision:
    if request_mode is WebSearchRequestMode.OFF:
        return WebSearchDecision(request_mode, WebSearchRoute.NONE, None, "web_search_off")

    if candidate.native_status in {NativeSearchStatus.DECLARED, NativeSearchStatus.VERIFIED} \
            and candidate.native_adapter != "none" and candidate.native_compatible:
        return WebSearchDecision(
            request_mode, WebSearchRoute.NATIVE,
            candidate.native_adapter,
            "native_search_verified" if candidate.native_status is NativeSearchStatus.VERIFIED
            else "native_search_declared",
        )

    if candidate.host_backend_id:
        if request_mode is WebSearchRequestMode.FORCE_ONCE:
            return WebSearchDecision(
                request_mode, WebSearchRoute.HOST_PRESEARCH,
                candidate.host_backend_id, "user_forced_host_presearch",
            )
        if candidate.tool_mode != "text_only":
            return WebSearchDecision(
                request_mode, WebSearchRoute.HOST_TOOL,
                candidate.host_backend_id, "native_unavailable_host_tool",
            )
        raise WebSearchPolicyError(
            "native_web_search_unsupported",
            "This text-only model can search only when the user requests search for one message.",
        )

    if candidate.native_status is NativeSearchStatus.TEMPORARY_ERROR:
        raise WebSearchPolicyError(
            "external_search_setup_required",
            "Native web search is temporarily unavailable and no external search backend is configured.",
        )
    raise WebSearchPolicyError(
        "external_search_setup_required",
        "No usable native or external web-search route is configured.",
    )
