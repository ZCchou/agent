from __future__ import annotations

import copy
from typing import Any


SUPPORTED_MODEL_MEDIA_TYPES = frozenset({"image/png", "image/jpeg"})


class MultimodalFormatError(ValueError):
    """Raised before a provider request when canonical content is invalid."""


def text_part(text: str) -> dict[str, str]:
    return {"type": "text", "text": text}


def image_part(
    *, attachment_id: str, media_type: str, data_base64: str,
    width: int, height: int, sha256: str,
) -> dict[str, Any]:
    if media_type not in SUPPORTED_MODEL_MEDIA_TYPES:
        raise MultimodalFormatError(f"Unsupported normalized image type: {media_type}")
    if not attachment_id or not data_base64 or not sha256 or width < 1 or height < 1:
        raise MultimodalFormatError("Canonical image content is incomplete.")
    return {
        "type": "image",
        "attachment_id": attachment_id,
        "media_type": media_type,
        "data_base64": data_base64,
        "width": width,
        "height": height,
        "sha256": sha256,
    }


def contains_images(content: Any) -> bool:
    return isinstance(content, list) and any(
        isinstance(item, dict) and item.get("type") == "image" for item in content
    )


def _canonical_parts(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [text_part(content)] if content else []
    if not isinstance(content, list):
        return []
    result: list[dict[str, Any]] = []
    for raw in content:
        if not isinstance(raw, dict):
            continue
        if raw.get("type") == "text" and isinstance(raw.get("text"), str):
            result.append(text_part(raw["text"]))
        elif raw.get("type") == "image":
            result.append(image_part(
                attachment_id=str(raw.get("attachment_id") or ""),
                media_type=str(raw.get("media_type") or ""),
                data_base64=str(raw.get("data_base64") or ""),
                width=int(raw.get("width") or 0),
                height=int(raw.get("height") or 0),
                sha256=str(raw.get("sha256") or ""),
            ))
        else:
            # Provider-native blocks produced by a prior tool turn are not
            # canonical image input. Keep them so protocol adapters can resume.
            result.append(copy.deepcopy(raw))
    return result


def openai_chat_content(content: Any) -> str | list[dict[str, Any]]:
    if isinstance(content, str):
        return content
    converted: list[dict[str, Any]] = []
    for part in _canonical_parts(content):
        if part.get("type") == "text":
            converted.append(part)
        elif part.get("type") == "image":
            converted.append({
                "type": "image_url",
                "image_url": {
                    "url": f'data:{part["media_type"]};base64,{part["data_base64"]}',
                    "detail": "auto",
                },
            })
        else:
            converted.append(part)
    return converted


def responses_content(content: Any) -> str | list[dict[str, Any]]:
    if isinstance(content, str):
        return content
    converted: list[dict[str, Any]] = []
    for part in _canonical_parts(content):
        if part.get("type") == "text":
            converted.append({"type": "input_text", "text": part["text"]})
        elif part.get("type") == "image":
            converted.append({
                "type": "input_image",
                "image_url": f'data:{part["media_type"]};base64,{part["data_base64"]}',
                "detail": "auto",
            })
        else:
            converted.append(part)
    return converted


def anthropic_content(content: Any) -> str | list[dict[str, Any]]:
    if isinstance(content, str):
        return content
    converted: list[dict[str, Any]] = []
    for part in _canonical_parts(content):
        if part.get("type") == "text":
            converted.append(part)
        elif part.get("type") == "image":
            converted.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": part["media_type"],
                    "data": part["data_base64"],
                },
            })
        else:
            converted.append(part)
    return converted


def ollama_content(content: Any) -> tuple[str, list[str]]:
    if isinstance(content, str):
        return content, []
    texts: list[str] = []
    images: list[str] = []
    for part in _canonical_parts(content):
        if part.get("type") == "text":
            texts.append(part["text"])
        elif part.get("type") == "image":
            images.append(part["data_base64"])
    return "\n".join(texts), images


def estimate_image_tokens(width: int, height: int, protocol: str | None = None) -> int:
    """Conservative estimate tied to the final serialization protocol.

    Providers do not expose one portable image tokenizer.  The factors are a
    versioned safety margin, not claimed billing usage; actual usage always wins.
    """
    tiles = max(1, ((width + 511) // 512) * ((height + 511) // 512))
    base = 256 + tiles * 170
    factor = {
        "openai_chat": 1.0,
        "openai_responses": 1.0,
        "anthropic_messages": 1.15,
        "ollama": 1.25,
        "memory": 1.0,
    }.get(protocol or "", 1.25)
    return int(base * factor + 0.999)
