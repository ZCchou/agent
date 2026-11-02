"""Public tool narration is retained separately from the delivered final answer."""
import asyncio
import json
import sqlite3

from agent.context_engine.history import reconstruct_turns
from agent.storage import CoreRepository
from agent.providers import ProviderEvent
from agent.tool_contracts import ToolCall
from test_tool_continuation_retry import fixture, install_http, list_reply, start


def progress_messages(repository, work):
    return [message for message in repository.get_conversation(work["id"])["messages"]
            if message.get("event_type") == "assistant_progress"]


def test_tool_round_narration_is_not_the_final_answer_and_survives_reload(tmp_path, monkeypatch):
    first = dict(list_reply("read-one"), content="先读取 main.js。", reasoning_content="PRIVATE-REASONING")
    second = dict(list_reply("read-two"), content="正在检查数据集生成逻辑。")
    requests = install_http(monkeypatch, [(first, "tool_calls"), (second, "tool_calls"),
        ({"content": "Datasets added."}, "stop")])

    async def scenario():
        events = []
        async def sink(event):
            events.append(event)
            if event["event_type"] == "run.tool_requested":
                operation = event["data"]["operation"]
                service.tool_status(operation["id"], "executing")
                service.resolve_tool(operation["id"], True, '{"entries":[]}')
        repository, _, work, service = await fixture(tmp_path, sink)
        run_id = await start(service, work)
        run = repository.get_model_run(run_id)
        assert run["status"] == "completed", (run["error_code"], run["error_message"])
        assert run["assistant_message"]["content"] == "Datasets added."
        saved = progress_messages(CoreRepository(tmp_path / "synthetic.db"), work)
        assert [json.loads(item["content"])["content"] for item in saved] == [
            "先读取 main.js。", "正在检查数据集生成逻辑。"]
        assert all(json.loads(item["content"])["run_id"] == run_id for item in saved)
        assert len([e for e in events if e["event_type"] == "run.progress_recorded"]) == 2
        completed = next(e for e in events if e["event_type"] == "run.completed")
        assert [item["id"] for item in completed["data"]["progress_messages"]] == [item["id"] for item in saved]
        history = repository.get_conversation(work["id"])["messages"]
        assert "PRIVATE-REASONING" not in json.dumps(history, ensure_ascii=False)
        assert "PRIVATE-REASONING" not in json.dumps(events, ensure_ascii=False)
        turns = reconstruct_turns(history, [run])
        assert [m["role"] for turn in turns for m in turn.messages] == ["user", "assistant"]
        assert "先读取" not in json.dumps([turn.messages for turn in turns], ensure_ascii=False)
        assert len(requests) == 3
    asyncio.run(scenario())


def test_failed_partial_is_preserved_as_progress_and_recovery_delivers_a_clean_answer(tmp_path, monkeypatch):
    requests = install_http(monkeypatch, [({"content": "I will inspect the source next."}, "length"),
        ({"content": "The task is unfinished because the response was interrupted."}, "stop")])
    async def scenario():
        async def sink(_event):
            pass
        repository, _, work, service = await fixture(tmp_path, sink)
        run_id = await start(service, work)
        run = repository.get_model_run(run_id)
        assert run["status"] == "completed"
        assert run["assistant_message"]["content"].startswith("The task is unfinished")
        assert "I will inspect" not in run["assistant_message"]["content"]
        assert "provider_response_incomplete" in run["assistant_message"]["content"]
        assert any("I will inspect" in json.loads(item["content"])["content"] for item in progress_messages(repository, work))
        assert not requests[-1][1].get("tools") and len(requests) == 2
    asyncio.run(scenario())


def test_direct_answer_needs_no_extra_model_request_or_progress_record(tmp_path, monkeypatch):
    requests = install_http(monkeypatch, [({"content": "A direct answer."}, "stop")])
    async def scenario():
        async def sink(_event):
            pass
        repository, _, work, service = await fixture(tmp_path, sink)
        run_id = await start(service, work)
        assert repository.get_model_run(run_id)["assistant_message"]["content"] == "A direct answer."
        assert not progress_messages(repository, work)
        assert len(requests) == 1
    asyncio.run(scenario())


def test_optional_progress_lookup_cannot_suppress_committed_answer(tmp_path, monkeypatch):
    install_http(monkeypatch, [({"content": "A committed answer."}, "stop")])
    def unavailable(_self, _run_id):
        raise sqlite3.OperationalError("PRIVATE-DATABASE-DETAIL")
    monkeypatch.setattr(CoreRepository, "list_assistant_progress", unavailable)
    async def scenario():
        events = []
        async def sink(event):
            events.append(event)
        repository, _, work, service = await fixture(tmp_path, sink)
        run_id = await start(service, work)
        assert repository.get_model_run(run_id)["status"] == "completed"
        completed = [e for e in events if e["event_type"] == "run.completed"]
        assert len(completed) == 1
        assert completed[0]["data"]["message"]["content"] == "A committed answer."
        assert "PRIVATE-DATABASE-DETAIL" not in json.dumps(events)
    asyncio.run(scenario())


def test_empty_final_after_search_and_host_tool_uses_recovery(tmp_path, monkeypatch):
    class Provider:
        round = 0
        async def stream_events(self, _model, _messages, **_options):
            self.round += 1
            if self.round == 1:
                yield ProviderEvent("text_delta", text="Checking sources and project files.")
                yield ProviderEvent("web_search_result", web_search={"provider_call_id": "search-1", "sources": []})
                yield ProviderEvent("tool_call", tool_call=ToolCall("list-1", "fs.list", {"path": "."}))
            elif self.round == 3:
                yield ProviderEvent("text_delta", text="The files were listed, but the final response was empty.")
            yield ProviderEvent("finish", finish_reason="stop")
    provider = Provider()
    def create(profile):
        provider.profile = profile
        return provider
    monkeypatch.setattr("agent.runtime.create_provider", create)
    async def scenario():
        events = []
        async def sink(event):
            events.append(event)
            if event["event_type"] == "run.tool_requested":
                operation = event["data"]["operation"]
                service.tool_status(operation["id"], "executing")
                service.resolve_tool(operation["id"], True, '{"entries":[]}')
        repository, _, work, service = await fixture(tmp_path, sink)
        # Exercise a provider-managed search followed by a host tool in the
        # final-response loop, independently of specialist route selection.
        original_start = repository.start_model_run
        def native_run(**kwargs):
            return original_start(**dict(kwargs, web_search_route="native"))
        monkeypatch.setattr(repository, "start_model_run", native_run)
        repository.update_project_web_access(work["project_id"], "allow_search")
        run_id = await start(service, work)
        run = repository.get_model_run(run_id)
        assert run["status"] == "completed", (run["error_code"], run["error_message"], [(e["event_type"], e["data"].get("error_code")) for e in events])
        assert "The files were listed" in run["assistant_message"]["content"]
        assert "Checking sources" not in run["assistant_message"]["content"]
        assert any(e["event_type"] == "run.response_recovering" for e in events)
        assert provider.round == 3
        assert len(service.list_tool_operations(run_id)) == 1
    asyncio.run(scenario())
