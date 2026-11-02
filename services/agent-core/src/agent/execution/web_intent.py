from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from agent.execution.domain import stable_hash
from agent.execution.follow_up import PendingFollowUpAction, resolve_pending_confirmation


_REQUIRED_MARKERS = (
    "联网", "上网", "搜索", "搜一下", "查找", "检索", "最新", "今天", "目前", "近期", "最近",
    "实时", "本周", "本月", "近几天", "近一周", "近两周", "最近两周", "过去两周",
    "最近一个月", "近一个月", "截至", "新闻", "天气", "价格", "汇率", "赛程", "版本",
    "发布", "官方公告", "最新论文", "http://", "https://", "search", "look up", "browse",
    "latest", "today", "currently", "recent", "last week", "last two weeks", "past two weeks",
    "last month", "past month", "real-time", "realtime", "news", "weather", "price", "exchange rate",
    "schedule", "release", "official announcement", "latest paper",
)
_DISABLED_MARKERS = (
    "不要联网", "别联网", "无需联网", "不用联网", "不要上网", "别上网",
    "不要搜索", "别搜索", "无需搜索", "不用搜索",
    "do not search", "don't search", "without web", "no web search",
)
_STATIC_MARKERS = (
    "翻译", "润色", "改写", "总结上文", "根据上文", "写一首", "创作", "解释这个概念",
    "translate", "rewrite", "polish", "summarize the above", "write a poem",
)
_CORRECTION_MARKERS = (
    "今年", "年份", "日期", "时间不对", "说错了", "不准确", "更正", "纠正", "其实是",
    "应该是", "不是", "我说的是", "year", "date", "that's wrong", "correction", "actually",
)
_FOLLOW_UP_MARKERS = (
    "那", "那么", "这个", "那个", "第二个", "第三个", "再找", "还有", "继续", "它", "他们",
    "呢", "详细点", "来源呢", "官网吗", "what about", "that one", "more", "continue", "source",
)


def should_probe_semantic_web_intent(content: str) -> bool:
    """Return whether automatic mode should ask the model about web relevance.

    This is a recall gate only. The advisor still decides whether the request
    actually needs web research; it cannot select a route or change permission.
    """
    normalized = content.casefold().strip()
    if not normalized or any(marker in normalized for marker in _DISABLED_MARKERS):
        return False
    if any(marker in normalized for marker in _STATIC_MARKERS) \
            and not any(marker in normalized for marker in _REQUIRED_MARKERS):
        return False
    return any(marker in normalized for marker in (
        "?", "？", "什么", "哪些", "哪个", "哪里", "怎么", "谁", "是否", "推荐",
        "出处", "热梗", "官网", "来源", "what", "which", "where", "how", "who",
        "recommend", "origin", "meme", "source",
    ))


def should_probe_contextual_web_intent(
    content: str, recent_messages: list[dict[str, str]],
) -> bool:
    """Recall short contextual turns for model-based capability judgement.

    This intentionally does not decide that web search is needed. A terse
    continuation can inherit many kinds of intent, so it only opens the
    bounded semantic-advisor path; permissions and route selection remain in
    the planner.
    """
    normalized = " ".join(content.casefold().strip().split())
    if not normalized or not recent_messages:
        return False
    if any(marker in normalized for marker in _DISABLED_MARKERS):
        return False
    return len(normalized) <= 64 and "\n" not in normalized


@dataclass(frozen=True)
class ResolvedWebIntent:
    requirement: Literal["off", "optional", "required"] = "off"
    freshness: Literal["static", "current"] = "static"
    evidence: Literal["optional", "required"] = "optional"
    relation: Literal["new_topic", "follow_up", "correction", "continuation"] = "new_topic"
    reason_code: str = "web_not_required_for_request"
    inherited_from_run_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def hash(self) -> str:
        return stable_hash(self.to_dict())


class WebIntentResolver:
    """Resolve web intent without choosing a model, route, provider, or permission."""

    max_research_turns = 3

    @staticmethod
    def is_explicit_current_request(content: str) -> bool:
        normalized = content.casefold().strip()
        return any(marker in normalized for marker in _REQUIRED_MARKERS)

    @staticmethod
    def _active_research(recent: list[dict[str, Any]]) -> dict[str, Any] | None:
        for item in recent[:WebIntentResolver.max_research_turns]:
            resolved = item.get("resolved_intent")
            if not isinstance(resolved, dict):
                continue
            requirement = resolved.get("requirement", resolved.get("web"))
            if requirement == "required" or item.get("search_executed") is True:
                return item
        return None

    def resolve(
        self, *, content: str, request_mode: str,
        recent: list[dict[str, Any]] | None = None,
        pending_action: PendingFollowUpAction | None = None,
        pending_decision: Literal["confirm", "reject", "none"] | None = None,
    ) -> ResolvedWebIntent:
        normalized = content.casefold().strip()
        if any(marker in normalized for marker in _DISABLED_MARKERS):
            return ResolvedWebIntent(reason_code="user_explicitly_disabled_web")
        pending_decision = pending_decision or resolve_pending_confirmation(
            user_text=content, action=pending_action,
        )
        if pending_decision == "confirm" and pending_action is not None:
            if request_mode == "off":
                return ResolvedWebIntent(reason_code="web_search_off")
            return ResolvedWebIntent(
                "required", "current", pending_action.evidence, "continuation",
                "confirmed_pending_web_search", pending_action.source_run_id,
            )
        if pending_decision == "reject":
            return ResolvedWebIntent(reason_code="rejected_pending_web_search")
        if request_mode == "off":
            return ResolvedWebIntent(reason_code="web_search_off")
        if request_mode == "force_once":
            return ResolvedWebIntent(
                "required", "current", "required", "new_topic", "user_forced_search",
            )
        if self.is_explicit_current_request(content):
            return ResolvedWebIntent(
                "required", "current", "required", "new_topic", "explicit_current_information",
            )
        if any(marker in normalized for marker in _STATIC_MARKERS):
            return ResolvedWebIntent(reason_code="explicit_static_request")

        active = self._active_research(recent or [])
        if active is None:
            return ResolvedWebIntent(reason_code="web_not_required_for_request")
        inherited = active.get("run_id") if isinstance(active.get("run_id"), str) else None
        if any(marker in normalized for marker in _CORRECTION_MARKERS):
            return ResolvedWebIntent(
                "required", "current", "required", "correction",
                "research_context_correction", inherited,
            )
        if any(marker in normalized for marker in _FOLLOW_UP_MARKERS):
            return ResolvedWebIntent(
                "optional", "current", "required", "follow_up",
                "research_context_ambiguous_follow_up", inherited,
            )
        return ResolvedWebIntent(reason_code="research_context_topic_changed")
