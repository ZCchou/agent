import asyncio
import json

import httpx
import pytest

from agent.rag.grounding import RagGrounding
from agent.rag.service import RagService
from test_rag_service import Engines, ready, wait_job
from test_tool_continuation_retry import configuration, fixture


@pytest.mark.parametrize("mode", ["text_only", "native", "structured"])
@pytest.mark.parametrize("corrupt_quote", [False, True])
def test_project_chat_retrieves_before_model_and_validates_answer(tmp_path, monkeypatch, mode, corrupt_quote):
    requests = []
    transports = []
    history_only = False
    original = httpx.AsyncClient

    def respond(request):
        payload = json.loads(request.content)
        if history_only:
            chunk = {"choices": [{"delta": {"content": "继续核查历史资料。"}, "finish_reason": "stop"}]}
            return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")
        requests.append(payload)
        records = []
        for message in payload["messages"]:
            try:
                value = json.loads(message.get("content", ""))
            except (ValueError, TypeError):
                continue
            if isinstance(value.get("evidence"), dict):
                records.append(value["evidence"])
            elif isinstance(value.get("evidence"), list):
                records.extend(value["evidence"])
        assert records, "RAG evidence must precede every generation"
        assert any("适航资料问答输出约定" in message.get("content", "") for message in payload["messages"])
        assert "fs_list" not in json.dumps(payload.get("tools", []))
        if mode == "structured":
            assert "Serialize the claims JSON" in payload["messages"][0]["content"]
        if mode != "text_only" and len(requests) == 1:
            delta = {"tool_calls": [{"index": 0, "id": "retrieve", "type": "function", "function": {
                "name": "rag_search", "arguments": json.dumps({"query": "25.1309 失效"})}}]}
            finish = "tool_calls"
            if mode == "structured":
                delta = {"content": json.dumps({"type": "tool_call", "id": "retrieve", "name": "rag.search", "arguments": {"query": "25.1309 失效"}})}
                finish = "stop"
        else:
            source = records[-1]
            delta = {"content": json.dumps({"claims": [{"text": "需要核查失效状态安全性要求。", "evidence": [
                {"id": source["id"], "quote": "伪造原文" if corrupt_quote else source["text"]}]}]}, ensure_ascii=False)}
            finish = "stop"
            if mode == "structured":
                delta["content"] = json.dumps({"type": "text", "content": delta["content"]})
        chunk = {"choices": [{"delta": delta, "finish_reason": finish}]}
        return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")

    def client(**kwargs):
        transports.append(kwargs.get("trust_env", True))
        return original(transport=httpx.MockTransport(respond), **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", client)

    async def exercise():
        nonlocal history_only
        events = []
        async def sink(event):
            events.append(event)
        config = configuration()
        config["base_url"] = "http://127.0.0.1:12345/v1"
        config["model_tool_modes"]["model"] = mode
        core, project, conversation, service = await fixture(tmp_path, sink, config)
        await service.rag.close()
        service.rag = RagService(core, Engines())
        service.rag_grounding = RagGrounding(service.rag)
        service.context.rag_context = service.rag_grounding.evidence
        library = (await service.rag.handle("library.create", {"name": "法规", "embedding_model_id": "embed", "reranker_model_id": "rank"}))["library"]["id"]
        await ready(service.rag, library)
        assert (await wait_job(service.rag, await service.rag.handle("index.build", {"library_id": library})))["status"] == "completed"
        await service.rag.handle("library.bind", {"library_id": library, "project_id": project["id"], "enabled": True})
        response = await service.start({"conversation_id": conversation["id"], "content": "25.1309 对失效有何要求",
            "idempotency_key": "rag-chat", "web_search_global_enabled": True})
        run_id = response["run"]["id"]
        await service.tasks[run_id]
        run = core.get_model_run(run_id)
        if corrupt_quote:
            assert run["status"] == "failed"
            assert run["error_code"] == "rag_quote_invalid"
            assert "伪造原文" not in str(run.get("draft"))
            assert "claims" not in str(run.get("draft"))
        else:
            assert run["status"] == "completed", run.get("error_message")
            assert "rag-evidence:rag-" in run["assistant_message"]["content"]
            assert "草案" in run["assistant_message"]["content"]
        assert all("claims" not in event["data"].get("delta", "") for event in events if event["event_type"] == "run.chunk")
        assert len(requests) == (1 if mode == "text_only" else 2)
        if not corrupt_quote and mode == "text_only":
            await service.rag.handle("library.bind", {"library_id": library, "project_id": project["id"], "enabled": False})
            history_only = True
            transports.clear()
            followup = await service.start({"conversation_id": conversation["id"], "content": "继续说明",
                "idempotency_key": "rag-history"})
            await service.tasks[followup["run"]["id"]]
            assert transports and not any(transports), "RAG history must keep environment proxies disabled after unbinding"
        await service.shutdown()
    asyncio.run(exercise())
