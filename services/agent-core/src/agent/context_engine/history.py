from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class HistoryTurn:
    messages: tuple[dict[str, Any], ...]
    complete: bool
    legacy: bool = False


def reconstruct_turns(messages: list[dict[str, Any]], runs: list[dict[str, Any]]) -> list[HistoryTurn]:
    ordered = sorted(messages, key=lambda item: (item.get("sequence_no") or 0, item["id"]))
    by_id = {message["id"]: message for message in ordered}
    selected: dict[str, dict[str, Any]] = {}
    superseded: set[str] = set()
    successful = [run for run in runs if run.get("status") == "completed" and run.get("assistant_message_id")]
    for run in sorted(successful, key=lambda item: (item.get("finished_at") or "", item["id"])):
        assistant = by_id.get(run["assistant_message_id"])
        if assistant is not None and assistant["role"] != "assistant":
            continue
        previous = selected.get(run["user_message_id"])
        if previous and previous["assistant_message_id"] != run["assistant_message_id"]:
            superseded.add(previous["assistant_message_id"])
        selected[run["user_message_id"]] = run

    superseded.difference_update(run["assistant_message_id"] for run in selected.values())

    turns: list[HistoryTurn] = []
    paired_ids = set(superseded)
    for run in selected.values():
        user = by_id.get(run["user_message_id"])
        assistant = by_id.get(run["assistant_message_id"])
        if user is None or user["role"] != "user" or assistant is None:
            continue
        turns.append(HistoryTurn((user, assistant), True))
        paired_ids.update((user["id"], assistant["id"]))

    remaining = [message for message in ordered
                 if message["role"] != "system_event"
                 and message["id"] not in paired_ids]
    index = 0
    while index < len(remaining):
        message = remaining[index]
        if message["role"] == "user" and index + 1 < len(remaining) \
                and remaining[index + 1]["role"] == "assistant":
            turns.append(HistoryTurn((message, remaining[index + 1]), True, True))
            index += 2
        else:
            turns.append(HistoryTurn((message,), False, True))
            index += 1
    return sorted(turns, key=lambda turn: (turn.messages[0].get("sequence_no") or 0, turn.messages[0]["id"]))


def segment_text(text: str, maximum_chars: int) -> list[tuple[int, int, str]]:
    if maximum_chars < 1:
        raise ValueError("maximum_chars must be positive")
    result: list[tuple[int, int, str]] = []
    start = 0
    length = len(text)
    while start < length:
        upper = min(length, start + maximum_chars)
        if upper < length:
            boundary = text.rfind("\n\n", start, upper)
            if boundary <= start:
                boundary = text.rfind("\n", start, upper)
            if boundary > start:
                upper = boundary + (2 if text[boundary:boundary + 2] == "\n\n" else 1)
        result.append((start, upper, text[start:upper]))
        start = upper
    return result or [(0, 0, "")]


def source_hash(parts: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(str(part.get("id", part.get("source_id", ""))).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(part.get("content", "")).encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()
