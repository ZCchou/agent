from __future__ import annotations

from agent.execution.web_intent import (
    WebIntentResolver, should_probe_contextual_web_intent,
    should_probe_semantic_web_intent,
)
from agent.web_search.evidence import evaluate_web_evidence


def test_static_question_does_not_search_in_auto_mode() -> None:
    intent = WebIntentResolver().resolve(
        content="五条悟是谁", request_mode="auto", recent=[],
    )
    assert intent.requirement == "off"
    assert intent.evidence == "optional"


def test_explicit_current_request_requires_traceable_evidence() -> None:
    intent = WebIntentResolver().resolve(
        content="搜索最新番剧", request_mode="auto", recent=[],
    )
    assert (intent.requirement, intent.freshness, intent.evidence) == (
        "required", "current", "required",
    )


def test_recent_time_window_requires_current_search() -> None:
    intent = WebIntentResolver().resolve(
        content="最近两周有哪些热梗", request_mode="auto", recent=[],
    )
    assert (intent.requirement, intent.freshness, intent.evidence) == (
        "required", "current", "required",
    )


def test_ambiguous_question_can_be_sent_to_semantic_web_advisor() -> None:
    assert should_probe_semantic_web_intent("大狗叫又是什么") is True
    assert should_probe_semantic_web_intent("帮我润色这段文字") is False


def test_short_contextual_reply_can_be_sent_to_semantic_web_advisor() -> None:
    recent = [{"role": "assistant", "content": "如果需要，我可以核实出处。"}]

    for reply in ("需要", "再试一次", "你尝试一次", "再试下"):
        assert should_probe_contextual_web_intent(reply, recent) is True

    assert should_probe_contextual_web_intent("需要", []) is False
    assert should_probe_contextual_web_intent("不要联网", recent) is False


def test_correction_inherits_recent_completed_research() -> None:
    recent = [{
        "run_id": "research-run",
        "search_executed": True,
        "resolved_intent": {
            "requirement": "required", "evidence": "required",
        },
    }]
    intent = WebIntentResolver().resolve(
        content="今年是2026年，你刚才说错了", request_mode="auto", recent=recent,
    )
    assert intent.requirement == "required"
    assert intent.relation == "correction"
    assert intent.inherited_from_run_id == "research-run"


def test_ambiguous_follow_up_uses_bounded_semantic_advisor_path() -> None:
    recent = [{
        "run_id": "research-run",
        "search_executed": True,
        "resolved_intent": {"requirement": "required", "evidence": "required"},
    }]
    intent = WebIntentResolver().resolve(
        content="那第二个呢", request_mode="auto", recent=recent,
    )
    assert intent.requirement == "optional"
    assert intent.evidence == "required"
    assert intent.relation == "follow_up"


def test_unexecuted_optional_history_is_not_an_active_research_context() -> None:
    recent = [{
        "run_id": "plain-run",
        "search_executed": False,
        "resolved_intent": {"requirement": "optional", "evidence": "required"},
    }]
    intent = WebIntentResolver().resolve(
        content="那第二个呢", request_mode="auto", recent=recent,
    )
    assert intent.requirement == "off"


def test_evidence_status_does_not_treat_provider_reference_as_source() -> None:
    reference_only = evaluate_web_evidence([{
        "status": "completed", "evidence_status": "provider_reference",
        "verified_source_count": 0,
    }])
    grounded = evaluate_web_evidence([{
        "status": "completed", "evidence_status": "traceable_sources",
        "verified_source_count": 2,
    }])
    assert reference_only.status == "provider_reference"
    assert reference_only.grounded is False
    assert grounded.grounded is True
