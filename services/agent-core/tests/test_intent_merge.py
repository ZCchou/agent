from __future__ import annotations

from dataclasses import replace

from agent.execution.domain import CapabilityRequirements
from agent.execution.requirements import merge_routing_intent


def test_advisor_cannot_lower_explicit_file_write() -> None:
    base = CapabilityRequirements(files="write", tools="required")
    result = merge_routing_intent(
        base,
        {"web": "not_needed", "reasoning": "standard", "task_kinds": ["general"]},
        web_allowed=True,
    )

    assert result.files == "write"
    assert result.tools == "required"


def test_advisor_file_work_raises_project_tools_requirement() -> None:
    base = CapabilityRequirements(files="available", tools="available")
    result = merge_routing_intent(
        base,
        {"web": "not_needed", "reasoning": "standard", "task_kinds": ["file_work"]},
        web_allowed=True,
        conversation_kind="project_work",
    )

    assert result.files == "write"
    assert result.tools == "required"


def test_advisor_cannot_enable_disabled_web() -> None:
    base = CapabilityRequirements(web="off")
    result = merge_routing_intent(
        base,
        {"web": "required", "reasoning": "standard", "task_kinds": ["research"]},
        web_allowed=False,
    )

    assert result.web == "off"
