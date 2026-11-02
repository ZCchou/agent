from __future__ import annotations

import pytest

from agent.execution.rule_intent import RuleIntentResolver


@pytest.mark.parametrize("text", [
    "删除空目录", "清理没有内容的文件夹", "remove empty directories",
])
def test_rules_detect_empty_directory_mutation(text: str) -> None:
    result = RuleIntentResolver().resolve(
        text=text, conversation_kind="project_work", request_mode="off",
        attachments=[], recent=[], minimum_context_tokens=None,
    )

    assert result.requirements.files == "write"
    assert result.requirements.tools == "required"
    assert result.confidence == "high"


def test_rules_do_not_turn_directory_explanation_into_file_write() -> None:
    result = RuleIntentResolver().resolve(
        text="什么是目录", conversation_kind="project_work", request_mode="off",
        attachments=[], recent=[], minimum_context_tokens=None,
    )

    assert result.requirements.files == "available"
    assert result.requirements.tools == "available"
