from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class WorkPurposeProfile:
    id: str
    instruction: str
    planning_style: str
    discovery_style: str
    verification_style: str
    response_style: str

    def as_policy(self) -> dict[str, object]:
        return {
            "category": self.id,
            "instruction": self.instruction,
            "planning_style": self.planning_style,
            "discovery_style": self.discovery_style,
            "verification_style": self.verification_style,
            "response_style": self.response_style,
            # Kept for one compatibility release. Purpose never grants tools.
            "suggested_abilities": [],
        }


PURPOSE_PROFILES: Final[dict[str, WorkPurposeProfile]] = {
    "casual": WorkPurposeProfile(
        "casual", "Keep the interaction natural, lightweight, and concise.",
        "Act directly unless ambiguity changes the outcome.",
        "Inspect only files clearly relevant to the request.",
        "Check that the requested result is present and understandable.",
        "Give a compact result-focused response.",
    ),
    "research": WorkPurposeProfile(
        "research", "Emphasize evidence, sources, and uncertainty.",
        "Form questions, gather evidence, then synthesize conclusions.",
        "Search broadly enough to identify primary project evidence.",
        "Cite project paths and distinguish evidence from inference.",
        "Present findings, sources, uncertainty, and open questions.",
    ),
    "work": WorkPurposeProfile(
        "work", "Focus on outcomes, deliverables, and actionable next steps.",
        "Identify the deliverable and execute it in coherent batches.",
        "Inspect inputs and existing artifacts before changing them.",
        "Confirm the deliverable matches the request and list remaining work.",
        "Lead with the completed outcome and a short action summary.",
    ),
    "writing": WorkPurposeProfile(
        "writing", "Consider audience, structure, tone, and iterative drafts.",
        "Understand audience and voice before revising content.",
        "Read surrounding material and style examples before editing.",
        "Check structure, consistency, tone, and formatting continuity.",
        "Explain editorial choices without overwhelming the finished text.",
    ),
    "coding": WorkPurposeProfile(
        "coding", "Prioritize correctness, minimal changes, tests, and impact.",
        "Inspect architecture, reproduce the issue, then make a focused change.",
        "Read entry points, contracts, tests, and affected callers first.",
        "Verify behavior and report tests or checks that could not be run.",
        "Lead with what changed, affected files, and verification results.",
    ),
    "gaming": WorkPurposeProfile(
        "gaming", "Emphasize mechanics, strategy, and a relaxed tone.",
        "Clarify the objective and compare viable strategies.",
        "Inspect configuration or save data only when clearly required.",
        "Check recommendations against the described rules and constraints.",
        "Keep the response practical and easy to follow.",
    ),
    "other": WorkPurposeProfile(
        "other", "Use a neutral, general-purpose approach.",
        "Choose a proportionate plan based on the request.",
        "Inspect only information needed to complete the task.",
        "Check the result against the user's explicit requirements.",
        "Give a clear result and mention material caveats.",
    ),
}


class BehaviorPolicyResolver:
    def resolve(self, purpose_id: str) -> WorkPurposeProfile:
        try:
            return PURPOSE_PROFILES[purpose_id]
        except KeyError as error:
            raise ValueError("Purpose is not supported.") from error


BEHAVIOR_POLICIES: Final[dict[str, dict[str, object]]] = {
    purpose_id: profile.as_policy() for purpose_id, profile in PURPOSE_PROFILES.items()
}
