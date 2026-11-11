"""A failed execution still delivers a model explanation or a labelled system result."""
import asyncio
import json

import pytest

from test_tool_continuation_retry import fixture, install_http, list_reply, start


PROGRESS = "已完成目录检查。"
EXPLANATION = "工具执行无法继续，目录检查已完成，文件尚未创建。"


def saved_progress(repository, run_id):
    return "\n".join(json.loads(item["content"])["content"]
                     for item in repository.list_assistant_progress(run_id))


@pytest.mark.parametrize("limit", ["MAX_ROUNDS", "MAX_READ_OPERATIONS", "MAX_FILE_MUTATIONS"])
def test_limit_returns_model_explanation_without_replaying_tools(tmp_path, monkeypatch, limit):
    blocked = list_reply("blocked")
    if limit == "MAX_FILE_MUTATIONS":
        blocked = {"tool_calls": [{"index": 0, "id": "blocked", "type": "function", "function": {
            "name": "fs_propose_changes", "arguments": json.dumps({"title": "Create", "mutations": [
                {"kind": "create_text", "path": "file.txt", "content": "not executed"}]})}}]}
    blocked["content"] = PROGRESS
    requests = install_http(monkeypatch, [(list_reply("done"), "tool_calls"), (blocked, "tool_calls"),
        ({"content": EXPLANATION}, "stop")])

    async def scenario():
        events, effects = [], []
        async def sink(event):
            events.append(event)
            if event["event_type"] == "run.tool_requested":
                operation = event["data"]["operation"]
                effects.append(operation["provider_call_id"])
                service.tool_status(operation["id"], "executing")
                service.resolve_tool(operation["id"], True, '{"entries":[{"name":"sentinel.txt"}]}')
        repository, _, work, service = await fixture(tmp_path, sink)
        monkeypatch.setattr(service.tool_loop, limit, 0 if limit == "MAX_FILE_MUTATIONS" else 1)
        run_id = await start(service, work)
        run = repository.get_model_run(run_id)
        assert run["status"] == "completed", (run["error_code"], run["error_message"])
        assert PROGRESS not in run["assistant_message"]["content"]
        assert PROGRESS in saved_progress(repository, run_id)
        assert EXPLANATION in run["assistant_message"]["content"]
        assert "执行说明（系统）" in run["assistant_message"]["content"]
        assert effects == ["done"] and len(requests) == 3
        final_payload = requests[-1][1]
        assert not final_payload.get("tools")
        assert "sentinel.txt" in json.dumps(final_payload["messages"])
        assert not any(message.get("tool_calls") or message["role"] == "tool"
                       for message in final_payload["messages"])
        assert any("tool_" in message.get("content", "") and "失败" in message.get("content", "")
                   for message in final_payload["messages"] if isinstance(message.get("content"), str))
        assert not run["draft"] and not service.tool_loop.waiters
        assert any(event["event_type"] == "run.response_recovering" for event in events)
        with repository._connect() as connection:
            specs = [dict(row) for row in connection.execute(
                "SELECT round_no, tools_json, generation_json FROM invocation_specs WHERE plan_id=? ORDER BY round_no",
                (run["execution_plan_id"],))]
        assert [spec["round_no"] for spec in specs] == [1, 2, 3]
    asyncio.run(scenario())


def test_truncated_response_gets_one_text_only_closing_response(tmp_path, monkeypatch):
    requests = install_http(monkeypatch, [({"content": PROGRESS}, "length"),
        ({"content": "本次输出被截断；以上是已完成部分，后续尚未执行。"}, "stop")])
    async def scenario():
        repository, _, work, service = await fixture(tmp_path, lambda event: asyncio.sleep(0))
        run_id = await start(service, work)
        run = repository.get_model_run(run_id)
        assert run["status"] == "completed"
        assert PROGRESS not in run["assistant_message"]["content"]
        assert PROGRESS in saved_progress(repository, run_id)
        assert "provider_response_incomplete" in run["assistant_message"]["content"]
        assert len(requests) == 2 and not requests[-1][1].get("tools")
    asyncio.run(scenario())


def test_internal_tool_error_then_failed_finalization_preserves_both_partial_results(tmp_path, monkeypatch):
    requests = install_http(monkeypatch, [({**list_reply("broken"), "content": PROGRESS}, "tool_calls"),
        ({"content": "收尾回复的一部分。"}, "length")])
    async def scenario():
        events = []
        async def sink(event):
            events.append(event)
            if event["event_type"] == "run.tool_requested":
                raise RuntimeError("SECRET-INTERNAL-EXCEPTION")
        repository, _, work, service = await fixture(tmp_path, sink)
        run_id = await start(service, work)
        run = repository.get_model_run(run_id)
        assert run["status"] == "failed"
        content = (run.get("draft") or {}).get("content", "")
        assert PROGRESS in content and "收尾回复的一部分。" in content
        assert "系统执行说明" in content and "internal_error" in content
        assert "provider_response_incomplete" in content
        assert "SECRET-INTERNAL-EXCEPTION" not in content + json.dumps(requests)
        assert len(requests) == 2 and not service.tool_loop.waiters
        assert not any(op["status"] in {"requested", "validating", "executing"}
                       for op in service.list_tool_operations(run_id))
        failure = next(event for event in events if event["event_type"] == "run.failed")
        assert failure["data"]["fallback_content"] == content
    asyncio.run(scenario())


def test_pre_dispatch_failure_still_has_a_labelled_result_without_model_call(tmp_path, monkeypatch):
    requests = install_http(monkeypatch, [])
    async def scenario():
        repository, _, work, service = await fixture(tmp_path, lambda event: asyncio.sleep(0))
        def broken(*args, **kwargs):
            raise RuntimeError("SECRET-PRE-DISPATCH")
        monkeypatch.setattr(service.execution_coordinator, "get_for_run", broken)
        run_id = await start(service, work)
        run = repository.get_model_run(run_id)
        assert run["status"] == "failed"
        assert "系统执行说明" in run["draft"]["content"]
        assert "internal_error" in run["draft"]["content"]
        assert "SECRET" not in run["draft"]["content"]
        assert not requests
    asyncio.run(scenario())


def test_long_model_answer_is_saved_without_the_user_message_character_cap(tmp_path, monkeypatch):
    answer = "内容" * 12000
    requests = install_http(monkeypatch, [({"content": answer}, "stop")])
    async def scenario():
        repository, _, work, service = await fixture(tmp_path, lambda event: asyncio.sleep(0))
        run_id = await start(service, work)
        run = repository.get_model_run(run_id)
        assert run["status"] == "completed", (run["error_code"], run["error_message"])
        assert run["assistant_message"]["content"] == answer
        assert len(requests) == 1
    asyncio.run(scenario())


def test_post_completion_bookkeeping_failure_keeps_the_answer_completed(tmp_path, monkeypatch):
    install_http(monkeypatch, [({"content": "已正常返回的回答。"}, "stop")])
    async def scenario():
        events = []
        async def sink(event): events.append(event)
        repository, _, work, service = await fixture(tmp_path, sink)
        def broken(*args, **kwargs): raise RuntimeError("bookkeeping")
        monkeypatch.setattr(service.context, "post_run_compaction_decision", broken)
        run_id = await start(service, work)
        run = repository.get_model_run(run_id)
        assert run["status"] == "completed" and run["assistant_message"]["content"] == "已正常返回的回答。"
        assert not any(event["event_type"] == "run.failed" for event in events)
        assert len([event for event in events if event["event_type"] == "run.completed"]) == 1
    asyncio.run(scenario())


def test_valid_multi_call_batch_keeps_the_first_effect_when_second_hits_limit(tmp_path, monkeypatch):
    first = list_reply("first")["tool_calls"][0]
    second = dict(list_reply("blocked")["tool_calls"][0], index=1)
    requests = install_http(monkeypatch, [({"tool_calls": [first, second]}, "tool_calls"),
        ({"content": "First inspection finished; the second inspection could not run."}, "stop")])
    async def scenario():
        async def sink(event):
            if event["event_type"] == "run.tool_requested":
                service.resolve_tool(event["data"]["operation"]["id"], True, '{"entries":[{"name":"first-result"}]}')
        repository, _, work, service = await fixture(tmp_path, sink)
        monkeypatch.setattr(service.tool_loop, "MAX_ROUNDS", 1)
        run_id = await start(service, work)
        assert repository.get_model_run(run_id)["status"] == "completed"
        assert [op["provider_call_id"] for op in service.list_tool_operations(run_id)] == ["first"]
        assert len(requests) == 2 and "first-result" in json.dumps(requests[-1])
    asyncio.run(scenario())


def test_revocation_during_closing_prevents_another_dispatch(tmp_path, monkeypatch):
    from test_tool_continuation_retry import configure, configuration
    requests = install_http(monkeypatch, [({"content": PROGRESS}, "length")])
    async def scenario():
        async def sink(event):
            if event["event_type"] == "run.response_recovering":
                value = configuration()
                value["enabled"] = False
                await configure(service, value)
        repository, _, work, service = await fixture(tmp_path, sink)
        run_id = await start(service, work)
        run = repository.get_model_run(run_id)
        assert run["status"] == "failed" and len(requests) == 1
        assert PROGRESS in run["draft"]["content"]
        assert "execution_plan_stale" in run["draft"]["content"]
    asyncio.run(scenario())


def test_cancellation_during_closing_never_dispatches_again(tmp_path, monkeypatch):
    requests = install_http(monkeypatch, [({"content": PROGRESS}, "length")])
    async def scenario():
        async def sink(event):
            if event["event_type"] == "run.response_recovering":
                asyncio.current_task().cancel()
        repository, _, work, service = await fixture(tmp_path, sink)
        response = await service.start(dict(conversation_id=work["id"], content="Inspect the project directory.",
            idempotency_key="cancel-test", selection=dict(mode="fixed", provider_profile_id="synthetic",
            model_id="model", routing_policy={"preset":"balanced"})))
        with pytest.raises(asyncio.CancelledError):
            await service.tasks[response["run"]["id"]]
        run = repository.get_model_run(response["run"]["id"])
        assert run["status"] == "cancelled" and len(requests) == 1
        assert PROGRESS in run["draft"]["content"]
    asyncio.run(scenario())


def test_closing_invocation_disables_even_planned_native_search(tmp_path, monkeypatch):
    install_http(monkeypatch, [({"content": "A normal answer."}, "stop")])
    async def scenario():
        repository, _, work, service = await fixture(tmp_path, lambda event: asyncio.sleep(0))
        run_id = await start(service, work)
        run = repository.get_model_run(run_id)
        plan = service.execution_repository.get_for_run(run_id)
        step = next(step for step in plan["steps"] if step["step_type"] == "final_response")
        step = dict(step, capability_signature=["text", "function_tools", "native_web_search"])
        spec = service._create_model_invocation_spec(run, plan, step, round_no=2, attempt_no=1,
            allowed_tools=(), max_output_tokens=1024, response_only=True)
        assert spec.native_search_mode == "off"
        assert spec.capability_signature == ("text",)
        assert not service.invocation_compiler.compile_model(spec, [{"role":"user", "content":"Explain"}]).tools_enabled
    asyncio.run(scenario())


def test_post_commit_step_failure_emits_completed_once(tmp_path, monkeypatch):
    install_http(monkeypatch, [({"content": "The answer committed before optional tracking."}, "stop")])
    async def scenario():
        events = []
        async def sink(event): events.append(event)
        repository, _, work, service = await fixture(tmp_path, sink)
        original = service.execution_repository.transition_step
        def fail_completion(step_id, expected, target, *args, **kwargs):
            if target == "completed": raise RuntimeError("tracking failed")
            return original(step_id, expected, target, *args, **kwargs)
        monkeypatch.setattr(service.execution_repository, "transition_step", fail_completion)
        run_id = await start(service, work)
        run = repository.get_model_run(run_id)
        assert run["status"] == "completed" and run["assistant_message"]
        assert len([event for event in events if event["event_type"] == "run.completed"]) == 1
        assert not any(event["event_type"] == "run.failed" for event in events)
    asyncio.run(scenario())


def test_unexpected_stream_exception_preserves_the_uncheckpointed_tail(tmp_path, monkeypatch):
    import httpx
    original = httpx.AsyncClient
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        texts = ["first", "second", "third"] if len(requests) == 1 else ["Closing explanation."]
        chunks = [{"choices":[{"delta":{"content":text}, "finish_reason":None}]} for text in texts]
        chunks.append({"choices":[{"delta":{}, "finish_reason":"stop"}]})
        return httpx.Response(200, text="".join("data: "+json.dumps(chunk)+"\n\n" for chunk in chunks)+"data: [DONE]\n\n")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    async def scenario():
        failed = False
        async def sink(event):
            nonlocal failed
            if event["event_type"] == "run.chunk" and event["data"]["delta"] == "third" and not failed:
                failed = True
                raise RuntimeError("SECRET-STREAM-EXCEPTION")
        repository, _, work, service = await fixture(tmp_path, sink)
        run_id = await start(service, work)
        run = repository.get_model_run(run_id)
        assert run["status"] == "completed"
        assert "firstsecondthird" not in run["assistant_message"]["content"]
        assert "firstsecondthird" in saved_progress(repository, run_id)
        assert "Closing explanation." in run["assistant_message"]["content"]
        plan = service.execution_repository.get_for_run(run_id)
        assert not any(item["status"] == "dispatched" for item in plan["invocations"])
        assert "SECRET" not in run["assistant_message"]["content"]
    asyncio.run(scenario())


def test_save_failure_keeps_full_returned_answer_instead_of_older_checkpoint(tmp_path, monkeypatch):
    import sqlite3
    requests = install_http(monkeypatch, [({"content": "Full returned answer with its final tail."}, "stop")])
    async def scenario():
        async def sink(event):
            if event["event_type"] == "run.draft_checkpoint":
                run = repository.get_model_run(event["run_id"])
                repository.checkpoint_draft(event["run_id"], run["attempt"]["id"], "Older checkpoint.")
        repository, _, work, service = await fixture(tmp_path, sink)
        def fail_save(*args, **kwargs): raise sqlite3.OperationalError("synthetic save failure")
        monkeypatch.setattr(repository, "complete_model_run", fail_save)
        run_id = await start(service, work)
        run = repository.get_model_run(run_id)
        assert run["status"] == "failed"
        assert "Full returned answer with its final tail." in run["draft"]["content"]
        assert "persistence_failed" in run["draft"]["content"]
        assert len(requests) == 1
    asyncio.run(scenario())


def test_context_limit_uses_existing_strict_retry_before_text_only_closing(tmp_path, monkeypatch):
    from agent.providers import OpenAICompatibleProvider, ProviderError
    requests = install_http(monkeypatch, [({"content": "Normal work resumed after context repair."}, "stop")])
    original_stream = OpenAICompatibleProvider.stream_events
    first = True
    async def stream(provider, *args, **kwargs):
        nonlocal first
        if first:
            first = False
            raise ProviderError("provider_context_limit_exceeded", "Synthetic context capacity failure")
        async for event in original_stream(provider, *args, **kwargs): yield event
    monkeypatch.setattr(OpenAICompatibleProvider, "stream_events", stream)
    async def scenario():
        events, strict_calls = [], []
        async def sink(event): events.append(event)
        repository, _, work, service = await fixture(tmp_path, sink)
        original_prepare = service._prepare_context
        async def prepare(*args, **kwargs):
            strict_calls.append(kwargs.get("strict", False))
            return await original_prepare(*args, **kwargs)
        monkeypatch.setattr(service, "_prepare_context", prepare)
        run_id = await start(service, work)
        run = repository.get_model_run(run_id)
        assert run["status"] == "completed"
        assert strict_calls == [False, True]
        assert len(requests) == 1 and requests[0][1].get("tools")
        assert not any(event["event_type"] == "run.response_recovering" for event in events)
    asyncio.run(scenario())


def test_compiler_capacity_failure_reassembles_with_measured_overhead(tmp_path, monkeypatch):
    from agent.context_engine.policy import estimate_tokens
    from agent.storage import CoreDataError
    requests = install_http(monkeypatch, [({'content': 'Completed after input rebudgeting.'}, 'stop')])
    async def scenario():
        async def sink(event):
            pass
        repository, _, work, service = await fixture(tmp_path, sink)
        original_compile = service.invocation_compiler.compile_model
        original_prepare = service._prepare_context
        overheads = []
        first = True
        def compile_model(spec, messages, **kwargs):
            nonlocal first
            if first and spec.endpoint_role == 'final_response':
                first = False
                raise CoreDataError('provider_context_limit_exceeded', 'Measured envelope overflow',
                                    {'input_overhead_tokens': 700, 'request_tokens': estimate_tokens(messages) + 700})
            return original_compile(spec, messages, **kwargs)
        async def prepare(*args, **kwargs):
            overheads.append(kwargs.get('input_overhead_tokens', 0))
            return await original_prepare(*args, **kwargs)
        monkeypatch.setattr(service.invocation_compiler, 'compile_model', compile_model)
        monkeypatch.setattr(service, '_prepare_context', prepare)
        run_id = await start(service, work)
        assert repository.get_model_run(run_id)['status'] == 'completed'
        assert overheads[0] == 0 and overheads[1] > 764
        assert len(requests) == 1
    asyncio.run(scenario())


def test_tool_round_snapshot_uses_same_chinese_token_estimator(tmp_path, monkeypatch):
    from agent.context_engine.policy import estimate_tokens
    install_http(monkeypatch, [({'content': 'Completed.'}, 'stop')])
    async def scenario():
        async def sink(event):
            pass
        _, _, work, service = await fixture(tmp_path, sink)
        run_id = await start(service, work)
        messages = [{'role': 'user', 'content': '振动试验' * 100}]
        context = service.context.context_repository
        context.save_tool_round_snapshot(run_id, context.next_snapshot_round(run_id), messages)
        assert context.preview(run_id)['estimated_tokens'] == estimate_tokens(messages)
    asyncio.run(scenario())
