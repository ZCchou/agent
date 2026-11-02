from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from agent.execution.domain import stable_hash

FollowUpActionType = Literal["web_search"]
FollowUpStatus = Literal["pending", "consumed", "rejected", "expired"]
SemanticFollowUpAction = Literal["offer_confirmation", "confirm", "reject", "none"]

_CONFIRMATIONS = {
    "好", "好的", "可以", "可以的", "行", "行的", "查吧", "搜吧", "继续", "确认",
    "yes", "yeah", "ok", "okay", "sure", "go ahead", "please do", "continue",
}
_REJECTIONS = {
    "不用", "不要", "别搜", "别搜索", "不要搜", "算了", "先不用", "取消", "不需要",
    "no", "no thanks", "cancel", "stop", "don't", "do not",
}
_SEARCH_OFFER_PATTERNS = (
    re.compile(
        r"(?:要不要|是否需要|需要我|要我|可以帮你|我可以|我也可以|需要的话).{0,32}"
        r"(?:联网|上网|搜索|搜一下|搜一次|搜一遍|实际搜|核对|核验|核实|查证|验证|查一下|查一次|查一遍)"
    ),
    re.compile(r"(?:would you like|shall i|i can|i could).{0,48}(?:search|look up|browse|verify)", re.I),
)


@dataclass(frozen=True)
class PendingFollowUpAction:
    id: str
    conversation_id: str
    source_run_id: str
    source_message_id: str
    action_type: FollowUpActionType
    query_hint: str
    purpose: str
    evidence: Literal["optional", "required"]
    expires_at: str
    status: FollowUpStatus = "pending"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PendingFollowUpAction":
        return cls(
            id=str(value["id"]), conversation_id=str(value["conversation_id"]),
            source_run_id=str(value["source_run_id"]),
            source_message_id=str(value["source_message_id"]),
            action_type=value["action_type"], query_hint=str(value["query_hint"]),
            purpose=str(value["purpose"]), evidence=value["evidence"],
            expires_at=str(value["expires_at"]), status=value.get("status", "pending"),
        )

    @property
    def expired(self) -> bool:
        value = self.expires_at.replace("Z", "+00:00")
        return datetime.now(timezone.utc) >= datetime.fromisoformat(value)

    def with_status(self, status: FollowUpStatus) -> "PendingFollowUpAction":
        return replace(self, status=status)

    @property
    def hash(self) -> str:
        return stable_hash(self.to_dict())


@dataclass(frozen=True)
class SemanticFollowUpIntent:
    action: SemanticFollowUpAction
    capability: Literal["web_search", "none"]
    query_hint: str | None
    evidence: Literal["optional", "required"]
    confidence: float

    @property
    def actionable(self) -> bool:
        return self.action != "none" and self.confidence >= 0.65


def should_probe_semantic_follow_up(text: str) -> bool:
    """Cheap recall gate; the model, not this list, makes the final decision."""
    normalized = text.casefold()
    return any(marker in normalized for marker in (
        "要不要", "如果你愿意", "可以帮你", "核验", "核对", "核实", "查证",
        "验证", "浏览", "搜", "搜索", "查询", "查", "联网", "官网", "来源",
        "当前信息", "would you like", "i can", "search", "look up", "browse", "verify",
        "check", "current", "source", "?", "？",
    ))


def pending_action_from_semantic(
    intent: SemanticFollowUpIntent, *, user_request: str,
    source_run_id: str, source_message_id: str,
    now: datetime | None = None,
) -> PendingFollowUpAction | None:
    if not intent.actionable or intent.action != "offer_confirmation" \
            or intent.capability != "web_search" or not user_request.strip():
        return None
    current = now or datetime.now(timezone.utc)
    expires_at = (current + timedelta(minutes=10)).isoformat(
        timespec="milliseconds",
    ).replace("+00:00", "Z")
    return PendingFollowUpAction(
        id="", conversation_id="", source_run_id=source_run_id,
        source_message_id=source_message_id, action_type="web_search",
        query_hint=(intent.query_hint or user_request).strip()[:8_000],
        purpose="核对当前信息并返回可核验来源", evidence=intent.evidence,
        expires_at=expires_at,
    )


def parse_semantic_follow_up(content: str) -> SemanticFollowUpIntent:
    """Parse the model-only follow-up contract without accepting authority fields."""
    raw = content.strip()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        # Some OpenAI-compatible endpoints ignore response_format and wrap the
        # requested object in prose or a markdown fence. Recover only a JSON
        # object; the strict field validation below remains authoritative.
        value = None
        decoder = json.JSONDecoder()
        for index, character in enumerate(raw):
            if character != "{":
                continue
            try:
                candidate, _ = decoder.raw_decode(raw[index:])
            except json.JSONDecodeError:
                continue
            value = candidate
            break
        if value is None:
            raise
    if not isinstance(value, dict):
        raise ValueError("follow-up intent must be an object")
    allowed = {"action", "capability", "query_hint", "evidence", "confidence"}
    if set(value) - allowed:
        raise ValueError("follow-up intent contains unsupported fields")
    action = value.get("action", "none")
    capability = value.get("capability", "none")
    query_hint = value.get("query_hint")
    evidence = value.get("evidence", "optional")
    confidence = value.get("confidence", 0.0)
    if not isinstance(action, str) \
            or not isinstance(capability, str) \
            or not isinstance(evidence, str) \
            or action not in {"offer_confirmation", "confirm", "reject", "none"} \
            or capability not in {"web_search", "none"} \
            or evidence not in {"optional", "required"} \
            or isinstance(confidence, bool) \
            or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1 \
            or (query_hint is not None and (
                not isinstance(query_hint, str) or len(query_hint.strip()) > 8_000
            )):
        raise ValueError("follow-up intent fields are invalid")
    if action in {"offer_confirmation", "confirm", "reject"} and capability != "web_search":
        raise ValueError("follow-up action requires web_search capability")
    if action == "offer_confirmation" and (
        not isinstance(query_hint, str) or not query_hint.strip()
    ):
        raise ValueError("search offers require a query hint")
    return SemanticFollowUpIntent(
        action=action, capability=capability, query_hint=query_hint.strip()
        if isinstance(query_hint, str) else None,
        evidence=evidence, confidence=float(confidence),
    )


def confirmation_kind(user_text: str) -> Literal["confirm", "reject", "none"]:
    normalized = " ".join(user_text.casefold().strip().split())
    if normalized in _REJECTIONS:
        return "reject"
    if normalized in _CONFIRMATIONS:
        return "confirm"
    return "none"


def resolve_pending_confirmation(
    *, user_text: str, action: PendingFollowUpAction | None,
) -> Literal["confirm", "reject", "none"]:
    decision = confirmation_kind(user_text)
    if action is None or action.expired:
        return "none"
    return decision


def detect_web_search_offer(
    *, assistant_text: str, user_request: str, source_run_id: str,
    source_message_id: str, now: datetime | None = None,
) -> PendingFollowUpAction | None:
    """Create a candidate only for an explicit assistant search offer.

    The detector never executes a search. It only records the original user
    request as a query hint for the next confirmation turn.
    """
    if not assistant_text.strip() or not user_request.strip():
        return None
    if not any(pattern.search(assistant_text) for pattern in _SEARCH_OFFER_PATTERNS):
        return None
    current = now or datetime.now(timezone.utc)
    expires_at = (current + timedelta(minutes=10)).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z",
    )
    return PendingFollowUpAction(
        id="",
        conversation_id="",
        source_run_id=source_run_id,
        source_message_id=source_message_id,
        action_type="web_search",
        query_hint=user_request.strip()[:8_000],
        purpose="核对当前信息并返回可核验来源",
        evidence="required",
        expires_at=expires_at,
    )
