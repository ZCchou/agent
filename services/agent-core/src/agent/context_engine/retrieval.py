from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from agent.context_engine.domain import RetrievalConfidence


_ENGLISH = re.compile(r"[a-z0-9_./\\-]{2,}")
_HAN = re.compile(r"[\u3400-\u9fff]")


def query_units(text: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    latin = set(_ENGLISH.findall(normalized))
    han = "".join(_HAN.findall(normalized))
    return latin | {han[index:index + 2] for index in range(max(0, len(han) - 1))}


@dataclass(frozen=True)
class RetrievedKnowledge:
    item: dict[str, Any]
    exact: bool
    title_hits: int
    content_hits: int


def retrieve_knowledge(
    knowledge: list[dict[str, Any]], query: str,
) -> tuple[list[RetrievedKnowledge], RetrievalConfidence]:
    units = query_units(query)
    ranked: list[RetrievedKnowledge] = []
    best = RetrievalConfidence.NONE
    normalized_query = unicodedata.normalize("NFKC", query).casefold()
    for item in knowledge:
        if item.get("status") == "archived" or item.get("pinned"):
            continue
        title = unicodedata.normalize("NFKC", str(item.get("title", ""))).casefold()
        content = unicodedata.normalize("NFKC", str(item.get("content", ""))).casefold()
        title_units, content_units = query_units(title), query_units(content)
        exact = bool(title and title in normalized_query) or any(
            unit in normalized_query and unit in title_units
            for unit in title_units if "/" in unit or "\\" in unit or "_" in unit
        )
        title_hits = len(units & title_units)
        content_hits = len(units & content_units)
        total = title_hits + content_hits
        if not exact and total == 0:
            continue
        ranked.append(RetrievedKnowledge(item, exact, title_hits, content_hits))
        confidence = RetrievalConfidence.HIGH if exact else (
            RetrievalConfidence.MEDIUM if total >= 2 else RetrievalConfidence.LOW
        )
        if list(RetrievalConfidence).index(confidence) < list(RetrievalConfidence).index(best):
            best = confidence
    # Stable multi-pass ordering keeps the documented precedence while making
    # newer entries win only after exact/title/content relevance has tied.
    ranked.sort(key=lambda candidate: str(candidate.item.get("id", "")))
    ranked.sort(key=lambda candidate: str(candidate.item.get("updated_at", "")), reverse=True)
    ranked.sort(key=lambda candidate: candidate.content_hits, reverse=True)
    ranked.sort(key=lambda candidate: candidate.title_hits, reverse=True)
    ranked.sort(key=lambda candidate: candidate.exact, reverse=True)
    return ranked, best
