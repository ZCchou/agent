from copy import deepcopy

import pytest

from agent.execution.compatibility import incompatibility_reason
from agent.execution.domain import CapabilityRequirements, ModelCandidate
from agent.runtime import ChatRunService


def reason(content, *, conversation=None, attachments=None):
    minimum = ChatRunService._minimum_context_requirement(
        conversation, content, attachments or [],
    )
    candidate = ModelCandidate(
        "local", "qwen", "openai_chat", "local",
        {"text": "yes", "context_length": 16128},
    )
    return incompatibility_reason(
        candidate, CapabilityRequirements(minimum_context_tokens=minimum),
        automatic=True,
    )


@pytest.mark.parametrize("role", ["assistant", "user"])
def test_compressible_history_does_not_reject_current_turn_before_context_engine(role):
    conversation = {"messages": [
        {"id": "long-report", "role": role, "content": "审阅报告正文" * 12000},
        {"id": "latest", "role": "user", "content": "继续审阅附件的选定位置"},
    ]}
    original = deepcopy(conversation)
    assert reason("审阅附件表格中的签名角色，保留引用依据。", conversation=conversation) is None
    # Admission must leave history intact for the existing packing/compaction lifecycle.
    assert conversation == original


def test_irreducible_oversized_current_turn_still_rejects_small_model():
    assert reason("当前请求必须保留的内容" * 12000) == "model_context_too_small"


def test_current_images_still_count_toward_admission_capacity():
    images = [{"kind": "image", "model_width": 4096, "model_height": 4096}] * 40
    assert reason("逐一比较所有图片", attachments=images) == "model_context_too_small"
