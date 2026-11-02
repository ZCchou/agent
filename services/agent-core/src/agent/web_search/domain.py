from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class WebSearchPolicyError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class WebSearchMode(StrEnum):
    INHERIT = "inherit"
    OFF = "off"
    AUTO = "auto"


class WebAccessPolicy(StrEnum):
    DISABLED = "disabled"
    ALLOW_SEARCH = "allow_search"


class WebSearchRequestMode(StrEnum):
    OFF = "off"
    AUTO = "auto"
    FORCE_ONCE = "force_once"


class WebSearchRoute(StrEnum):
    NONE = "none"
    NATIVE = "native"
    HOST_TOOL = "host_tool"
    HOST_PRESEARCH = "host_presearch"
    SPECIALIST = "specialist"


class NativeSearchStatus(StrEnum):
    DECLARED = "declared"
    VERIFIED = "verified"
    UNSUPPORTED = "unsupported"
    TEMPORARY_ERROR = "temporary_error"
    NOT_APPLICABLE = "not_applicable"


class WebOperationStatus(StrEnum):
    PREPARED = "prepared"
    DISPATCHED = "dispatched"
    SEARCHING = "searching"
    READING_SOURCES = "reading_sources"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    UNKNOWN_OUTCOME = "unknown_outcome"


class WebEvidenceStatus(StrEnum):
    NOT_EVALUATED = "not_evaluated"
    PROVIDER_REFERENCE = "provider_reference"
    TRACEABLE_SOURCES = "traceable_sources"
    MISSING = "missing"
    INVALID = "invalid"
    LEGACY_UNKNOWN = "legacy_unknown"


WEB_OPERATION_TRANSITIONS: dict[WebOperationStatus, frozenset[WebOperationStatus]] = {
    WebOperationStatus.PREPARED: frozenset({
        WebOperationStatus.DISPATCHED, WebOperationStatus.CANCELLED, WebOperationStatus.FAILED,
    }),
    WebOperationStatus.DISPATCHED: frozenset({
        WebOperationStatus.SEARCHING, WebOperationStatus.COMPLETED,
        WebOperationStatus.FAILED, WebOperationStatus.CANCELLED,
        WebOperationStatus.INTERRUPTED, WebOperationStatus.UNKNOWN_OUTCOME,
    }),
    WebOperationStatus.SEARCHING: frozenset({
        WebOperationStatus.READING_SOURCES, WebOperationStatus.COMPLETED,
        WebOperationStatus.FAILED, WebOperationStatus.CANCELLED,
        WebOperationStatus.INTERRUPTED, WebOperationStatus.UNKNOWN_OUTCOME,
    }),
    WebOperationStatus.READING_SOURCES: frozenset({
        WebOperationStatus.COMPLETED, WebOperationStatus.FAILED,
        WebOperationStatus.CANCELLED, WebOperationStatus.INTERRUPTED,
    }),
    WebOperationStatus.COMPLETED: frozenset(),
    WebOperationStatus.FAILED: frozenset(),
    WebOperationStatus.CANCELLED: frozenset(),
    WebOperationStatus.INTERRUPTED: frozenset(),
    WebOperationStatus.UNKNOWN_OUTCOME: frozenset(),
}


@dataclass(frozen=True)
class WebSearchDecision:
    request_mode: WebSearchRequestMode
    route: WebSearchRoute
    adapter_or_backend_id: str | None
    reason_code: str
    policy_version: str = "web-search-policy-v2"

    def to_dict(self) -> dict[str, str | None]:
        return {
            "request_mode": self.request_mode.value,
            "route": self.route.value,
            "adapter_or_backend_id": self.adapter_or_backend_id,
            "reason_code": self.reason_code,
            "policy_version": self.policy_version,
        }


@dataclass(frozen=True)
class WebSearchCandidate:
    native_status: NativeSearchStatus
    native_adapter: str
    native_compatible: bool
    host_backend_id: str | None
    tool_mode: str
