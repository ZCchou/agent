import asyncio
import json
from pathlib import Path

import pytest

from agent.storage import CoreDataError, CoreRepository
from agent.tool_contracts import ToolCall, validate_tool_call
from agent.tool_loop import ToolLoopService
from agent.tool_repository import ToolRepository


@pytest.fixture
def budget(tmp_path: Path):
    repository = CoreRepository(tmp_path / "budget.db")
    project = repository.create_project("Budget", "coding")
    conversation = repository.create_conversation(
        "project_work", "Budget", project_id=project["id"],
    )
    run = repository.start_model_run(
        content="Build and verify a project", idempotency_key="budget",
        provider_profile_id="builtin-demo", model_id="demo",
        conversation_id=conversation["id"],
    )
    repository.transition_model_run(run["id"], {"queued"}, "building_context")
    repository.transition_model_run(run["id"], {"building_context"}, "generating")
    attempt = repository.begin_run_attempt(run["id"])
    tools = ToolRepository(repository.database_path)

    async def sink(event):
        operation = event["data"]["operation"]
        output = json.dumps({
            "stdout": "verification", "stderr": "", "exit_code": 0,
            "duration_ms": 1, "timed_out": False, "cancelled": False,
            "stdout_truncated": False, "stderr_truncated": False,
        }) if operation["tool_name"] == "terminal.run" else "{}"
        service.resolve(operation["id"], success=True, output=output)

    service = ToolLoopService(tools, sink)
    return repository, tools, service, run, attempt


def proposal(identity: str, count: int) -> ToolCall:
    return ToolCall(identity, "fs.propose_changes", {
        "title": identity,
        "mutations": [{"kind": "mkdir", "path": f"{identity}-{i}"} for i in range(count)],
    })


def recorded(budget, call: ToolCall, status="completed", error_code=None):
    _, tools, _, run, attempt = budget
    operation = tools.create(run["id"], attempt["id"], len(tools.list_for_run(run["id"])) + 1, call)
    if status != "requested":
        tools.transition(operation["id"], {"requested"}, status, error_code=error_code)
    return operation


def admitted(budget, call: ToolCall, **budget_options):
    _, tools, service, run, attempt = budget
    return asyncio.run(service.request(
        run_id=run["id"], attempt_id=attempt["id"],
        sequence=len(tools.list_for_run(run["id"])) + 1, call=call, **budget_options,
    ))


def test_missing_parent_proposal_does_not_spend_corrected_write_budget(budget, monkeypatch):
    monkeypatch.setattr(budget[2], "MAX_FILE_MUTATIONS", 20)
    recorded(budget, proposal("missing-parents", 10), "failed", "missing_parent_dependency")
    recorded(budget, proposal("parents", 2))

    operation, resolution = admitted(budget, proposal("corrected-files", 10))

    assert operation["status"] == "completed" and resolution.success
    assert len(budget[1].list_for_run(budget[3]["id"])) == 3


def test_default_run_supports_multiple_full_change_sets(budget):
    recorded(budget, proposal("first", 20))
    operation, resolution = admitted(budget, proposal("second", 20))
    assert operation["status"] == "completed" and resolution.success


def test_two_hundred_mutations_across_batches_are_admitted(budget):
    for index in range(9):
        recorded(budget, proposal(f"batch-{index}", 20))
    operation, resolution = admitted(budget, proposal("last-batch", 20))
    assert operation["status"] == "completed" and resolution.success


def test_run_boundary_still_blocks_a_new_mutation_before_persistence(budget):
    for index in range(10):
        recorded(budget, proposal(f"batch-{index}", 20))
    with pytest.raises(CoreDataError, match="file mutation limit"):
        admitted(budget, proposal("over-budget", 1))
    assert len(budget[1].list_for_run(budget[3]["id"])) == 10


def test_rejected_changes_do_not_consume_file_budget(budget, monkeypatch):
    monkeypatch.setattr(budget[2], "MAX_FILE_MUTATIONS", 20)
    recorded(budget, proposal("rejected", 20), "rejected", "rejected_by_user")
    operation, resolution = admitted(budget, proposal("approved", 20))
    assert operation["status"] == "completed" and resolution.success


@pytest.mark.parametrize(("status", "error_code"), [
    ("failed", "recovery_required"),
    ("failed", "recovery_conflict"),
    ("failed", "rollback_incomplete"),
    ("failed", "unknown_error"),
    ("completed", "missing_parent_dependency"),
    ("interrupted", "missing_parent_dependency"),
    ("cancelled", "cancelled_by_user"),
    ("requested", None),
])
def test_uncertain_or_pending_writes_keep_their_budget_charge(budget, monkeypatch, status, error_code):
    monkeypatch.setattr(budget[2], "MAX_FILE_MUTATIONS", 20)
    recorded(budget, proposal("uncertain", 20), status, error_code)
    with pytest.raises(CoreDataError, match="file mutation limit"):
        admitted(budget, proposal("additional", 1))
    assert len(budget[1].list_for_run(budget[3]["id"])) == 1


def test_previous_attempt_writes_still_consume_the_run_budget(budget, monkeypatch):
    repository, tools, _, run, first = budget
    monkeypatch.setattr(budget[2], "MAX_FILE_MUTATIONS", 20)
    recorded(budget, proposal("first-attempt", 20))
    second = repository.begin_run_attempt(run["id"])
    assert second["id"] != first["id"]
    with pytest.raises(CoreDataError, match="file mutation limit"):
        admitted((repository, tools, budget[2], run, second), proposal("retry-extra", 1))


@pytest.mark.parametrize(("tool_name", "arguments"), [
    ("fs.read", {"path": "index.html"}),
    ("terminal.run", {"command": "node --test", "cwd": ".", "timeout_ms": 5000}),
])
def test_read_or_terminal_can_verify_legacy_over_budget_writes(budget, monkeypatch, tool_name, arguments):
    monkeypatch.setattr(budget[2], "MAX_FILE_MUTATIONS", 20)
    recorded(budget, proposal("first", 20))
    recorded(budget, proposal("legacy-extra", 1))
    operation, resolution = admitted(budget, ToolCall("verify", tool_name, arguments))
    assert operation["status"] == "completed" and resolution.success


def test_single_batch_and_content_limits_still_apply(budget, monkeypatch):
    with pytest.raises(CoreDataError, match="1 to 20"):
        validate_tool_call(proposal("oversized-batch", 21))
    monkeypatch.setattr(budget[2], "MAX_RESULT_BYTES", 128)
    call = ToolCall("large-body", "fs.propose_changes", {
        "title": "Large body", "mutations": [
            {"kind": "create_text", "path": "large.txt", "content": "x" * 256},
        ],
    })
    with pytest.raises(CoreDataError, match="content budget"):
        admitted(budget, call)
    assert budget[1].list_for_run(budget[3]["id"]) == []


def test_default_attempt_supports_more_than_twenty_four_calls(budget):
    for index in range(24):
        recorded(budget, ToolCall(f"inspect-{index}", "fs.list", {"path": "."}))
    operation, resolution = admitted(budget, ToolCall("inspect-more", "fs.list", {"path": "."}))
    assert operation["sequence"] == 25 and resolution.success


def test_attempt_call_budget_admits_256th_but_not_257th(budget):
    _, tools, _, run, attempt = budget
    for index in range(255):
        operation = tools.create(run["id"], attempt["id"], index + 1, proposal(f"declined-{index}", 1))
        tools.transition(operation["id"], {"requested"}, "rejected")
    operation, resolution = admitted(budget, ToolCall("last", "fs.read", {"path": "README.md"}))
    assert operation["sequence"] == 256 and resolution.success
    with pytest.raises(CoreDataError) as caught:
        admitted(budget, ToolCall("extra", "fs.read", {"path": "README.md"}))
    assert caught.value.code == "tool_call_limit_exceeded"
    assert caught.value.details == {"limit": 256, "used": 256, "scope": "attempt"}
    assert len(tools.list_for_run(run["id"])) == 256


def test_explicit_new_attempt_gets_call_allowance_without_resetting_sequence(budget, monkeypatch):
    repository, tools, service, run, _ = budget
    monkeypatch.setattr(service, "MAX_ROUNDS", 3)
    for index in range(3):
        recorded(budget, ToolCall(f"previous-{index}", "fs.list", {"path": "."}))
    second = repository.begin_run_attempt(run["id"])
    retry = repository, tools, service, run, second
    for index in range(3):
        operation, resolution = admitted(retry, ToolCall(f"new-{index}", "fs.list", {"path": "."}))
        assert operation["sequence"] == index + 4 and operation["attempt_id"] == second["id"]
        assert resolution.success
    with pytest.raises(CoreDataError) as caught:
        admitted(retry, ToolCall("one-too-many", "fs.list", {"path": "."}))
    assert caught.value.code == "tool_call_limit_exceeded"
    assert len(tools.list_for_run(run["id"])) == 6


def test_retry_read_allowance_does_not_charge_previous_attempt_reads(budget, monkeypatch):
    repository, tools, service, run, _ = budget
    monkeypatch.setattr(service, "MAX_READ_OPERATIONS", 2)
    recorded(budget, ToolCall("previous-read-1", "fs.read", {"path": "one.txt"}))
    recorded(budget, ToolCall("previous-read-2", "fs.read", {"path": "two.txt"}))
    second = repository.begin_run_attempt(run["id"])
    retry = repository, tools, service, run, second
    for index in range(2):
        admitted(retry, ToolCall(f"retry-read-{index}", "fs.read", {"path": "one.txt"}))
    with pytest.raises(CoreDataError) as caught:
        admitted(retry, ToolCall("read-extra", "fs.read", {"path": "one.txt"}))
    assert caught.value.code == "tool_read_limit_exceeded"
    assert caught.value.details == {"limit": 2, "used": 2, "scope": "attempt"}


def test_write_and_data_limits_have_distinct_diagnostic_codes(budget, monkeypatch):
    monkeypatch.setattr(budget[2], "MAX_FILE_MUTATIONS", 1)
    recorded(budget, proposal("spent", 1))
    with pytest.raises(CoreDataError) as write:
        admitted(budget, proposal("too-many", 1))
    assert write.value.code == "tool_mutation_limit_exceeded"
    assert write.value.details == {"limit": 1, "used": 1, "requested": 1, "scope": "run"}
    monkeypatch.setattr(budget[2], "MAX_RESULT_BYTES", 10)
    with pytest.raises(CoreDataError) as data:
        admitted(budget, proposal("oversized", 1))
    assert data.value.code == "tool_data_limit_exceeded"


def test_oversized_tool_result_reports_data_limit_and_remains_resolvable(budget, monkeypatch):
    service = budget[2]
    monkeypatch.setattr(service, "MAX_RESULT_BYTES", 8)

    async def sink(event):
        identity = event["data"]["operation"]["id"]
        with pytest.raises(CoreDataError) as caught:
            service.resolve(identity, success=True, output="123456789")
        assert caught.value.code == "tool_data_limit_exceeded"
        service.resolve(identity, success=False, output="large", error_code=caught.value.code)

    service.event_sink = sink
    operation, resolution = admitted(budget, ToolCall("read-large", "fs.read", {"path": "large.txt"}))
    assert operation["status"] == "failed" and not resolution.success
    assert resolution.error_code == "tool_data_limit_exceeded"


def test_automatic_attempts_can_share_one_call_allowance(budget, monkeypatch):
    repository, tools, service, run, first = budget
    monkeypatch.setattr(service, "MAX_ROUNDS", 3)
    recorded(budget, ToolCall("first-1", "fs.list", {"path": "."}))
    recorded(budget, ToolCall("first-2", "fs.list", {"path": "."}))
    second = repository.begin_run_attempt(run["id"])
    retry = repository, tools, service, run, second
    # Even if a caller lists only earlier technical attempts, current use is charged.
    lineage = (first["id"],)
    operation, resolution = admitted(retry, ToolCall("auto-1", "fs.list", {"path": "."}), budget_attempt_ids=lineage)
    assert operation["sequence"] == 3 and resolution.success
    with pytest.raises(CoreDataError) as caught:
        admitted(retry, ToolCall("auto-extra", "fs.list", {"path": "."}), budget_attempt_ids=lineage)
    assert caught.value.code == "tool_call_limit_exceeded"
    assert caught.value.details["used"] == 3
