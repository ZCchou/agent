"""Malformed native output crosses HTTP, compiler, audit and completed tool boundaries."""
import asyncio
import json

import httpx
import pytest

from agent.execution.compiler import InvocationCompiler
from agent.model_invocation import ModelInvocationService
from agent.providers import OpenAICompatibleProvider, ProviderError, ProviderProfile
from test_tool_continuation_retry import configure, configuration, fixture, install_http, list_reply, start
from test_unified_invocation_v3 import model_spec


PROGRESS = "现在写核心引擎。"
SECRET = "RAW-ARGUMENT-SENTINEL"


def call_reply(identity, name, arguments, content=None):
    reply = {"tool_calls": [{"index": 0, "id": identity, "type": "function",
        "function": {"name": name, "arguments": arguments}}]}
    if content:
        reply["content"] = content
    return reply


def malformed(kind="json"):
    arguments = '{"path":"' + SECRET if kind == "json" else json.dumps({"path": ".", SECRET: True})
    return call_reply("rejected", "fs_list", arguments, PROGRESS)


@pytest.mark.parametrize("kind", ["json", "schema"])
def test_progress_malformed_call_recovers_after_completed_file_effect(tmp_path, monkeypatch, caplog, kind):
    proposal = call_reply("write-once", "fs_propose_changes", json.dumps({"title": "Create engine",
        "mutations": [{"kind": "create_text", "path": "engine.txt", "content": "completed file"}]}))
    requests = install_http(monkeypatch, [(proposal, "tool_calls"), (malformed(kind), "tool_calls"),
        (list_reply("corrected"), "tool_calls"), ({"content": "Verified complete."}, "stop")])

    async def scenario():
        events, executed = [], []
        async def sink(event):
            events.append(event)
            if event["event_type"] == "run.tool_requested":
                operation = event["data"]["operation"]
                executed.append(operation["provider_call_id"])
                service.tool_status(operation["id"], "executing")
                if operation["tool_name"] == "fs.propose_changes":
                    target = tmp_path / "engine.txt"
                    assert not target.exists(), "completed mutation must never replay"
                    target.write_text(operation["arguments"]["mutations"][0]["content"])
                service.resolve_tool(operation["id"], True, '{"completed":true}')
        repository, _, work, service = await fixture(tmp_path, sink)
        run_id = await start(service, work)
        run = repository.get_model_run(run_id)
        assert run["status"] == "completed", (run["error_code"], run["error_message"])
        assert executed == ["write-once", "corrected"]
        assert (tmp_path / "engine.txt").read_text() == "completed file"
        assert run["assistant_message"]["content"] == "Verified complete."
        progress = [json.loads(item["content"])["content"] for item in repository.get_conversation(work["id"])["messages"]
                    if item["event_type"] == "assistant_progress"]
        assert PROGRESS in progress
        assert run["draft"] is None
        assert [item["status"] for item in service.list_tool_operations(run_id)] == ["completed", "completed"]
        failed = [event["data"] for event in events if event["event_type"] == "run.invocation_failed"]
        assert len(failed) == 1 and failed[0]["produced_output"] is True
        assert failed[0]["tool_validation_reason"] == "invalid_" + kind
        assert SECRET not in json.dumps(failed) + caplog.text
        assert PROGRESS not in caplog.text
        assert configuration()["api_key"] not in caplog.text
        assert len(requests) == 4
        corrected_messages = requests[2][1]["messages"]
        assert SECRET not in json.dumps(corrected_messages)
        assert any(message.get("role") == "tool" for message in corrected_messages)
        with repository._connect() as connection:
            audit = list(connection.execute("SELECT status,error_code FROM invocation_specs WHERE plan_id=? ORDER BY round_no", (run["execution_plan_id"],)))
        assert [row[0] for row in audit] == ["completed", "failed", "completed", "completed"]
    asyncio.run(scenario())


@pytest.mark.parametrize("separated", [False, True])
def test_correction_is_bounded_per_tool_round_and_whole_attempt(tmp_path, monkeypatch, separated):
    replies = []
    for index in range(4 if separated else 2):
        replies.append((malformed(), "tool_calls"))
        if separated and index < 3:
            replies.append((list_reply(f"valid-{index}"), "tool_calls"))
    replies.append(({"content": "The invalid operation was not executed; prior results are retained."}, "stop"))
    requests = install_http(monkeypatch, replies)
    async def scenario():
        async def sink(event):
            if event["event_type"] == "run.tool_requested":
                operation = event["data"]["operation"]
                service.tool_status(operation["id"], "executing")
                service.resolve_tool(operation["id"], True, '{"entries":[]}')
        repository, _, work, service = await fixture(tmp_path, sink)
        run_id = await start(service, work)
        run = repository.get_model_run(run_id)
        assert run["status"] == "completed"
        assert "model_tool_protocol_failed" in run["assistant_message"]["content"]
        assert not requests[-1][1].get("tools")
        assert len(requests) == (8 if separated else 3)
        assert len(service.list_tool_operations(run_id)) == (3 if separated else 0)
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["mixed", "length", "fatal"])
def test_ambiguous_batch_incomplete_and_fatal_outputs_are_not_corrected(tmp_path, monkeypatch, failure):
    reply = malformed()
    finish = "tool_calls"
    if failure == "mixed":
        reply["tool_calls"][0]["index"] = 1
        reply["tool_calls"].insert(0, list_reply("valid-not-executed")["tool_calls"][0])
    elif failure == "length":
        finish = "length"
    if failure == "fatal":
        requests = []
        original = httpx.AsyncClient
        def respond(request):
            requests.append(request.url)
            return httpx.Response(401, json={"error": {"message": "Synthetic authentication failure"}})
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
            transport=httpx.MockTransport(respond), **kwargs))
    else:
        requests = install_http(monkeypatch, [(reply, finish),
            ({"content": "The invalid operation was not executed."}, "stop")])
    async def scenario():
        async def sink(_event):
            pass
        repository, _, work, service = await fixture(tmp_path, sink)
        run_id = await start(service, work)
        run = repository.get_model_run(run_id)
        expected = {"mixed": "model_tool_protocol_failed", "length": "provider_response_incomplete",
            "fatal": "authentication_failed"}[failure]
        if failure == "fatal":
            assert run["status"] == "failed" and run["error_code"] == expected
            assert "系统执行说明" in run["draft"]["content"]
        else:
            assert run["status"] == "completed" and expected in run["assistant_message"]["content"]
            assert not requests[-1][1].get("tools")
        assert len(requests) == (1 if failure == "fatal" else 2)
        assert not service.list_tool_operations(run_id)
    asyncio.run(scenario())


@pytest.mark.parametrize("revocation", ["provider", "project", "tool_scope"])
def test_correction_revalidates_dispatch_authority(tmp_path, monkeypatch, revocation):
    requests = install_http(monkeypatch, [(malformed(), "tool_calls")])
    async def scenario():
        async def sink(event):
            if event["event_type"] == "run.invocation_failed":
                if revocation == "project":
                    repository.update_project_model_policy(project["id"], "local_only")
                elif revocation == "tool_scope":
                    value = configuration()
                    value["model_tool_modes"] = {"model": "text_only"}
                    await configure(service, value)
                else:
                    value = configuration()
                    value["enabled"] = False
                    await configure(service, value)
        repository, project, work, service = await fixture(tmp_path, sink)
        run_id = await start(service, work)
        run = repository.get_model_run(run_id)
        assert run["status"] == "failed" and run["error_code"] in {"execution_plan_stale", "invocation_route_stale"}
        assert len(requests) == 1 and not service.list_tool_operations(run_id)
    asyncio.run(scenario())


@pytest.mark.parametrize("protocol", ["openai_chat", "openai_responses"])
@pytest.mark.parametrize("kind", ["json", "schema"])
def test_validation_diagnostic_logs_only_fixed_reason(monkeypatch, caplog, protocol, kind):
    original = httpx.AsyncClient
    arguments = malformed(kind)["tool_calls"][0]["function"]["arguments"]
    def respond(_request):
        if protocol == "openai_chat":
            chunks = [{"choices": [{"delta": malformed(kind), "finish_reason": "tool_calls"}]}]
        else:
            chunks = [{"type": "response.output_text.delta", "delta": PROGRESS},
                {"type": "response.output_item.done", "item": {"type": "function_call",
                    "call_id": "rejected", "name": "fs_list", "arguments": arguments}},
                {"type": "response.completed", "response": {"status": "completed"}}]
        return httpx.Response(200, text="".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
            + "data: [DONE]\n\n")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(respond), **kwargs))
    spec = model_spec(protocol_id=protocol, adapter_id="openai.chat" if protocol == "openai_chat" else "openai.responses",
        capability_signature=("function_tools", "text"), allowed_tool_names=("fs.list",),
        tool_transport="native_function_call")
    prepared = InvocationCompiler().compile_model(spec, [{"role": "user", "content": "Synthetic"}])
    provider = OpenAICompatibleProvider(ProviderProfile("synthetic", "Synthetic", "openai_compatible",
        "https://synthetic.invalid/v1", api_key=configuration()["api_key"], primary_protocol=protocol))
    with pytest.raises(ProviderError) as error:
        asyncio.run(ModelInvocationService().complete_prepared(provider, spec, prepared))
    assert error.value.code == "model_tool_protocol_failed"
    assert error.value.details == {"tool_validation_reason": "invalid_" + kind,
        "native_tool_correction_eligible": True}
    diagnostic = json.dumps([record.__dict__ for record in caplog.records], default=str)
    assert "tool_validation_reason" in diagnostic and "invalid_" + kind in diagnostic
    assert SECRET not in diagnostic and PROGRESS not in diagnostic
    assert configuration()["api_key"] not in diagnostic


@pytest.mark.parametrize("protocol,case", [
    (protocol, case) for protocol in ("openai_chat", "openai_responses")
    for case in ("invalid_first", "invalid_last", "multiple_invalid", "duplicate_identity", "missing_terminal", "unsuccessful_terminal")
] + [("openai_chat", "early_fragment")])
@pytest.mark.parametrize("preface", [False, True])
def test_uncertified_native_operations_are_not_executed_but_model_can_explain(tmp_path, monkeypatch, protocol, case, preface):
    original = httpx.AsyncClient
    requests = []
    bad = malformed()["tool_calls"][0]
    good = list_reply("valid-not-executed")["tool_calls"][0]
    calls = [bad]
    if case == "invalid_first":
        calls = [bad, good]
    elif case == "invalid_last":
        calls = [good, bad]
    elif case == "multiple_invalid":
        calls = [bad, dict(bad, id="rejected-again")]
    elif case == "duplicate_identity":
        calls = [bad, bad]
    calls = [dict(call, index=index) for index, call in enumerate(calls)]
    if case == "early_fragment":
        calls[0]["index"] = True

    def respond(request):
        requests.append(json.loads(request.content))
        if protocol == "openai_chat":
            delta = {"content": PROGRESS if preface else "", "tool_calls": calls} if len(requests) == 1 else {"content": "The operation was not executed."}
            chunks = [{"choices": [{"delta": delta,
                "finish_reason": "stop" if len(requests) > 1 else (None if case in {"missing_terminal", "early_fragment"} else (
                    "content_filter" if case == "unsuccessful_terminal" else "tool_calls"))}]}]
        elif len(requests) == 1:
            chunks = [{"type": "response.output_text.delta", "delta": PROGRESS if preface else ""}]
            chunks += [{"type": "response.output_item.done", "item": {"type": "function_call",
                "call_id": call["id"], "name": call["function"]["name"],
                "arguments": call["function"]["arguments"]}} for call in calls]
            if case != "missing_terminal":
                chunks.append({"type": "response.completed", "response": {
                    "status": "failed" if case == "unsuccessful_terminal" else "completed"}})
        else:
            chunks = [{"type": "response.output_text.delta", "delta": "The operation was not executed."},
                {"type": "response.completed", "response": {"status": "completed"}}]
        return httpx.Response(200, text="".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
            + "data: [DONE]\n\n")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(respond), **kwargs))

    async def scenario():
        async def sink(event):
            assert event["event_type"] != "run.tool_requested", "ambiguous batch must never execute"
        value = configuration()
        if protocol == "openai_responses":
            value["invocation_routes"][0].update(protocol=protocol, adapter_id="openai.responses")
        repository, _, work, service = await fixture(tmp_path, sink, value)
        run_id = await start(service, work)
        run = repository.get_model_run(run_id)
        assert run["status"] == "completed"
        assert "model_tool_protocol_failed" in run["assistant_message"]["content"]
        assert len(requests) == 2 and not service.list_tool_operations(run_id)
        assert not requests[-1].get("tools")
    asyncio.run(scenario())
