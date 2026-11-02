from __future__ import annotations

from agent.execution.domain import CapabilityRequirements
from agent.execution.intent import IntentResolution


def test_intent_resolution_serializes_source_confidence_and_requirements() -> None:
    result = IntentResolution(
        requirements=CapabilityRequirements(files="write", tools="required"),
        source="rules",
        confidence="high",
        signals=("delete_empty_directories",),
        explanation_code="explicit_file_mutation",
    )

    assert result.to_dict()["source"] == "rules"
    assert result.to_dict()["confidence"] == "high"
    assert result.to_dict()["requirements"]["files"] == "write"
