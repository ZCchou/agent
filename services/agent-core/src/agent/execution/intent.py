from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from agent.execution.domain import CapabilityRequirements

IntentSource = Literal["rules", "advisor", "merged", "fallback"]
IntentConfidence = Literal["high", "medium", "low"]


@dataclass(frozen=True)
class IntentResolution:
    """Auditable capability intent without provider or permission authority."""

    requirements: CapabilityRequirements
    source: IntentSource
    confidence: IntentConfidence
    signals: tuple[str, ...] = ()
    explanation_code: str = "intent_unresolved"
    advisor_error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirements": self.requirements.to_dict(),
            "source": self.source,
            "confidence": self.confidence,
            "signals": list(self.signals),
            "explanation_code": self.explanation_code,
            "advisor_error_code": self.advisor_error_code,
        }

    @property
    def hash_payload(self) -> dict[str, Any]:
        return asdict(self)
