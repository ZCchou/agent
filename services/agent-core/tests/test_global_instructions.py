from __future__ import annotations

import hashlib

import pytest

from agent.protocol import handle_message
from agent.storage import CoreDataError, CoreRepository


def test_new_database_seeds_global_instructions_and_schema_v20(tmp_path) -> None:
    repository = CoreRepository(tmp_path / "global-instructions.db")

    instructions = repository.get_global_instructions()

    assert instructions["enabled"] is True
    assert instructions["revision"] == 1
    assert instructions["default_version"] == "agent-default-v1"
    assert "agent" in instructions["content"]
    assert instructions["content_hash"] == hashlib.sha256(
        instructions["content"].encode("utf-8")
    ).hexdigest()

    with repository._connect() as connection:
        assert connection.execute("SELECT version FROM schema_info").fetchone()[0] == 26
        assert connection.execute(
            "SELECT COUNT(*) FROM global_instructions WHERE id = 1"
        ).fetchone()[0] == 1


def test_update_disable_and_reset_global_instructions_are_versioned(tmp_path) -> None:
    repository = CoreRepository(tmp_path / "global-instructions-update.db")

    updated = repository.update_global_instructions("先给结论，再给依据。", enabled=True)
    assert updated["content"] == "先给结论，再给依据。"
    assert updated["enabled"] is True
    assert updated["revision"] == 2

    disabled = repository.update_global_instructions("不应被注入。", enabled=False)
    assert disabled["enabled"] is False
    assert disabled["revision"] == 3

    reset = repository.reset_global_instructions()
    assert reset["enabled"] is True
    assert reset["revision"] == 4
    assert "agent" in reset["content"]


def test_global_instruction_length_is_bounded(tmp_path) -> None:
    repository = CoreRepository(tmp_path / "global-instructions-validation.db")

    with pytest.raises(CoreDataError) as error:
        repository.update_global_instructions("x" * 8001, enabled=True)

    assert error.value.code == "invalid_global_instruction"


def test_global_instruction_protocol_round_trip(tmp_path) -> None:
    repository = CoreRepository(tmp_path / "global-instructions-protocol.db")

    loaded = handle_message(
        {"v": 19, "id": "get", "type": "memory.global_instructions.get", "params": {}},
        repository,
    )
    assert loaded["result"]["global_instructions"]["revision"] == 1

    saved = handle_message(
        {
            "v": 19,
            "id": "update",
            "type": "memory.global_instructions.update",
            "params": {"content": "使用简洁中文回答。", "enabled": True},
        },
        repository,
    )
    assert saved["result"]["global_instructions"]["content"] == "使用简洁中文回答。"

    reset = handle_message(
        {"v": 19, "id": "reset", "type": "memory.global_instructions.reset", "params": {}},
        repository,
    )
    assert reset["result"]["global_instructions"]["revision"] == 3
