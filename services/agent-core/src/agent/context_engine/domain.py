from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol


class ContextEngineError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass(frozen=True)
class CompactionTarget:
    provider_profile_id: str
    model_id: str
    protocol_id: str
    context_window: int | None
    max_output_tokens: int | None


@dataclass(frozen=True)
class CompactionClaim:
    attempt_id: str
    claim_token: str
    target: CompactionTarget


class TrustKind(StrEnum):
    SYSTEM_POLICY = "system_policy"
    USER_AUTHORED = "user_authored"
    USER_CONFIRMED_PROJECT_DATA = "user_confirmed_project_data"
    EXTERNAL_UNTRUSTED = "external_untrusted"
    MODEL_INFERENCE = "model_inference"
    SYSTEM_OBSERVATION = "system_observation"


class ContextSection(StrEnum):
    STABLE = "stable"
    CHECKPOINT = "checkpoint"
    RECENT_HISTORY = "recent_history"
    RETRIEVAL = "retrieval"
    TASK_STATE = "task_state"
    CURRENT = "current"


class Representation(StrEnum):
    ORIGINAL = "original"
    CHECKPOINT = "checkpoint"
    REFERENCE = "reference"
    RAW_REPLAY = "raw_replay"
    METADATA = "metadata"


class RetrievalConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"


@dataclass(frozen=True)
class ContextPolicy:
    version: str
    target_ratio: float
    hard_limit_ratio: float
    fallback_context_window: int
    minimum_output_reserve: int
    token_method: str
    segmenter_version: str
    compaction_output_limit: int
    minimum_compaction_reclaim: int


@dataclass(frozen=True)
class SourceRef:
    source_type: str
    source_id: str | None
    trust: TrustKind
    source_start: int | None = None
    source_end: int | None = None
    source_hash: str | None = None
    source_version: str | None = None
    derived_from_untrusted: bool = False


@dataclass(frozen=True)
class ContextItem:
    source: SourceRef
    section: ContextSection
    representation: Representation
    estimated_tokens: int
    included: bool
    inclusion_reason: str | None = None
    exclusion_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        return {
            "source_type": self.source.source_type,
            "source_id": self.source.source_id,
            "section": self.section.value,
            "representation": self.representation.value,
            "trust": self.source.trust.value,
            "derived_from_untrusted": self.source.derived_from_untrusted,
            "source_start": self.source.source_start,
            "source_end": self.source.source_end,
            "source_hash": self.source.source_hash,
            "source_version": self.source.source_version,
            "estimated_tokens": self.estimated_tokens,
            "included": self.included,
            "inclusion_reason": self.inclusion_reason,
            "exclusion_reason": self.exclusion_reason,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class PackedContext:
    messages: list[dict[str, Any]]
    items: list[ContextItem]
    estimated_tokens: int
    context_window: int
    input_target: int
    output_reserve: int
    stable_prefix_hash: str
    dynamic_suffix_hash: str
    content_hash: str
    retrieval_confidence: RetrievalConfidence
    context_generation: int
    global_instruction_revision: int = 0
    global_instruction_hash: str | None = None
    compression_applied: bool = False
    input_hard_limit: int | None = None
    body_input_limit: int | None = None
    request_overhead_tokens: int = 0
    history_omission_allowed: bool = False


class ContextStore(Protocol):
    def load_context_sources(self, run_id: str) -> dict[str, Any]: ...
