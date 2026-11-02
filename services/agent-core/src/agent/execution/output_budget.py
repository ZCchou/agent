from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


OutputPurpose = Literal[
    "context_compaction",
    "intent_analysis",
    "vision_analysis",
    "web_research",
    "reasoning_analysis",
    "final_response",
]

POLICY_VERSION = "invocation-output-budget-v2"

_CONSERVATIVE_FINAL_TOKENS = 4_096
_NORMAL_FINAL_TOKENS = 16_384
FALLBACK_CONTEXT_WINDOW = 8_192


@dataclass(frozen=True)
class OutputBudget:
    """The single output-token decision shared by planning and execution."""

    purpose: OutputPurpose
    initial_tokens: int
    retry_tokens: int | None
    model_limit: int
    policy_version: str = POLICY_VERSION

    @property
    def reservation_tokens(self) -> int:
        """Largest single response that the already-packed context must fit."""

        return self.retry_tokens or self.initial_tokens

    @property
    def maximum_billed_output_tokens(self) -> int:
        """Worst-case output allowance across the initial call and safe retry."""

        return self.initial_tokens + (self.retry_tokens or 0)


_PURPOSE_LIMITS: dict[OutputPurpose, tuple[int | None, int | None]] = {
    "context_compaction": (8_192, None),
    "intent_analysis": (256, None),
    "vision_analysis": (4_096, 8_192),
    "web_research": (4_096, None),
    "reasoning_analysis": (8_192, None),
    "final_response": (None, None),
}


def _positive_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def resolve_output_budget(
    purpose: OutputPurpose,
    *,
    model_limit: object,
    protocol_limit: object = None,
    remaining_context_tokens: object = None,
    context_window: object = None,
) -> OutputBudget:
    """Resolve one auditable limit without provider- or vendor-specific branches.

    Model configuration is the hard ceiling.  Purpose policy controls normal
    spend, while optional protocol/context ceilings can only reduce it.
    """

    for value in (model_limit, protocol_limit, remaining_context_tokens):
        if isinstance(value, int) and not isinstance(value, bool) and value <= 0:
            raise ValueError("The known output ceiling leaves no token budget.")
    fallback = 8_192 if purpose == "context_compaction" else _CONSERVATIVE_FINAL_TOKENS
    window = _positive_int(context_window)
    if purpose == "final_response" and window is None:
        window = FALLBACK_CONTEXT_WINDOW
    if purpose == "final_response" and window is not None:
        # Leave the existing input headroom on small models. Large-context
        # coding models need room for reasoning, tool arguments and the answer
        # even when discovery did not provide an output-capacity ceiling.
        fallback = (_NORMAL_FINAL_TOKENS if window >= _NORMAL_FINAL_TOKENS * 4
                    else min(_CONSERVATIVE_FINAL_TOKENS, max(512, window // 4)))
    configured_model_limit = _positive_int(model_limit) or fallback
    ceilings = [configured_model_limit]
    if purpose == "final_response" and window is not None:
        ceilings.append(max(1, window // 4))
    for value in (protocol_limit, remaining_context_tokens):
        parsed = _positive_int(value)
        if parsed is not None:
            ceilings.append(parsed)
    hard_ceiling = min(ceilings)
    initial_policy, retry_policy = _PURPOSE_LIMITS[purpose]
    initial = min(initial_policy or hard_ceiling, hard_ceiling)
    retry = min(retry_policy, hard_ceiling) if retry_policy is not None else None
    if retry is not None and retry <= initial:
        retry = None
    return OutputBudget(
        purpose=purpose,
        initial_tokens=initial,
        retry_tokens=retry,
        model_limit=configured_model_limit,
    )


def is_zero_output_budget_exhaustion(error_code: str, details: object) -> bool:
    if error_code != "provider_response_incomplete" or not isinstance(details, dict):
        return False
    reason = str(details.get("incomplete_reason") or "").casefold()
    text_characters = _positive_int(details.get("text_character_count")) or 0
    return reason in {"max_output_tokens", "max_tokens", "length"} and text_characters == 0
