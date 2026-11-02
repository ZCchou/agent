from __future__ import annotations

from typing import Any


def normalize_openai_citation(annotation: dict[str, Any], source_id: str) -> dict[str, Any] | None:
    citation = annotation.get("url_citation") if isinstance(annotation, dict) else None
    if not isinstance(citation, dict):
        return None
    url = citation.get("url")
    title = citation.get("title")
    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
        return None
    return {
        "source_id": source_id,
        "title": title if isinstance(title, str) and title else url,
        "url": url,
        "provider_source_id": citation.get("source_id"),
        "trust": "external_untrusted",
    }


def kimi_native_compatible(*, reasoning_enabled: bool) -> bool:
    """Kimi built-in web search must never silently disable model reasoning."""
    return not reasoning_enabled
