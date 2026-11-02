"""Project work keeps usable file tools even without file-operation keywords."""
import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from agent.execution.domain import CapabilityRequirements, InvocationTarget
from agent.execution.planner import ExecutionPlanner
from agent.runtime import ChatRunService
from agent.storage import CoreRepository
from test_execution_routing import candidate, request


def routed(signature, *, status="declared", tool_mode="native", health="ready"):
    base = candidate("synthetic", "model")
    return replace(base, tool_mode=tool_mode, invocation_target=InvocationTarget(
        provider_profile_id="synthetic", model_id="model", route_id="chat",
        route_role="primary", protocol="openai_chat", adapter_id="deepseek.openai_chat",
        capability_signature=signature, route_manifest_version="synthetic-v1",
        route_config_fingerprint="synthetic", source_provenance="none",
        capability_status=status, health_status=health,
    ))


@pytest.mark.parametrize("selection", ["automatic", "fixed"])
@pytest.mark.parametrize("bound", [False, True])
def test_available_project_tools_choose_declared_tool_route_over_verified_text(selection, bound):
    text = routed(("text",), status="verified")
    tools = routed(("function_tools", "text"))
    plan = ExecutionPlanner().plan(request(
        selection_mode=selection, fixed_key=text.key if selection == "fixed" else None,
        coordinator_key=text.key if bound else None, candidates=(text, tools),
        requirements=CapabilityRequirements(files="available", tools="available"),
    ))
    assert plan.steps[-1].capability_signature == ("function_tools", "text")


@pytest.mark.parametrize("unusable", ["unsupported", "configuration_blocked", "text_only"])
def test_available_tools_do_not_invent_support_or_block_plain_answers(unusable):
    text = routed(("text",), status="verified")
    tools = routed(("function_tools", "text"),
                   status=unusable if unusable != "text_only" else "declared",
                   tool_mode="text_only" if unusable == "text_only" else "native")
    plan = ExecutionPlanner().plan(request(candidates=(text, tools),
        requirements=CapabilityRequirements(files="available", tools="available")))
    assert plan.steps[-1].capability_signature == ("text",)


def test_standard_chat_does_not_prefer_file_tools():
    plan = ExecutionPlanner().plan(request(candidates=(
        routed(("text",), status="verified"), routed(("function_tools", "text")),
    )))
    assert plan.steps[-1].capability_signature == ("text",)


def test_bound_model_keeps_compatible_text_fallback_before_another_models_tools():
    bad = routed(("function_tools", "text"), status="unsupported")
    text = routed(("text",), status="verified")
    other = replace(routed(("function_tools", "text")), model_id="other", invocation_target=replace(
        bad.invocation_target, model_id="other", capability_status="declared"))
    plan = ExecutionPlanner().plan(request(candidates=(bad, text, other), coordinator_key=text.key,
        requirements=CapabilityRequirements(files="available", tools="available")))
    assert plan.coordinator_model_id == "model"
    assert plan.steps[-1].model_id == "model"
    assert plan.steps[-1].capability_signature == ("text",)


def test_project_search_keeps_file_tools_on_the_final_route():
    text = routed(("text",), status="verified")
    tools = routed(("function_tools", "text"))
    search = replace(text, native_search_status="declared", invocation_target=replace(
        text.invocation_target, route_id="search", route_role="native_search",
        capability_signature=("native_web_search", "text"), source_provenance="structured"))
    requirements = CapabilityRequirements(files="available", tools="available", web="required")
    planner = ExecutionPlanner()
    route = planner.resolve_web_route(requirements=requirements, candidates=(text, tools, search),
        coordinator_key=text.key, selection_mode="automatic", host_backend_id=None,
        host_backend_kind=None, request_mode="auto")
    plan = planner.plan(request(requirements=requirements, candidates=(text, tools, search),
                                web_route=route.route))
    assert plan.steps[-1].capability_signature == ("function_tools", "text")
    assert any(step.step_type == "web_research" for step in plan.steps)


def test_new_project_can_enable_search_and_explicit_restrictions_survive_restart(tmp_path):
    repository = CoreRepository(tmp_path / "policies.db")
    project = repository.create_project("Synthetic", "coding")
    work = repository.create_conversation("project_work", "Search", project_id=project["id"])
    repository.update_conversation_web_search(work["id"], "auto")
    repository.update_project_web_access(project["id"], "disabled")
    repository.update_project_model_policy(project["id"], "local_only")
    restarted = CoreRepository(tmp_path / "policies.db")
    with pytest.raises(Exception, match="not allowed web search"):
        restarted.update_conversation_web_search(work["id"], "auto")
    assert restarted.get_conversation(work["id"])["model_access_policy"] == "local_only"


@pytest.mark.parametrize("content", ["就按这个来，直接帮我做", "直接放根目录、不用看", "继续"])
def test_confirmation_receives_real_file_tools_and_completes_change_set(tmp_path, monkeypatch, content):
    requests = []
    real_client = httpx.AsyncClient
    mutations = [{"kind": "mkdir", "path": "css"}, {"kind": "mkdir", "path": "js"}] + [
                 {"kind": "create_text", "path": path, "content": "synthetic content"}
                 for path in ("index.html", "css/style.css", "js/net.js", "js/data.js",
                              "js/viz.js", "js/main.js", "README.md")]

    def respond(req):
        payload = json.loads(req.content)
        requests.append(payload)
        names = {item["function"]["name"] for item in payload.get("tools", [])}
        if "fs_propose_changes" not in names:
            delta, reason = {"content": "No tools supplied"}, "stop"
        elif len(requests) == 1:
            delta, reason = {"tool_calls": [{"index": 0, "id": "create-page", "type": "function",
                "function": {"name": "fs_propose_changes", "arguments": json.dumps({
                    "title": "Create synthetic page", "mutations": mutations})}}]}, "tool_calls"
        else:
            delta, reason = {"content": "Created seven files."}, "stop"
        body = {"choices": [{"delta": delta, "finish_reason": reason}]}
        return httpx.Response(200, text="data: " + json.dumps(body) + "\n\ndata: [DONE]\n\n")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(respond), **kwargs))

    async def no_advisor(*args, **kwargs):
        return None, "synthetic_advisor_unavailable"

    monkeypatch.setattr(ChatRunService, "_preflight_intent_advisor", no_advisor)

    async def scenario():
        repository = CoreRepository(tmp_path / "run.db")
        project = repository.create_project("Synthetic", "coding")
        work = repository.create_conversation("project_work", "Build page", project_id=project["id"])
        repository.update_conversation_routing(work["id"], "automatic", "balanced", "synthetic", "model")
        repository._insert_message(work["id"], "assistant", "Plan: create a seven-file browser page.")
        events = []

        async def sink(event):
            events.append(event)
            if event["event_type"] == "run.tool_requested":
                operation = event["data"]["operation"]
                service.tool_status(operation["id"], "executing")
                service.resolve_tool(operation["id"], True, '{"changed_files":7}')

        service = ChatRunService(repository, sink)
        service.configure_provider({"id": "synthetic", "name": "Synthetic", "kind": "openai_compatible",
            "base_url": "https://synthetic.invalid", "api_key": "synthetic",
            "allowed_model_ids": ["model"], "automatic_model_ids": ["model"],
            "model_tool_modes": {"model": "native"},
            "model_capabilities": {"model": {"text": "yes", "tools": "yes", "context_length": 65536}},
            "invocation_routes": [{"route_id": "chat", "route_role": "primary", "protocol": "openai_chat",
                "adapter_id": "deepseek.openai_chat", "manifest_version": "synthetic-v1",
                "config_fingerprint": "synthetic", "source_provenance": "none"}],
            "model_route_capabilities": {"model": [
                {"route_id": "chat", "capability_signature": ["text"], "status": "verified",
                 "route_config_fingerprint": "synthetic"},
                {"route_id": "chat", "capability_signature": ["function_tools", "text"], "status": "declared",
                 "route_config_fingerprint": "synthetic"},
            ]}})
        started = await service.start({"conversation_id": work["id"], "content": content,
            "idempotency_key": "confirm", "selection": {"mode": "automatic",
                "provider_profile_id": "synthetic", "model_id": "model", "routing_policy": {"preset": "balanced"}}})
        run_id = started["run"]["id"]
        await service.tasks[run_id]
        completed = repository.get_model_run(run_id)
        assert completed["status"] == "completed", (completed.get("error_code"), completed.get("error_message"), requests)
        operations = service.list_tool_operations(run_id)
        assert [(op["tool_name"], op["status"]) for op in operations] == [("fs.propose_changes", "completed")]
        assert operations[0]["arguments"]["mutations"] == mutations
        assert repository.get_model_run(run_id)["status"] == "completed"
        answers = [m["content"] for m in repository.get_conversation(work["id"])["messages"]
                   if m["role"] == "assistant"]
        assert answers == ["Plan: create a seven-file browser page.", "Created seven files."]
        assert len(requests) == 2
        assert {item["function"]["name"] for item in requests[0]["tools"]} == {
            "fs_list", "fs_search", "fs_read", "fs_propose_changes", "terminal_run",
            "skills_load", "skills_read_resource", "skills_save"}
        assert "tool" in {item["role"] for item in requests[1]["messages"]}
        assert not any(event["event_type"] == "run.waiting_permission" for event in events)

    asyncio.run(scenario())
