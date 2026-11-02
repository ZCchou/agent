"""Shared lifecycle decisions; no request content is used in diagnostics."""
from __future__ import annotations

import logging
import sqlite3
import traceback

from agent.context_engine.domain import ContextEngineError
from agent.providers import ProviderError
from agent.storage import CoreDataError


# Generic availability/HTTP gateway errors cannot prove that generation was
# never dispatched. Only the transport's positive connection-failure evidence
# may be retried automatically, including after profile refresh or next send.
AUTOMATIC_RECOVERY_CODES = frozenset({"provider_request_not_sent"})


def error_code(error: Exception) -> str:
    if isinstance(error, (ContextEngineError, ProviderError, CoreDataError)):
        return error.code
    if isinstance(error, sqlite3.Error):
        return "persistence_failed"
    if isinstance(error, TimeoutError):
        return "request_timeout"
    return "context_compaction_internal_error"


def log_failure(error: Exception, *, logger: logging.Logger | None = None) -> None:
    # Exception messages and source lines can contain model/user data. Retain
    # only frame locations and exception type, sufficient to locate the defect.
    frames = [{"file": frame.filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1],
               "line": frame.lineno, "function": frame.name}
              for frame in traceback.extract_tb(error.__traceback__)]
    (logger or logging.getLogger(__name__)).error(
        "Context task failed: %s; frames=%s", type(error).__name__, frames,
    )
