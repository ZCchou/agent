"""Explicit attachment selection. Text mentions never grant send permission."""
from dataclasses import dataclass, field
from typing import Any

from agent.storage import CoreDataError
from agent.execution.domain import stable_hash


@dataclass(frozen=True)
class AttachmentReferenceSelection:
    effective_attachments: list[dict[str, Any]]
    unresolved_candidates: list[dict[str, Any]] = field(default_factory=list)
    exclusions: list[dict[str, Any]] = field(default_factory=list)


def validate_saved_selection(plan: Any, intent: dict[str, Any]) -> list[dict[str, Any]]:
    """Verify saved provenance before it may widen the current model input.

    The mutable intent has its own hash; selection is also bound to the immutable
    requirements hash so an ordinary intent refresh cannot authorize new ranges.
    Legacy plans without a selection retain the existing fail-closed clone gate.
    """
    if "attachment_selection" not in intent:
        return []
    selection = intent["attachment_selection"]
    try:
        identifiers = [item["current_attachment_id"] for item in selection]
        requirements_hash = stable_hash(intent["effective_requirements"])
        expected = stable_hash({"requirements_hash": requirements_hash,
                                "attachment_selection": selection}) if selection else requirements_hash
        valid = (
            isinstance(selection, list)
            and len(identifiers) == len(set(identifiers))
            and identifiers == intent["effective_attachment_ids"]
            and stable_hash(intent) == plan["resolution_hash"]
            and expected == plan["requirements_hash"]
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise CoreDataError("attachment_reference_selection_changed",
                            "The saved attachment selection failed integrity validation. Select it again.")
    return selection


def validate_references(references: Any) -> list[dict[str, Any]]:
    if not isinstance(references, list) or len(references) > 8:
        raise CoreDataError("attachment_reference_invalid", "Select at most eight attachment references.")
    seen = set()
    for ref in references:
        if not isinstance(ref, dict) or set(ref) - {"attachment_id", "source_start", "source_end"} \
                or not isinstance(ref.get("attachment_id"), str) or not ref["attachment_id"] \
                or ref["attachment_id"] in seen:
            raise CoreDataError("attachment_reference_invalid", "Attachment references must contain unique IDs and optional ranges only.")
        seen.add(ref["attachment_id"])
        start, end = ref.get("source_start", 0), ref.get("source_end")
        if type(start) is not int or start < 0 or (
            "source_end" in ref and (type(end) is not int or end <= start)
        ):
            raise CoreDataError("attachment_reference_range_invalid", "The attachment range is invalid.")
    return references


def text_range(attachment: dict[str, Any], content: str) -> tuple[str, int, int]:
    start = attachment.get("source_start", 0)
    end = attachment.get("source_end", len(content))
    if type(start) is not int or type(end) is not int or not 0 <= start <= end <= len(content) \
            or (start == end and content):
        raise CoreDataError("attachment_reference_range_invalid", "The range is outside the extracted text.")
    return content[start:end], start, end
