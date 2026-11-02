from pathlib import Path

import pytest

from agent.storage import CoreDataError, CoreRepository
from agent.tool_contracts import ToolCall, ToolContractRegistry, validate_tool_call
from agent.tool_repository import ToolRepository


def started_run(tmp_path: Path) -> tuple[CoreRepository, ToolRepository, dict, dict]:
    repository = CoreRepository(tmp_path / "agent.db")
    project = repository.create_project("Code", "coding")
    conversation = repository.create_conversation(
        "project_work", "Work", project_id=project["id"],
    )
    run = repository.start_model_run(
        content="inspect the project", idempotency_key="tool-test",
        provider_profile_id="builtin-demo", model_id="demo",
        conversation_id=conversation["id"],
    )
    repository.transition_model_run(run["id"], {"queued"}, "building_context")
    repository.transition_model_run(run["id"], {"building_context"}, "generating")
    attempt = repository.begin_run_attempt(run["id"])
    return repository, ToolRepository(repository.database_path), run, attempt


def test_tool_contract_rejects_unknown_fields_and_oversized_batches() -> None:
    with pytest.raises(CoreDataError, match="unsupported fields"):
        validate_tool_call(ToolCall("call-1", "fs.read", {"path": "a.txt", "secret": True}))
    with pytest.raises(CoreDataError, match="1 to 20"):
        validate_tool_call(ToolCall("call-2", "fs.propose_changes", {
            "title": "too much", "mutations": [
                {"kind": "mkdir", "path": f"d-{index}"} for index in range(21)
            ],
        }))


def test_web_search_tool_requests_public_links_instead_of_internal_ids() -> None:
    definition = ToolContractRegistry.definitions({"web.search"})[0]
    description = definition["function"]["description"].casefold()
    assert "source ids" not in description
    assert "markdown links" in description


def test_tool_operation_is_idempotent_and_does_not_store_result_body(tmp_path: Path) -> None:
    _, tools, run, attempt = started_run(tmp_path)
    call = ToolCall("provider-call", "fs.read", {"path": "README.md", "offset": 0})
    created = tools.create(run["id"], attempt["id"], 1, call)
    duplicate = tools.create(run["id"], attempt["id"], 1, call)
    assert duplicate["id"] == created["id"]

    completed = tools.transition(
        created["id"], {"requested"}, "completed", result_summary="read 120 bytes",
    )
    assert completed["result_summary"] == "read 120 bytes"
    assert completed["result_hash"]
    assert "file body" not in str(completed)


def test_reused_provider_call_with_different_arguments_is_rejected(tmp_path: Path) -> None:
    _, tools, run, attempt = started_run(tmp_path)
    tools.create(run["id"], attempt["id"], 1, ToolCall("same", "fs.read", {"path": "a"}))
    with pytest.raises(CoreDataError, match="reused"):
        tools.create(run["id"], attempt["id"], 2, ToolCall("same", "fs.read", {"path": "b"}))


def test_active_tool_operations_are_interrupted_on_restart(tmp_path: Path) -> None:
    _, tools, run, attempt = started_run(tmp_path)
    operation = tools.create(
        run["id"], attempt["id"], 1, ToolCall("call", "fs.list", {"path": "."}),
    )
    assert tools.interrupt_active() == [operation["id"]]
    assert tools.get(operation["id"])["status"] == "interrupted"


def test_provider_call_ids_may_repeat_on_a_new_attempt(tmp_path: Path) -> None:
    repository, tools, run, first_attempt = started_run(tmp_path)
    call = ToolCall("provider-call", "fs.list", {"path": "."})
    first = tools.create(run["id"], first_attempt["id"], 1, call)
    tools.transition(first["id"], {"requested"}, "completed", result_summary="done")
    repository.finish_attempt(first_attempt["id"], "connection_reset")
    second_attempt = repository.begin_run_attempt(run["id"])
    second = tools.create(run["id"], second_attempt["id"], 2, call)
    assert second["id"] != first["id"]
