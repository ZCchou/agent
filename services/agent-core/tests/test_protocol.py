import asyncio
import os
from pathlib import Path

from agent.protocol import handle_async_message, handle_message
from agent.storage import CoreDataError, CoreRepository


def test_ping_returns_valid_envelope() -> None:
    result = handle_message({"v": 24, "id": "req-001", "type": "ping", "params": {}})
    assert result["v"] == 24
    assert result["id"] == "req-001"
    assert result["type"] == "ping"
    assert result["result"] == {
        "pid": os.getpid(), "core_version": "0.16.0-preview.4", "protocol_version": 24,
        "schema_version": 31, "build_id": "schema31-agent-unified-v1",
    }


def test_unknown_message_keeps_correlation_fields() -> None:
    result = handle_message({"v": 24, "id": "req-002", "type": "unknown", "params": {}})
    assert result["v"] == 24
    assert result["id"] == "req-002"
    assert result["type"] == "unknown"
    assert result["error"]["code"] == "unsupported_message_type"


def test_protocol_version_is_required() -> None:
    result = handle_message({"id": "req-003", "type": "ping"})
    assert result["error"]["code"] == "unsupported_protocol"


def test_request_id_is_required() -> None:
    result = handle_message({"v": 24, "type": "ping"})
    assert result["error"]["code"] == "invalid_request_id"


def test_compaction_resume_passes_only_target_identity() -> None:
    class Service:
        update_prepared_id = None
        update_admissions = 0
        async def resume_compaction(self, conversation_id, target=None):
            return {"conversation_id": conversation_id, "target": target}
    response = asyncio.run(handle_async_message({
        "v": 24, "id": "resume", "type": "context.compaction.resume",
        "params": {"conversation_id": "conversation", "target": {
            "provider_profile_id": "provider", "model_id": "model", "context_window": 999999,
        }},
    }, Service()))
    assert response["result"]["compaction"]["target"] == {
        "provider_profile_id": "provider", "model_id": "model",
    }


def test_two_track_protocol_flow(tmp_path: Path) -> None:
    repository = CoreRepository(tmp_path / "data.db")
    project_response = handle_message({
            "v": 24, "id": "p", "type": "project.create",
        "params": {"name": "Code", "category": "coding"},
    }, repository)
    project_id = project_response["result"]["project"]["id"]
    chat_response = handle_message({
            "v": 24, "id": "c", "type": "conversation.create",
        "params": {"kind": "standard_chat", "title": "Question", "category": "research"},
    }, repository)
    chat_id = chat_response["result"]["conversation"]["id"]
    derive_response = handle_message({
            "v": 24, "id": "d", "type": "conversation.derive_work",
        "params": {"source_conversation_id": chat_id, "project_id": project_id},
    }, repository)
    assert derive_response["result"]["conversation"]["kind"] == "project_work"


def test_protocol_rejects_bad_params_with_original_id(tmp_path: Path) -> None:
    result = handle_message({
            "v": 24, "id": "bad", "type": "project.create",
        "params": {"name": "Project", "category": 7},
    }, CoreRepository(tmp_path / "data.db"))
    assert result["id"] == "bad"
    assert result["error"]["code"] == "invalid_params"


def test_start_preflight_rejection_is_a_success_union_without_a_run() -> None:
    class RejectingService:
        update_prepared_id = None
        update_admissions = 0
        async def start(self, _params: dict[str, object]) -> dict[str, object]:
            raise CoreDataError(
                "no_compatible_execution_plan",
                "No route can satisfy the request.",
                {
                    "reason_code": "no_compatible_execution_plan",
                    "phase": "preflight",
                    "unmet_capabilities": ["native_web_search"],
                    "attempted_topologies": ["single_target"],
                    "rejected_routes": [],
                    "suggested_actions": ["disable_web_for_once"],
                },
            )

    response = asyncio.run(handle_async_message({
                "v": 24,
            "id": "preflight",
            "type": "model.run.start",
            "params": {"content": "search"},
        }, RejectingService(),  # type: ignore[arg-type]
    ))

    assert "error" not in response
    assert response["result"]["status"] == "preflight_rejected"
    assert response["result"]["rejection"]["phase"] == "preflight"
    assert "run" not in response["result"]


def test_resource_management_protocol_flow(tmp_path: Path) -> None:
    repository = CoreRepository(tmp_path / "data.db")
    group = repository.create_chat_group("Group")
    chat = repository.create_conversation("standard_chat", "Chat", "casual")
    moved = handle_message({
            "v": 24, "id": "move", "type": "conversation.move_chat",
        "params": {"conversation_id": chat["id"], "chat_group_id": group["id"], "target_index": 0},
    }, repository)
    assert moved["result"]["conversation"]["chat_group_id"] == group["id"]
    invalid = handle_message({
            "v": 24, "id": "bad-order", "type": "conversation.reorder",
        "params": {"kind": "standard_chat", "container_id": group["id"], "ordered_ids": "bad"},
    }, repository)
    assert invalid["id"] == "bad-order"
    assert invalid["error"]["code"] == "invalid_params"
