from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class WebEvidenceDecision:
    status: str
    verified_source_count: int

    @property
    def grounded(self) -> bool:
        return self.status == "traceable_sources" and self.verified_source_count > 0


def evaluate_web_evidence(operations: list[dict[str, Any]]) -> WebEvidenceDecision:
    verified = sum(int(item.get("verified_source_count") or 0) for item in operations)
    statuses = {str(item.get("evidence_status") or "not_evaluated") for item in operations}
    if verified > 0:
        return WebEvidenceDecision("traceable_sources", verified)
    if "invalid" in statuses:
        return WebEvidenceDecision("invalid", 0)
    if "provider_reference" in statuses:
        return WebEvidenceDecision("provider_reference", 0)
    if operations and all(item.get("status") == "completed" for item in operations):
        return WebEvidenceDecision("missing", 0)
    return WebEvidenceDecision("not_evaluated", 0)
