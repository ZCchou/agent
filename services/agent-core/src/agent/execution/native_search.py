from __future__ import annotations

from collections.abc import Iterable

from agent.providers import ProviderEvent
from agent.storage import CoreDataError
from agent.tool_contracts import ToolCall


class NativeSearchExecutor:
    """Validate provider-managed search without routing it through Host tools."""

    max_continuation_rounds = 3

    @staticmethod
    def accept_event(
        event: ProviderEvent, capability_signature: Iterable[str],
    ) -> ToolCall | None:
        if event.kind != "native_search_call":
            return None
        call = event.tool_call
        if call is None or call.name != "native.web_search" \
                or "native_web_search" not in capability_signature:
            raise CoreDataError(
                "native_search_event_unauthorized",
                "The provider emitted native search outside this invocation spec.",
            )
        return call

    def calls(
        self, events: Iterable[ProviderEvent], capability_signature: Iterable[str],
    ) -> list[ToolCall]:
        return [
            call for event in events
            if (call := self.accept_event(event, capability_signature)) is not None
        ]

    def require_another_round(self, completed_rounds: int) -> int:
        next_count = completed_rounds + 1
        if next_count >= self.max_continuation_rounds:
            raise CoreDataError(
                "native_search_continuation_exceeded",
                "The provider did not finish native search within three rounds.",
            )
        return next_count
