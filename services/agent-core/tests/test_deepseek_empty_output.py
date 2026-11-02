"""Synthetic regressions for reasoning controls and non-successful completion."""
import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from agent.execution.compiler import InvocationCompiler
from agent.model_invocation import ModelInvocationService
from agent.providers import GenerationOptions, OpenAICompatibleProvider, ProviderError, ProviderEvent, ProviderProfile
from agent.runtime import ChatRunService
from agent.storage import CoreRepository
from test_unified_invocation_v3 import model_spec


def install_http(monkeypatch, chunks):
    requests = []
    real_client = httpx.AsyncClient

    def respond(request):
        requests.append(json.loads(request.content))
        body = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
        return httpx.Response(200, text=body + "data: [DONE]\n\n")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(respond), **kwargs))
    return requests


def chunk(delta, finish=None):
    return {"choices": [{"delta": delta, "finish_reason": finish}]}


def profile():
    return ProviderProfile("synthetic", "Synthetic", "openai_compatible", "https://synthetic.invalid")


def spec(**kwargs):
    return model_spec(route_id="deepseek.primary", protocol_id="openai_chat",
                      adapter_id="deepseek.openai_chat", max_output_tokens=16384, **kwargs)


@pytest.mark.parametrize("effort,thinking,wire_effort", [
    ("none", "disabled", None), ("low", "enabled", "low"),
    ("medium", "enabled", "high"), ("high", "enabled", "high"),
])
def test_frozen_reasoning_setting_reaches_deepseek_chat(monkeypatch, effort, thinking, wire_effort):
    requests = install_http(monkeypatch, [chunk({"content": "Synthetic answer"}, "stop")])
    invocation = spec(reasoning_effort=effort)
    prepared = InvocationCompiler().compile_model(invocation, [{"role": "user", "content": "Synthetic request"}])
    asyncio.run(ModelInvocationService().complete_prepared(OpenAICompatibleProvider(profile()), invocation, prepared))
    assert requests[0].get("thinking") == {"type": thinking}
    assert requests[0].get("reasoning_effort") == wire_effort
    assert requests[0]["max_tokens"] == 16384


@pytest.mark.parametrize("adapter", ["openai.chat", "kimi.openai_chat", "deepseek.openai_chat", None])
def test_unrelated_and_legacy_chat_callers_get_no_deepseek_controls(monkeypatch, adapter):
    requests = install_http(monkeypatch, [chunk({"content": "Synthetic answer"}, "stop")])
    options = GenerationOptions(invocation_adapter_id=adapter) if adapter in {None, "deepseek.openai_chat"} else InvocationCompiler().compile_model(
        model_spec(protocol_id="openai_chat", adapter_id=adapter, reasoning_effort="high"),
        [{"role": "user", "content": "Synthetic request"}],
    ).options

    async def scenario():
        return [event async for event in OpenAICompatibleProvider(profile()).stream_events(
            "model", [{"role": "user", "content": "Synthetic request"}], tools=False, options=options)]

    asyncio.run(scenario())
    assert "thinking" not in requests[0]
    assert "reasoning_effort" not in requests[0]


@pytest.mark.parametrize("responses,expected_status,expected_codes,tool_count", [
    ([{"type": "text", "content": ""}], "failed", ["provider_empty_output"], 0),
    ([{"type": "text", "content": " \n\t"}], "failed", ["provider_empty_output"], 0),
    (["malformed", {"type": "text", "content": "Final synthetic answer"}],
     "completed", ["model_tool_protocol_failed", None], 0),
    ([{"type": "tool_call", "id": "read-1", "name": "fs.read", "arguments": {"path": "README.md"}},
      {"type": "text", "content": "Final synthetic answer"}], "completed", [None, None], 1),
    (["malformed", "malformed"], "failed", ["model_tool_protocol_failed", "model_tool_protocol_failed"], 0),
    ([{"type": "tool_call", "id": "bad-1", "name": "fs.read", "arguments": {"path": 12}},
      {"type": "text", "content": "Final synthetic answer"}], "completed", ["invalid_tool_call", None], 0),
    ([{"type": "tool_call", "id": "bad-1", "name": "fs.read", "arguments": {"path": 12}},
      {"type": "tool_call", "id": "bad-2", "name": "fs.read", "arguments": {"path": 12}}],
     "failed", ["invalid_tool_call", "model_tool_contract_failed"], 0),
])
def test_structured_decoded_result_controls_invocation_success(
    tmp_path, monkeypatch, responses, expected_status, expected_codes, tool_count,
):
    requests = []

    class Provider:
        def __init__(self, profile):
            self.profile = profile

        async def stream_events(self, model, messages, *, tools, options):
            index = len(requests)
            requests.append(messages)
            assert not tools  # Compiler selected the structured transport.
            if index >= len(responses):
                # The single text-only closing call also fails, retaining the
                # original failure for explicit retry and audit assertions.
                raise ProviderError("provider_response_incomplete", "Synthetic closing failure")
            response = responses[index]
            yield ProviderEvent("text_delta", text=response if isinstance(response, str) else json.dumps(response))
            yield ProviderEvent("usage", usage={"input_tokens": 12, "output_tokens": 8})
            yield ProviderEvent("finish", finish_reason="stop")

    monkeypatch.setattr("agent.runtime.create_provider", Provider)

    async def scenario():
        repository = CoreRepository(tmp_path / "structured.db")
        project = repository.create_project("Synthetic project", "coding")
        conversation = repository.create_conversation("project_work", "Synthetic", project_id=project["id"])
        events = []

        async def sink(event):
            events.append(event)
            if event["event_type"] == "run.tool_requested":
                operation = event["data"]["operation"]
                service.tool_status(operation["id"], "executing")
                service.resolve_tool(operation["id"], True, "Synthetic file content")

        service = ChatRunService(repository, sink)
        service.profiles["builtin-demo"] = replace(service.profiles["builtin-demo"],
            model_tool_modes={"demo": "structured"})
        started = await service.start({"conversation_id": conversation["id"], "content": "Read README.md",
            "idempotency_key": "structured", "provider_profile_id": "builtin-demo", "model_id": "demo"})
        run_id = started["run"]["id"]
        await service.tasks[run_id]
        run = repository.get_model_run(run_id)
        assert run["status"] == expected_status
        assert run["finished_at"]
        with repository._connect() as connection:
            attempt = dict(connection.execute("SELECT * FROM run_attempts WHERE run_id=?", (run_id,)).fetchone())
            invocations = [dict(row) for row in connection.execute(
                "SELECT * FROM invocation_specs WHERE plan_id=? ORDER BY round_no", (run["execution_plan_id"],))]
        audit_codes = expected_codes + (["provider_response_incomplete"] if expected_status == "failed" else [])
        assert [item["error_code"] for item in invocations] == audit_codes
        assert [item["status"] for item in invocations] == ["failed" if code else "completed" for code in audit_codes]
        assert all(item["finished_at"] for item in invocations)
        assert attempt["finished_at"]
        assert (attempt["input_tokens"], attempt["output_tokens"]) == (12, 8)
        assert len(requests) == len(audit_codes)
        assert len(service.list_tool_operations(run_id)) == tool_count
        history = repository.get_conversation(conversation["id"])["messages"]
        answers = [item for item in history if item["role"] == "assistant"]
        if expected_status == "failed":
            assert attempt["error_code"] == run["error_code"] == expected_codes[-1]
            assert not answers
            assert not any(event["event_type"] == "run.invocation_completed" for event in events)
            assert all(not event["data"]["produced_output"] for event in events
                       if event["event_type"] == "run.invocation_failed")
        else:
            assert len(answers) == 1 and answers[0]["content"] == "Final synthetic answer"

    asyncio.run(scenario())


@pytest.mark.parametrize("partial,tool", [("", False), ("Partial synthetic answer", False), ("", True)])
def test_chat_length_preserves_usage_and_text_but_never_yields_tools(monkeypatch, partial, tool):
    chunks = [chunk({"reasoning_content": "PRIVATE_SYNTHETIC_REASONING"})]
    if partial:
        chunks.append(chunk({"content": partial}))
    if tool:
        chunks.append(chunk({"tool_calls": [{"index": 0, "id": "call", "function": {
            "name": "fs_read", "arguments": '{"path":'}}]}))
    chunks.extend([chunk({}, "length"), {"choices": [], "usage": {"prompt_tokens": 2115, "completion_tokens": 16384}}])
    requests = install_http(monkeypatch, chunks)
    events = []

    async def scenario():
        async for event in OpenAICompatibleProvider(profile()).stream_events(
            "model", [{"role": "user", "content": "Synthetic request"}], tools=tool,
            options=GenerationOptions(max_output_tokens=16384)):
            events.append(event)

    with pytest.raises(ProviderError) as caught:
        asyncio.run(scenario())
    assert caught.value.code == "provider_response_incomplete"
    assert caught.value.details["incomplete_reason"] == "length"
    assert caught.value.details["text_character_count"] == len(partial)
    assert "PRIVATE_SYNTHETIC_REASONING" not in str(caught.value.details)
    assert "".join(event.text for event in events) == partial
    assert [event.usage["output_tokens"] for event in events if event.usage] == [16384]
    assert not any(event.kind == "tool_call" for event in events)
    assert len(requests) == 1


@pytest.mark.parametrize("finish,partial,code,hidden", [
    ("length", "", "provider_response_incomplete", False),
    ("length", "Partial synthetic answer", "provider_response_incomplete", False),
    ("stop", "", "provider_empty_output", False),
    ("length", "", "provider_response_incomplete", True),
    ("stop", "", "provider_empty_output", True),
])
def test_runtime_failure_agrees_with_invocation_attempt_usage_and_explicit_retry(tmp_path, monkeypatch, finish, partial, code, hidden):
    calls = []

    class Provider:
        def __init__(self, bound_profile):
            self.profile = bound_profile

        async def stream_events(self, model, messages, *, tools, options):
            calls.append(options)
            if not hidden and len(calls) == 2:
                raise ProviderError("provider_response_incomplete", "Synthetic closing failure")
            if len(calls) > (1 if hidden else 2):
                yield ProviderEvent("text_delta", text="Final synthetic answer")
                yield ProviderEvent("finish", finish_reason="stop")
                return
            yield ProviderEvent("activity", opaque_reasoning_delta="PRIVATE_SYNTHETIC_REASONING")
            if partial:
                yield ProviderEvent("text_delta", text=partial[:7])
                yield ProviderEvent("text_delta", text=partial[7:])
            yield ProviderEvent("usage", usage={"input_tokens": 2115, "output_tokens": 16384})
            yield ProviderEvent("finish", finish_reason=finish)

    monkeypatch.setattr("agent.runtime.create_provider", Provider)
    if hidden:
        # Exercise the actual hidden/specialist dispatch boundary in the same
        # persisted attempt lifecycle, without needing a real image or provider.
        async def generate_hidden(self, run, attempt_id, provider, messages, *args, **kwargs):
            content, _, _ = await self._invoke_planned_step(
                run, kwargs["execution_plan"], kwargs["execution_step"],
                {"id": attempt_id, "attempt_number": kwargs["attempt_number"]},
                provider, messages, max_output_tokens=16384,
            )
            return content

        monkeypatch.setattr(ChatRunService, "_generate_with_tools", generate_hidden)

    async def scenario():
        repository = CoreRepository(tmp_path / "synthetic.db")
        conversation = repository.create_conversation("standard_chat", "Synthetic", "casual")
        events = []

        async def sink(event):
            events.append(event)

        service = ChatRunService(repository, sink)
        started = await service.start({"conversation_id": conversation["id"], "content": "Synthetic request",
            "idempotency_key": "synthetic", "provider_profile_id": "builtin-demo", "model_id": "demo"})
        run_id = started["run"]["id"]
        await service.tasks[run_id]
        failed = repository.get_model_run(run_id)
        assert failed["status"] == "failed"
        assert failed["error_code"] == code
        assert failed["finished_at"]
        initial_calls = 1 if hidden else 2
        assert len(calls) == initial_calls
        if partial:
            assert partial in failed["draft"]["content"]
        assert "系统执行说明" in failed["draft"]["content"]
        with repository._connect() as connection:
            attempt = dict(connection.execute("SELECT * FROM run_attempts WHERE run_id=?", (run_id,)).fetchone())
            invocation = dict(connection.execute("SELECT * FROM invocation_specs WHERE id=?", (attempt["invocation_spec_id"],)).fetchone())
        assert attempt["error_code"] == code and attempt["finished_at"]
        assert (attempt["input_tokens"], attempt["output_tokens"]) == (2115, 16384)
        assert invocation["status"] == "failed" and invocation["error_code"] == code
        assert invocation["finished_at"]
        assert "PRIVATE_SYNTHETIC_REASONING" not in json.dumps(events)
        history = repository.get_conversation(conversation["id"])["messages"]
        assert [m["role"] for m in history if m.get("event_type") != "assistant_progress"] == ["user"]
        assert "PRIVATE_SYNTHETIC_REASONING" not in json.dumps(history)
        # Explicit retry still checks the current provider authority before dispatch.
        original = service.profiles["builtin-demo"]
        service.profiles["builtin-demo"] = replace(original, enabled=False)
        await service.retry(run_id)
        await service.tasks[run_id]
        assert repository.get_model_run(run_id)["status"] == "failed"
        assert len(calls) == initial_calls
        service.profiles["builtin-demo"] = original
        await service.retry(run_id)
        await service.tasks[run_id]
        assert repository.get_model_run(run_id)["status"] == "completed"
        history = repository.get_conversation(conversation["id"])["messages"]
        assert [m["role"] for m in history if m.get("event_type") != "assistant_progress"] == ["user", "assistant"]
        assert history[-1]["content"] == "Final synthetic answer"
        assert len(calls) == initial_calls + 1

    asyncio.run(scenario())


@pytest.mark.parametrize("prepared_call", [True, False])
@pytest.mark.parametrize("finish,code", [("length", "provider_response_incomplete"), ("stop", "provider_empty_output")])
def test_hidden_invocations_reject_empty_or_incomplete_results(prepared_call, finish, code):
    class Provider:
        profile = profile()

        async def stream_events(self, *args, **kwargs):
            yield ProviderEvent("usage", usage={"input_tokens": 11, "output_tokens": 16})
            yield ProviderEvent("finish", finish_reason=finish)

    async def scenario():
        service = ModelInvocationService()
        if prepared_call:
            invocation = spec()
            prepared = InvocationCompiler().compile_model(invocation, [{"role": "user", "content": "Synthetic request"}])
            await service.complete_prepared(Provider(), invocation, prepared)
        else:
            await service.complete_with_events(Provider(), "model", [{"role": "user", "content": "Synthetic request"}], options=GenerationOptions())

    with pytest.raises(ProviderError) as caught:
        asyncio.run(scenario())
    assert caught.value.code == code
    assert caught.value.usage == {"input_tokens": 11, "output_tokens": 16}


def test_real_deepseek_http_runtime_persists_late_usage_and_failed_terminal_state(tmp_path, monkeypatch):
    requests = install_http(monkeypatch, [
        chunk({"reasoning_content": "PRIVATE_SYNTHETIC_REASONING"}), chunk({}, "length"),
        {"choices": [], "usage": {"prompt_tokens": 2115, "completion_tokens": 16384}},
    ])

    async def scenario():
        repository = CoreRepository(tmp_path / "http-runtime.db")
        conversation = repository.create_conversation("standard_chat", "Synthetic", "casual")

        async def sink(event):
            pass

        service = ChatRunService(repository, sink)
        service.configure_provider({
            "id": "synthetic", "name": "Synthetic", "kind": "openai_compatible",
            "base_url": "https://synthetic.invalid", "primary_protocol": "openai_chat",
            "allowed_model_ids": ["model"], "model_tool_modes": {"model": "text_only"},
            "model_capabilities": {"model": {"text": "yes", "context_length": 65536, "max_output_tokens": 16384}},
            "invocation_routes": [{"route_id": "deepseek.primary", "route_role": "primary",
                "protocol": "openai_chat", "adapter_id": "deepseek.openai_chat",
                "endpoint_base_url": "https://synthetic.invalid", "source": "official_preset",
                "manifest_version": "v3.2", "config_fingerprint": "synthetic", "source_provenance": "none"}],
            "model_route_capabilities": {"model": [{"route_id": "deepseek.primary",
                "capability_signature": ["text"], "status": "verified", "route_config_fingerprint": "synthetic"}]},
        })
        started = await service.start({"conversation_id": conversation["id"], "content": "Synthetic request",
            "idempotency_key": "synthetic-http", "provider_profile_id": "synthetic", "model_id": "model"})
        run_id = started["run"]["id"]
        await service.tasks[run_id]
        failed = repository.get_model_run(run_id)
        assert (failed["status"], failed["error_code"]) == ("failed", "provider_response_incomplete")
        with repository._connect() as connection:
            attempt = dict(connection.execute("SELECT * FROM run_attempts WHERE run_id=?", (run_id,)).fetchone())
            invocation = dict(connection.execute("SELECT * FROM invocation_specs WHERE id=?", (attempt["invocation_spec_id"],)).fetchone())
        assert attempt["output_tokens"] == 16384 and attempt["input_tokens"] == 2115
        assert attempt["error_code"] == "provider_response_incomplete" and attempt["finished_at"]
        assert invocation["status"] == "failed" and invocation["finished_at"]
        assert json.loads(invocation["generation_json"])["reasoning_effort"] == "none"
        assert len(requests) == 2
        assert not requests[-1].get("tools")
        assert "系统执行说明" in failed["draft"]["content"]
        assert requests[0]["thinking"] == {"type": "disabled"}
        assert requests[0]["max_tokens"] == 16384

    asyncio.run(scenario())


@pytest.mark.parametrize("finish,code", [("length", "provider_response_incomplete"), ("stop", "context_compaction_output_empty")])
def test_compaction_chat_termination_retains_late_usage_without_replay(tmp_path, monkeypatch, finish, code):
    from agent.context_engine.domain import ContextEngineError
    from agent.context_engine.service import ContextEngineService
    from test_context_compaction_lifecycle import seed

    requests = install_http(monkeypatch, [chunk({"reasoning_content": "PRIVATE_SYNTHETIC_REASONING"}, finish),
        {"choices": [], "usage": {"prompt_tokens": 30, "completion_tokens": 512}}])

    async def scenario():
        repository = CoreRepository(tmp_path / "compaction-http.db")
        run = seed(repository)
        service = ContextEngineService(repository)
        with pytest.raises((ProviderError, ContextEngineError)) as caught:
            await service.compact_once(run, OpenAICompatibleProvider(profile()), context_window=4096, max_output_tokens=512)
        assert caught.value.code == code
        status = service.context_repository.compaction_status(run["conversation_id"])
        assert (status["input_tokens"], status["output_tokens"]) == (30, 512)
        assert status["status"] == "failed"
        assert len(requests) == 1

    asyncio.run(scenario())
