from __future__ import annotations

import pytest

from agent.execution.coordinator import ExecutionCoordinator


def test_advisor_parser_accepts_capability_only_contract() -> None:
    parsed = ExecutionCoordinator.parse_routing_intent(
        '{"files":"write","tools":"required","web":"not_needed",'
        '"web_evidence":"optional","reasoning":"standard",'
        '"task_kinds":["file_work"],"explanation_code":"file_cleanup"}'
    )

    assert parsed["files"] == "write"
    assert parsed["tools"] == "required"
    assert parsed["task_kinds"] == ["file_work"]


def test_advisor_parser_rejects_provider_and_permission_fields() -> None:
    with pytest.raises(ValueError):
        ExecutionCoordinator.parse_routing_intent(
            '{"provider":"kimi","approve":true,"web":"not_needed",'
            '"reasoning":"standard","task_kinds":["general"],'
            '"explanation_code":"general"}'
        )


def test_advisor_parser_accepts_provider_independent_web_vocabulary() -> None:
    parsed = ExecutionCoordinator.parse_routing_intent(
        '{"files":"available","tools":"available","web":"off",'
        '"web_evidence":"optional","reasoning":"preferred",'
        '"task_kinds":["research"],"explanation_code":"static_request"}'
    )

    assert parsed["web"] == "not_needed"
    assert parsed["reasoning"] == "high"


def test_advisor_parser_defaults_follow_up_to_none() -> None:
    parsed = ExecutionCoordinator.parse_routing_intent(
        '{"files":"available","tools":"available","web":"not_needed",'
        '"web_evidence":"optional","reasoning":"standard",'
        '"task_kinds":["general"],"explanation_code":"general"}'
    )

    assert parsed["follow_up"] == "none"


def test_advisor_can_parse_follow_up_but_runtime_must_constrain_it() -> None:
    payload = (
        '{"files":"available","tools":"available","web":"required",'
        '"web_evidence":"required","reasoning":"standard",'
        '"task_kinds":["research"],"explanation_code":"confirmed",'
        '"follow_up":"confirm"}'
    )
    parsed = ExecutionCoordinator.parse_routing_intent(payload)
    assert parsed["follow_up"] == "confirm"
    assert ExecutionCoordinator.constrain_follow_up(parsed, pending_action=False)["follow_up"] == "none"
    assert ExecutionCoordinator.constrain_follow_up(parsed, pending_action=True)["follow_up"] == "confirm"
