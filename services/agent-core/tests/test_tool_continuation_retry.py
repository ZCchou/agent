"""Host feedback and explicit retries cross the real compiler/provider/tool lifecycle."""
import asyncio
import copy
import json

import httpx
import pytest

from agent.protocol import handle_async_message
from agent.runtime import ChatRunService
from agent.storage import CoreDataError, CoreRepository
from agent.tool_contracts import ToolCall


def configuration():
    return dict(id="synthetic", name="Synthetic", kind="openai_compatible",
        base_url="https://synthetic.invalid/v1", api_key="synthetic-not-a-real-key",
        allowed_model_ids=["model"], automatic_model_ids=["model"],
        model_tool_modes={"model": "native"},
        model_capabilities={"model": {"text": "yes", "tools": "yes", "context_length": 65536}},
        invocation_routes=[dict(route_id="chat", route_role="primary", protocol="openai_chat",
            adapter_id="deepseek.openai_chat", config_fingerprint="synthetic", manifest_version="v1")],
        model_route_capabilities={"model": [dict(route_id="chat", capability_signature=signature,
            support_status="declared", verified_at=None, unsupported_reason_code=None,
            route_config_fingerprint="synthetic", evidence_kind="official_manifest")
            for signature in (["text"], ["function_tools", "text"])]})


def list_reply(identity):
    return {"tool_calls": [{"index": 0, "id": identity, "type": "function",
        "function": {"name": "fs_list", "arguments": '{"path":"."}'}}]}


def install_http(monkeypatch, replies):
    requests = []
    original = httpx.AsyncClient

    def respond(request):
        payload = json.loads(request.content)
        requests.append((str(request.url), payload))
        assert len(requests) <= len(replies), "unexpected model dispatch/replay"
        delta, finish = replies[len(requests) - 1]
        chunk = {"choices": [{"delta": delta, "finish_reason": finish}]}
        return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(respond), **kwargs))
    return requests


async def configure(service, value):
    response = await handle_async_message({"v": 17, "id": "host-feedback",
        "type": "provider.configure", "params": value}, service)
    assert "error" not in response, response


def successful_feedback(value, event, kind):
    feedback = copy.deepcopy(value)
    if kind in {"health", "combined"}:
        feedback["model_route_health"] = {"model": [dict(target_kind="model",
            provider_or_backend_id=value["id"], model_id="model", route_id="chat",
            capability_signature=event["data"]["capability_signature"], route_fingerprint="synthetic",
            health_status="ready", consecutive_failures=0, last_error_code=None,
            last_failure_at=None, retry_at=None, blocked_config_generation=None)]}
    if kind in {"support", "combined"}:
        for state in feedback["model_route_capabilities"]["model"]:
            if state["capability_signature"] == event["data"]["capability_signature"]:
                state.update(support_status="verified", verified_at="2026-10-23T12:00:00Z",
                             unsupported_reason_code=None)
    return feedback


async def fixture(tmp_path, sink, initial=None):
    repository = CoreRepository(tmp_path / "synthetic.db")
    project = repository.create_project("Synthetic", "coding")
    work = repository.create_conversation("project_work", "Synthetic work", project_id=project["id"])
    repository.update_project_model_policy(project["id"], "allow_online")
    repository.update_conversation_routing(work["id"], "fixed", "balanced", "synthetic", "model")
    service = ChatRunService(repository, sink)
    await configure(service, initial or configuration())
    return repository, project, work, service


async def start(service, work):
    response = await service.start(dict(conversation_id=work["id"], content="List the project directory.",
        idempotency_key="synthetic", selection=dict(mode="fixed", provider_profile_id="synthetic",
        model_id="model", routing_policy={"preset": "balanced"})))
    await service.tasks[response["run"]["id"]]
    return response["run"]["id"]


@pytest.mark.parametrize("feedback_kind", ["equivalent", "health", "support", "combined", "recovered", "vision_observation", "native_observation"])
def test_successful_host_feedback_allows_tool_continuation(tmp_path, monkeypatch, feedback_kind):
    requests = install_http(monkeypatch, [(list_reply("inspect"), "tool_calls"),
        ({"content": "Inspected the synthetic directory."}, "stop")])

    async def scenario():
        events = []
        initial = configuration()
        if feedback_kind == "recovered":
            initial = successful_feedback(initial, {"data": {"capability_signature": ["function_tools", "text"]}}, "health")
            initial["model_route_health"]["model"][0].update(health_status="degraded", consecutive_failures=1)
        if feedback_kind == "vision_observation":
            initial["model_capabilities"]["model"].update(vision="yes", vision_status="declared", vision_source="official_manifest", vision_verified_at=None, vision_error_code=None)
        if feedback_kind == "native_observation":
            initial["model_native_web_search"] = {"model": "declared"}
        async def sink(event):
            events.append(event)
            if event["event_type"] == "run.invocation_completed":
                feedback = successful_feedback(initial, event, "combined" if feedback_kind in {"recovered", "vision_observation", "native_observation"} else feedback_kind)
                if feedback_kind == "vision_observation":
                    feedback["model_capabilities"]["model"].update(vision_status="verified", vision_source="runtime", vision_verified_at="2026-10-23T12:00:00Z")
                if feedback_kind == "native_observation":
                    feedback["model_native_web_search"]["model"] = "verified"
                await configure(service, feedback)
            if event["event_type"] == "run.tool_requested":
                operation = event["data"]["operation"]
                service.tool_status(operation["id"], "executing")
                service.resolve_tool(operation["id"], True, '{"path":".","entries":[{"name":"sentinel.txt"}]}')
        repository, _, work, service = await fixture(tmp_path, sink, initial)
        run_id = await start(service, work)
        run = repository.get_model_run(run_id)
        assert run["status"] == "completed", (run["error_code"], run["error_message"])
        assert len(requests) == 2
        assert all(url == "https://synthetic.invalid/v1/chat/completions" for url, _ in requests)
        assert all(payload["model"] == "model" for _, payload in requests)
        assert "fs_list" in {tool["function"]["name"] for tool in requests[0][1]["tools"]}
        assert any(message.get("role") == "tool" and "sentinel.txt" in message["content"]
                   for message in requests[1][1]["messages"])
        assert [(item["tool_name"], item["status"]) for item in service.list_tool_operations(run_id)] == [("fs.list", "completed")]
        if feedback_kind in {"health", "combined"}:
            assert service.profiles["synthetic"].model_route_health["model"][0]["health_status"] == "ready"
        assert len([event for event in events if event["event_type"] == "run.invocation_completed"]) == 2
    asyncio.run(scenario())


@pytest.mark.parametrize("revocation", ["endpoint", "route_endpoint", "auth", "provider", "model",
    "route", "protocol", "adapter", "support", "automatic_models", "context_limit", "tool_mode", "project", "scope", "cooling_down", "configuration_blocked"])
def test_host_feedback_does_not_hide_real_dispatch_revocation(tmp_path, monkeypatch, revocation):
    requests = install_http(monkeypatch, [(list_reply("inspect"), "tool_calls")])

    async def scenario():
        async def sink(event):
            if event["event_type"] == "run.invocation_completed":
                value = successful_feedback(configuration(), event, "combined")
                if revocation == "endpoint": value["base_url"] = "https://changed.invalid/v1"
                elif revocation == "route_endpoint": value["invocation_routes"][0]["endpoint_base_url"] = "https://changed.invalid/v1"
                elif revocation == "auth": value["api_key"] = "synthetic-replacement"
                elif revocation == "provider": value["enabled"] = False
                elif revocation == "model": value["allowed_model_ids"] = []; value["automatic_model_ids"] = []
                elif revocation == "route": value["invocation_routes"][0]["config_fingerprint"] = "changed"
                elif revocation == "protocol": value["invocation_routes"][0]["protocol"] = "openai_responses"
                elif revocation == "adapter": value["invocation_routes"][0]["adapter_id"] = "openai.chat"
                elif revocation == "support": value["model_route_capabilities"]["model"][1]["support_status"] = "unsupported"
                elif revocation == "automatic_models": value["automatic_model_ids"] = []
                elif revocation == "context_limit": value["model_capabilities"]["model"]["context_length"] = 32768
                elif revocation == "tool_mode": value["model_tool_modes"]["model"] = "text_only"
                elif revocation in {"cooling_down", "configuration_blocked"}: value["model_route_health"]["model"][0]["health_status"] = revocation
                elif revocation == "project": repository.update_project_model_policy(project["id"], "local_only")
                elif revocation == "scope": repository.update_project_model_scope(project["id"], "restricted", [{"provider_profile_id": "synthetic", "model_id": "other"}])
                await configure(service, value)
            if event["event_type"] == "run.tool_requested":
                operation = event["data"]["operation"]
                service.tool_status(operation["id"], "executing")
                service.resolve_tool(operation["id"], True, '{"entries":[]}')
        repository, project, work, service = await fixture(tmp_path, sink)
        run_id = await start(service, work)
        run = repository.get_model_run(run_id)
        assert run["status"] == "failed"
        assert run["error_code"] in {"execution_plan_stale", "invocation_route_stale"}, run
        assert len(requests) == 1
        assert service.list_tool_operations(run_id)[0]["status"] == "completed"
    asyncio.run(scenario())


@pytest.mark.parametrize("strategy", ["default", "expanded_retry"])
def test_explicit_retry_after_tool_progress_preserves_audit_and_has_plan(tmp_path, monkeypatch, strategy):
    requests = install_http(monkeypatch, [(list_reply("inspect-original"), "tool_calls"),
        ({"content": "partial"}, "length"), ({}, "length"),
        (list_reply("inspect-new-explicit-call"), "tool_calls"),
        ({"content": "Explicit retry completed."}, "stop")])

    async def scenario():
        async def sink(event):
            if event["event_type"] == "run.tool_requested":
                operation = event["data"]["operation"]
                service.tool_status(operation["id"], "executing")
                service.resolve_tool(operation["id"], True, '{"entries":[]}')
        repository, _, work, service = await fixture(tmp_path, sink)
        run_id = await start(service, work)
        parent = repository.get_model_run(run_id)
        assert parent["error_code"] == "provider_response_incomplete", parent
        original_operations = service.list_tool_operations(run_id)
        assert len(original_operations) == 1
        with repository._connect() as connection:
            snapshots = [dict(row) for row in connection.execute("SELECT * FROM context_snapshots WHERE run_id=? ORDER BY round_no", (run_id,))]
            invocations = [dict(row) for row in connection.execute("SELECT * FROM invocation_specs WHERE plan_id=? ORDER BY round_no", (parent["execution_plan_id"],))]
        assert len(snapshots) == len(invocations) == 3
        retried = await service.retry(run_id, context_strategy=strategy)
        assert retried["execution_plan_id"], "explicit retry must have a persisted plan before dispatch"
        assert service.execution_repository.get_for_run(retried["id"])
        await service.tasks[retried["id"]]
        current = repository.get_model_run(retried["id"])
        assert current["status"] == "completed", (current["error_code"], current["error_message"])
        assert len(requests) == 5
        assert current["user_message_id"] == parent["user_message_id"]
        assert (current["id"] == parent["id"]) == (strategy == "default")
        if strategy == "expanded_retry": assert current["parent_run_id"] == parent["id"]
        assert service.list_tool_operations(run_id)[0] == original_operations[0]
        operations = service.list_tool_operations(current["id"])
        assert operations[-1]["id"] != original_operations[0]["id"]
        assert operations[-1]["provider_call_id"] == "inspect-new-explicit-call"
        with repository._connect() as connection:
            assert [dict(connection.execute("SELECT * FROM context_snapshots WHERE id=?", (row["id"],)).fetchone()) for row in snapshots] == snapshots
            assert [dict(connection.execute("SELECT * FROM invocation_specs WHERE id=?", (row["id"],)).fetchone()) for row in invocations] == invocations
            assert connection.execute("SELECT COUNT(*) FROM messages WHERE conversation_id=? AND role='user'", (work["id"],)).fetchone()[0] == 1
        # Already-finished operation identities still cannot be resolved/replayed.
        with pytest.raises(CoreDataError) as failure:
            service.resolve_tool(original_operations[0]["id"], True, "must not replay")
        assert failure.value.code == "tool_operation_not_waiting"
    asyncio.run(scenario())


@pytest.mark.parametrize("strategy", ["default", "expanded_retry", "parent_expanded_retry"])
def test_explicit_retry_recovers_legacy_child_only_via_bound_ancestor_plan(tmp_path, monkeypatch, strategy):
    requests = install_http(monkeypatch, [(list_reply("inspect-original"), "tool_calls"),
        ({"content": "partial"}, "length"), ({}, "length"),
        ({"content": "Recovered the explicit retry."}, "stop")])

    async def scenario():
        async def sink(event):
            if event["event_type"] == "run.tool_requested":
                operation = event["data"]["operation"]
                service.tool_status(operation["id"], "executing")
                service.resolve_tool(operation["id"], True, '{"entries":[]}')
            if event["event_type"] == "run.invocation_dispatched" and len(requests) == 2:
                assert service.execution_repository.get_for_run(event["run_id"]), "recovery must persist a real plan before dispatch"
        repository, _, work, service = await fixture(tmp_path, sink)
        parent_id = await start(service, work)
        parent = repository.get_model_run(parent_id)
        parent_plan = service.execution_repository.get_for_run(parent_id)
        original_operations = service.list_tool_operations(parent_id)
        orphan = repository.create_expanded_retry(parent_id)
        repository.transition_model_run(orphan["id"], {"queued"}, "failed", "invocation_spec_invalid")
        assert orphan["execution_plan_id"] is None
        recovered = await service.retry(parent_id if strategy == "parent_expanded_retry" else orphan["id"],
            context_strategy="expanded_retry" if strategy == "parent_expanded_retry" else strategy)
        assert recovered["execution_plan_id"], "old failed child needs a fresh validated plan before retry"
        assert recovered["execution_plan_id"] != parent["execution_plan_id"]
        await service.tasks[recovered["id"]]
        assert repository.get_model_run(recovered["id"])["status"] == "completed"
        assert recovered["user_message_id"] == parent["user_message_id"]
        assert service.list_tool_operations(parent_id) == original_operations
        assert not service.list_tool_operations(recovered["id"]), "finished ancestor tools must not automatically replay"
        assert len(requests) == 4
        assert service.execution_repository.get_for_run(parent_id) == parent_plan
        if strategy == "parent_expanded_retry":
            repeated = await service.retry(parent_id, context_strategy="expanded_retry")
            assert repeated["id"] == recovered["id"] and repeated["status"] == "completed"
            assert repeated["id"] not in service.tasks
            assert len(requests) == 4, "completed child must remain idempotent"
    asyncio.run(scenario())


@pytest.mark.parametrize("entry", ["child", "parent"])
@pytest.mark.parametrize("condition", ["no_ancestor", "cycle", "conversation", "message", "target",
    "tool_effect", "invocation", "usage", "draft", "budget_rejected", "revoked"])
def test_legacy_retry_without_safe_undispatched_lineage_fails_closed(tmp_path, monkeypatch, condition, entry):
    requests = install_http(monkeypatch, [(list_reply("inspect-original"), "tool_calls"),
        ({"content": "partial"}, "length"), ({}, "length")])

    async def scenario():
        async def sink(event):
            if event["event_type"] == "run.tool_requested":
                operation = event["data"]["operation"]
                service.tool_status(operation["id"], "executing")
                service.resolve_tool(operation["id"], True, '{"entries":[]}')
        repository, _, work, service = await fixture(tmp_path, sink)
        parent_id = await start(service, work)
        child = repository.create_expanded_retry(parent_id)
        attempt = repository.begin_run_attempt(child["id"])
        repository.finish_attempt(attempt["id"], "invocation_spec_invalid")
        repository.transition_model_run(child["id"], {"queued"}, "failed", "invocation_spec_invalid")
        if condition in {"no_ancestor", "cycle", "conversation", "message", "target"}:
            if condition == "conversation":
                column, value = "conversation_id", repository.create_conversation("project_work", "Other", project_id=work["project_id"])["id"]
            elif condition == "message":
                column, value = "user_message_id", repository._insert_message(work["id"], "user", "Another request")["id"]
            else:
                column, value = {"no_ancestor": ("parent_run_id", None),
                    "cycle": ("parent_run_id", child["id"]), "target": ("model_id", "other-model")}[condition]
            with repository._connect() as connection:
                connection.execute(f"UPDATE model_runs SET {column}=? WHERE id=?", (value, child["id"]))
        elif condition == "tool_effect":
            operation = service.tool_loop.repository.create(child["id"], attempt["id"], 1,
                ToolCall("already-executed", "fs.propose_changes", {"title": "Synthetic", "mutations": [{"kind": "mkdir", "path": "done"}]}))
            service.tool_loop.repository.transition(operation["id"], {"requested"}, "completed", result_summary="synthetic effect")
        elif condition == "invocation":
            with repository._connect() as connection:
                spec_id = connection.execute("SELECT id FROM invocation_specs LIMIT 1").fetchone()[0]
            repository.bind_attempt_invocation_spec(attempt["id"], spec_id)
        elif condition == "usage": repository.update_attempt_usage(attempt["id"], {"input_tokens": 12})
        elif condition == "draft": repository.checkpoint_draft(child["id"], attempt["id"], "Already produced output")
        elif condition == "budget_rejected": repository.transition_model_run(child["id"], {"failed"}, "failed", "routing_budget_rejected")
        elif condition == "revoked":
            value = configuration(); value["enabled"] = False
            await configure(service, value)
        with pytest.raises(CoreDataError):
            await service.retry(child["id"] if entry == "child" else parent_id,
                context_strategy="default" if entry == "child" else "expanded_retry")
        assert len(requests) == 3
        assert not repository.active_model_runs(), "failed recovery cannot leave an active planless run"
        assert service.execution_repository.get_for_run(child["id"]) is None
    asyncio.run(scenario())


@pytest.mark.parametrize("entry", ["new_child", "legacy_default", "legacy_expanded", "parent_repeat"])
def test_web_disabled_retry_plan_never_reenables_ancestor_web_requirement(tmp_path, monkeypatch, entry):
    requests = install_http(monkeypatch, [({"content": "Web-disabled retry completed."}, "stop")])

    async def scenario():
        tools = []
        async def sink(event):
            if event["event_type"] == "run.tool_requested":
                operation = event["data"]["operation"]
                tools.append((event["run_id"], operation["tool_name"]))
                service.tool_status(operation["id"], "executing")
                service.resolve_tool(operation["id"], True, '{"entries":[],"sources":[]}')
        repository, project, work, service = await fixture(tmp_path, sink)
        repository.update_project_web_access(project["id"], "allow_search")
        response = await service.start(dict(conversation_id=work["id"], content="Search the web and list project files.",
            idempotency_key="web-parent", web_search_request_mode="auto", web_search_global_enabled=True,
            web_search_backend_id="synthetic-host", web_search_backend_kind="searxng",
            selection=dict(mode="fixed", provider_profile_id="synthetic", model_id="model")))
        parent_id = response["run"]["id"]
        # Keep the real persisted web-required plan without dispatching an unrelated
        # Host presearch. Model dispatch under test belongs only to the explicit retry.
        await service.cancel(parent_id)
        repository.transition_model_run(parent_id, {"cancelled"}, "failed", "synthetic_pre_dispatch_failure")
        parent = repository.get_model_run(parent_id)
        assert parent["status"] == "failed" and parent["web_search_route"] != "none", parent
        assert requests == []
        if entry == "new_child":
            retried = await service.retry(parent_id, web_search_override="off")
        else:
            child = repository.create_web_disabled_retry(parent_id)
            repository.transition_model_run(child["id"], {"queued"}, "failed", "invocation_spec_invalid")
            if entry == "parent_repeat": retried = await service.retry(parent_id, web_search_override="off")
            else: retried = await service.retry(child["id"], context_strategy="default" if entry == "legacy_default" else "expanded_retry")
        assert retried["execution_plan_id"]
        plan = service.execution_repository.get_for_run(retried["id"])
        assert plan["web_route"] == "none"
        assert plan["resolved_intent"]["effective_requirements"]["web"] == "off"
        await service.tasks[retried["id"]]
        current = repository.get_model_run(retried["id"])
        assert current["status"] == "completed", (current["error_code"], current["error_message"], len(requests), tools)
        assert retried["web_search_request_mode"] == "off" and retried["web_search_route"] == "none"
        assert not any(name.startswith("web.") for identity, name in tools if identity == retried["id"])
        assert not {"web_search", "web_open"} & {tool["function"]["name"] for tool in requests[-1][1]["tools"]}
        assert len(requests) == 1
    asyncio.run(scenario())


def test_explicit_retry_continues_after_twenty_four_prior_tools(tmp_path, monkeypatch):
    replies = [(list_reply(f"prior-{index}"), "tool_calls") for index in range(24)]
    replies += [({"content": "Synthetic incomplete response."}, "length"), ({}, "length")]
    replies += [(list_reply(f"retry-{index}"), "tool_calls") for index in range(3)]
    replies += [({"content": "Retry finished after additional inspections."}, "stop")]
    requests = install_http(monkeypatch, replies)

    async def scenario():
        async def sink(event):
            if event["event_type"] == "run.tool_requested":
                operation = event["data"]["operation"]
                service.tool_status(operation["id"], "executing")
                service.resolve_tool(operation["id"], True, '{"path":".","entries":[]}')

        repository, _, work, service = await fixture(tmp_path, sink)
        run_id = await start(service, work)
        first = repository.get_model_run(run_id)
        assert first["status"] == "failed" and first["error_code"] == "provider_response_incomplete"
        retried = await service.retry(run_id)
        assert retried["id"] == run_id
        await service.tasks[run_id]
        final = repository.get_model_run(run_id)
        assert final["status"] == "completed", (final["error_code"], final["error_message"])
        operations = service.list_tool_operations(run_id)
        assert len(requests) == 30
        assert [item["sequence"] for item in operations] == list(range(1, 28))
        assert all(item["status"] == "completed" for item in operations)
        assert len({item["attempt_id"] for item in operations[:24]}) == 1
        assert len({item["attempt_id"] for item in operations[24:]}) == 1
        assert operations[0]["attempt_id"] != operations[-1]["attempt_id"]

    asyncio.run(scenario())


@pytest.mark.parametrize(("budget_name", "error_code"), [
    ("MAX_ROUNDS", "tool_call_limit_exceeded"),
    ("MAX_READ_OPERATIONS", "tool_read_limit_exceeded"),
])
def test_automatic_retry_keeps_allowance_but_explicit_retry_refreshes_it(tmp_path, monkeypatch, budget_name, error_code):
    requests = []
    manual = False
    manual_requests = 0
    original = httpx.AsyncClient

    def respond(request):
        nonlocal manual_requests
        requests.append(json.loads(request.content))
        if manual:
            manual_requests += 1
            delta = list_reply("manual-read") if manual_requests == 1 else {"content": "Manual retry finished."}
            finish = "tool_calls" if manual_requests == 1 else "stop"
        elif len(requests) == 3:
            raise httpx.ReadError("Synthetic transient failure after reads", request=request)
        elif len(requests) <= 5:
            delta, finish = list_reply(f"read-{len(requests)}"), "tool_calls"
        else:
            # The closing response also fails, so explicit retry remains available.
            assert not requests[-1].get("tools"), "automatic retries cannot refresh the tool budget"
            delta, finish = {}, "length"
        chunk = {"choices": [{"delta": delta, "finish_reason": finish}]}
        return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(respond), **kwargs))

    async def scenario():
        nonlocal manual

        async def sink(event):
            if event["event_type"] == "run.tool_requested":
                operation = event["data"]["operation"]
                service.tool_status(operation["id"], "executing")
                service.resolve_tool(operation["id"], True, '{"path":".","entries":[]}')

        repository, _, work, service = await fixture(tmp_path, sink)
        monkeypatch.setattr(service.tool_loop, budget_name, 3)
        run_id = await start(service, work)
        failed = repository.get_model_run(run_id)
        assert failed["status"] == "failed" and failed["error_code"] == error_code, failed["error_message"]
        operations = service.list_tool_operations(run_id)
        assert len(requests) == 6 and len(operations) == 3
        assert len({item["attempt_id"] for item in operations}) == 2
        manual = True
        retried = await service.retry(run_id)
        await service.tasks[retried["id"]]
        assert repository.get_model_run(run_id)["status"] == "completed"
        assert manual_requests == 2
        operations = service.list_tool_operations(run_id)
        assert [item["sequence"] for item in operations] == [1, 2, 3, 4]
        assert len({item["attempt_id"] for item in operations}) == 3

    asyncio.run(scenario())
