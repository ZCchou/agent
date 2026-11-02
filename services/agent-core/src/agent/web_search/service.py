from __future__ import annotations

from typing import Any

from agent.web_search.domain import (
    NativeSearchStatus,
    WebAccessPolicy,
    WebSearchCandidate,
    WebSearchPolicyError,
    WebSearchRequestMode,
)
from agent.web_search.policy import WebPermissionContext, enforce_web_permission
from agent.web_search.routing import resolve_web_search_route
from agent.web_search.native import kimi_native_compatible


class WebSearchService:
    """Small orchestration boundary; Host remains the authority for external network I/O."""

    def authorize(
        self, *, params: dict[str, Any], conversation: dict[str, Any],
        required: bool = False,
    ) -> WebSearchRequestMode:
        """Validate permission only; execution planning owns route selection."""
        try:
            requested = params.get("web_search_request_mode")
            if requested is None:
                requested = conversation.get("web_search_mode", "inherit")
            if required and requested == "off":
                raise WebSearchPolicyError(
                    "web_search_not_enabled",
                    "This conversation has web search explicitly disabled.",
                )
            if requested == "inherit":
                # A textual search request under inherited settings still needs
                # authorization.  If global consent is absent, enforcing AUTO
                # below returns the consent-required state instead of silently
                # downgrading the request to a plain answer.
                requested = (
                    "auto" if required or params.get("web_search_global_enabled") is True
                    else "off"
                )
            request_mode = WebSearchRequestMode(requested)
            enforce_web_permission(WebPermissionContext(
                global_enabled=params.get("web_search_global_enabled") is True,
                request_mode=request_mode,
                conversation_kind=conversation["kind"],
                project_policy=WebAccessPolicy(
                    conversation.get("web_access_policy") or "disabled"
                ),
            ))
            return request_mode
        except WebSearchPolicyError:
            raise
        except ValueError as error:
            raise WebSearchPolicyError(
                "invalid_web_search_mode", "Web search policy is invalid."
            ) from error

    def decide(
        self,
        *,
        params: dict[str, Any],
        conversation: dict[str, Any],
        native_status: str,
        native_adapter: str,
        native_compatible: bool | None,
        host_backend_id: str | None,
        tool_mode: str,
        reasoning_enabled: bool = False,
    ):
        try:
            requested = params.get("web_search_request_mode")
            if requested is None:
                requested = conversation.get("web_search_mode", "inherit")
            if requested == "inherit":
                requested = "auto" if params.get("web_search_global_enabled") is True else "off"
            request_mode = WebSearchRequestMode(requested)
            enforce_web_permission(WebPermissionContext(
                global_enabled=params.get("web_search_global_enabled") is True,
                request_mode=request_mode,
                conversation_kind=conversation["kind"],
                project_policy=WebAccessPolicy(conversation.get("web_access_policy") or "disabled"),
            ))
            compatible = native_compatible
            if compatible is None:
                compatible = not (
                    native_adapter == "kimi_builtin_web_search"
                    and not kimi_native_compatible(reasoning_enabled=reasoning_enabled)
                )
            return resolve_web_search_route(request_mode, WebSearchCandidate(
                native_status=NativeSearchStatus(native_status),
                native_adapter=native_adapter,
                native_compatible=compatible,
                host_backend_id=host_backend_id,
                tool_mode=tool_mode,
            ))
        except WebSearchPolicyError:
            # Keep web-search policy independent from the persistence/runtime
            # error hierarchy. The run boundary translates this domain error.
            raise
        except ValueError as error:
            raise WebSearchPolicyError(
                "invalid_web_search_mode", "Web search policy is invalid."
            ) from error
