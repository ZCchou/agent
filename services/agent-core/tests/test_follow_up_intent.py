from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent.execution.follow_up import (
    PendingFollowUpAction, detect_web_search_offer, parse_semantic_follow_up,
    resolve_pending_confirmation,
)
from agent.execution.web_intent import WebIntentResolver

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
ACTION = PendingFollowUpAction(
    id="action-1", conversation_id="conversation-1", source_run_id="run-1",
    source_message_id="message-1", action_type="web_search",
    query_hint="西安事变纪念馆官网", purpose="核对官方域名",
    evidence="required", expires_at="2099-01-01T00:00:00Z",
)


def test_explicit_search_offer_creates_pending_action() -> None:
    action = detect_web_search_offer(
        assistant_text="要不要我现在实际联网搜一次并核对官网域名？",
        user_request="帮我找西安事变纪念馆官网", source_run_id="run-1",
        source_message_id="message-1", now=NOW,
    )

    assert action is not None
    assert action.action_type == "web_search"
    assert action.query_hint == "帮我找西安事变纪念馆官网"
    assert action.expires_at == "2026-09-20T12:10:00.000Z"


def test_plain_search_word_does_not_create_action() -> None:
    assert detect_web_search_offer(
        assistant_text="本轮没有联网搜索。", user_request="问题",
        source_run_id="run-1", source_message_id="message-1", now=NOW,
    ) is None


def test_search_once_offer_creates_pending_action() -> None:
    action = detect_web_search_offer(
        assistant_text="要不要我这轮实际搜一次「小狗叫」，搜完再给你准话？",
        user_request="小狗叫是什么梗？", source_run_id="run-1",
        source_message_id="message-1", now=NOW,
    )

    assert action is not None
    assert action.query_hint == "小狗叫是什么梗？"


def test_soft_intro_search_offer_creates_pending_action() -> None:
    action = detect_web_search_offer(
        assistant_text="需要的话我也可以联网帮你搜一下确认最新的出处细节。",
        user_request="大狗叫又是什么", source_run_id="run-1",
        source_message_id="message-1", now=NOW,
    )

    assert action is not None
    assert action.query_hint == "大狗叫又是什么"


def test_verify_offer_creates_pending_action() -> None:
    action = detect_web_search_offer(
        assistant_text="如果你需要，我可以在下一轮核实更准确的信息来源。",
        user_request="大狗叫又是什么", source_run_id="run-1",
        source_message_id="message-1", now=NOW,
    )

    assert action is not None
    assert action.query_hint == "大狗叫又是什么"


def test_confirmation_requires_pending_action() -> None:
    assert resolve_pending_confirmation(user_text="好的", action=ACTION) == "confirm"
    assert resolve_pending_confirmation(user_text="好的", action=None) == "none"
    assert resolve_pending_confirmation(user_text="不用", action=ACTION) == "reject"


def test_confirmed_pending_search_becomes_current_required_web_intent() -> None:
    intent = WebIntentResolver().resolve(
        content="好的", request_mode="auto", recent=[], pending_action=ACTION,
    )

    assert (intent.requirement, intent.freshness, intent.evidence) == (
        "required", "current", "required",
    )
    assert intent.reason_code == "confirmed_pending_web_search"
    assert intent.inherited_from_run_id == "run-1"


def test_pending_search_does_not_override_web_off() -> None:
    intent = WebIntentResolver().resolve(
        content="好的", request_mode="off", recent=[], pending_action=ACTION,
    )

    assert intent.requirement == "off"
    assert intent.reason_code == "web_search_off"


def test_semantic_offer_parser_accepts_natural_search_offer() -> None:
    intent = parse_semantic_follow_up(
        '{"action":"offer_confirmation","capability":"web_search",'
        '"query_hint":"小狗叫是什么梗？","evidence":"required",'
        '"confidence":0.94}'
    )

    assert intent.action == "offer_confirmation"
    assert intent.capability == "web_search"
    assert intent.query_hint == "小狗叫是什么梗？"


def test_semantic_offer_parser_recovers_json_wrapped_in_model_prose() -> None:
    intent = parse_semantic_follow_up(
        '我判断这是一个搜索建议：\n```json\n'
        '{"action":"offer_confirmation","capability":"web_search",'
        '"query_hint":"小狗叫是什么梗？","evidence":"required",'
        '"confidence":0.94}\n```'
    )

    assert intent.action == "offer_confirmation"
    assert intent.query_hint == "小狗叫是什么梗？"


def test_semantic_follow_up_parser_rejects_permission_injection() -> None:
    with pytest.raises(ValueError):
        parse_semantic_follow_up(
            '{"action":"execute","capability":"web_search",'
            '"provider":"kimi","confidence":0.99}'
        )
