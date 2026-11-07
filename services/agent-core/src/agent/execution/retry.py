from __future__ import annotations

from collections.abc import Iterable


TRANSIENT_GENERATION_ERRORS = frozenset({
    "request_timeout",
    "stream_stalled",
    "connection_reset",
    "provider_unavailable",
    "network_unavailable",
})
READ_ONLY_TOOL_NAMES = frozenset({"fs.list", "fs.search", "fs.read", "rag.search", "rag.read"})
MAX_TRANSIENT_RETRIES = 2


def should_retry_transient_generation(
    *,
    error_code: str,
    produced_output: bool,
    tool_names: Iterable[str],
    retry_count: int,
) -> bool:
    """Return whether replaying a failed generation is safe and bounded.

    A retry may repeat a clean generation or read-only project inspection. Any
    output or non-read-only operation makes replay ambiguous, so it is refused.
    """
    if error_code not in TRANSIENT_GENERATION_ERRORS:
        return False
    if produced_output or retry_count >= MAX_TRANSIENT_RETRIES:
        return False
    return all(name in READ_ONLY_TOOL_NAMES for name in tool_names)
