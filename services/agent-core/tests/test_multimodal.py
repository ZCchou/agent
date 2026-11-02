from __future__ import annotations

import base64
import sqlite3

from agent.multimodal import (
    anthropic_content,
    estimate_image_tokens,
    image_part,
    ollama_content,
    openai_chat_content,
    responses_content,
    text_part,
)
from agent.providers import _ollama_messages, _provider_messages, _responses_input
from agent.storage import CoreRepository
from agent.web_search.native_adapters import (
    _anthropic_messages,
    _classify_anthropic_image_error,
)
from agent.migrations.v12_multimodal_images import migrate


def canonical_content() -> list[dict]:
    return [
        text_part("describe this"),
        image_part(
            attachment_id="attachment-1", media_type="image/png",
            data_base64=base64.b64encode(b"png-data").decode("ascii"),
            width=640, height=480, sha256="a" * 64,
        ),
    ]


def test_image_budget_estimate_is_bound_to_serialization_protocol() -> None:
    openai = estimate_image_tokens(1024, 768, "openai_responses")
    anthropic = estimate_image_tokens(1024, 768, "anthropic_messages")
    unknown = estimate_image_tokens(1024, 768, "custom")
    assert openai < anthropic < unknown


def test_openai_chat_uses_data_url_at_provider_boundary() -> None:
    content = openai_chat_content(canonical_content())
    assert content[0] == {"type": "text", "text": "describe this"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    message = _provider_messages([{"role": "user", "content": canonical_content()}])[0]
    assert message["content"] == content


def test_responses_uses_input_image() -> None:
    content = responses_content(canonical_content())
    assert content[0] == {"type": "input_text", "text": "describe this"}
    assert content[1]["type"] == "input_image"
    assert _responses_input([{"role": "user", "content": canonical_content()}])[0]["content"] == content


def test_anthropic_uses_base64_source_and_keeps_tools() -> None:
    content = anthropic_content(canonical_content())
    assert content[1] == {
        "type": "image",
        "source": {
            "type": "base64", "media_type": "image/png",
            "data": base64.b64encode(b"png-data").decode("ascii"),
        },
    }
    _system, messages = _anthropic_messages([
        {"role": "user", "content": canonical_content()},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "call-1", "function": {"name": "fs__read", "arguments": '{"path":"README.md"}'},
        }]},
    ])
    assert messages[0]["content"] == content
    assert messages[1]["content"][0]["type"] == "tool_use"


def test_ollama_uses_images_array() -> None:
    text, images = ollama_content(canonical_content())
    assert text == "describe this"
    assert images == [base64.b64encode(b"png-data").decode("ascii")]
    message = _ollama_messages([{"role": "user", "content": canonical_content()}])[0]
    assert message == {"role": "user", "content": "describe this", "images": images}


def test_anthropic_image_error_distinguishes_route_from_corrupt_image() -> None:
    route = _classify_anthropic_image_error(
        "Image blocks are invalid for this request combination",
        has_images=True, has_web_search=True, has_function_tools=True,
    )
    malformed = _classify_anthropic_image_error(
        "Invalid image: could not decode image data",
        has_images=True, has_web_search=False, has_function_tools=False,
    )
    assert route and route[0] == "multimodal_route_unsupported"
    assert malformed and malformed[0] == "image_format_unsupported"


def test_schema_v12_preserves_legacy_image_rows() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript("""
        CREATE TABLE projects(id TEXT PRIMARY KEY);
        CREATE TABLE conversations(id TEXT PRIMARY KEY);
        CREATE TABLE messages(id TEXT PRIMARY KEY);
        CREATE TABLE model_runs(id TEXT PRIMARY KEY);
        CREATE TABLE message_attachments(
            id TEXT PRIMARY KEY, message_id TEXT, kind TEXT, name TEXT,
            media_type TEXT, byte_size INTEGER, sha256 TEXT, storage_key TEXT,
            width INTEGER, height INTEGER, source_attachment_id TEXT, created_at TEXT
        );
        INSERT INTO message_attachments VALUES
            ('a', 'm', 'image', 'old.webp', 'image/webp', 10, 'hash', 'old.bin',
             20, 30, NULL, 'now');
    """)
    migrate(connection)
    row = connection.execute(
        """SELECT normalization_version, model_storage_key, model_media_type
           FROM message_attachments WHERE id = 'a'"""
    ).fetchone()
    assert row == ("legacy_passthrough", None, None)


def test_host_can_register_lazy_model_copy_for_legacy_image(tmp_path) -> None:
    repository = CoreRepository(tmp_path / "legacy-image.db")
    run = repository.start_model_run(
        content="inspect", idempotency_key="legacy-image", provider_profile_id="provider",
        model_id="vision-model", kind="standard_chat", category="casual",
        attachments=[{
            "id": "legacy-attachment", "kind": "image", "name": "old.webp",
            "media_type": "image/webp", "byte_size": 12, "sha256": "a" * 64,
            "storage_key": "message-attachments/old.bin", "width": 20, "height": 30,
            "normalization_version": "legacy_passthrough",
        }],
    )
    before_generation = repository.get_conversation(run["conversation_id"])["context_generation"]
    attachment = repository.update_attachment_model_copy(
        "legacy-attachment", "message-attachments/old.model.png", "image/png",
        20, "b" * 64, 20, 30, "image-normalization-v1",
    )
    assert attachment["model_media_type"] == "image/png"
    assert attachment["normalization_version"] == "image-normalization-v1"
    assert repository.get_conversation(run["conversation_id"])["context_generation"] \
        == before_generation + 1
