import asyncio
import json

import httpx
import pytest

from agent.rag.grounding import RagGrounding
from agent.rag.service import RagService
from agent.storage import CoreDataError
from test_rag_service import Engines, ready, wait_job


def test_automatic_greeting_skips_all_retrieval_and_force_mode_checks_project(tmp_path, monkeypatch):
    original = httpx.AsyncClient
    payloads = []
    def respond(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        chunk = {"choices": [{"delta": {"content": "你好！"}, "finish_reason": "stop"}]}
        return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    async def exercise():
        async def sink(event): pass
        config = configuration()
        config['base_url'] = 'http://127.0.0.1:12345/v1'
        config['model_tool_modes']['model'] = 'text_only'
        core, project, conversation, service = await fixture(tmp_path, sink, config)
        try:
            core._insert_message(conversation['id'], 'user', '此前文件任务')
            core._insert_message(conversation['id'], 'assistant', '[Open folder](project-dir:.)')
            async def forbidden(*args, **kwargs):
                raise AssertionError('Greeting must skip retrieval, embedding and reranking')
            service.rag.search = forbidden
            response = await service.start({'conversation_id': conversation['id'], 'content': '你好', 'idempotency_key': 'greeting'})
            await service.tasks[response['run']['id']]
            run = core.get_model_run(response['run']['id'])
            assert run['status'] == 'completed', run.get('error_message')
            assert run['rag_decision']['reason'] == 'greeting'
            assert payloads and not any(payload.get('response_format') for payload in payloads)
            final_messages = payloads[-1]['messages']
            assert not any('project-dir:' in str(message.get('content', '')) for message in final_messages)
            assert not any(message['role'] == 'assistant' for message in final_messages)
            assert not payloads[-1].get('tools')
            before = len(core.get_conversation(conversation['id'])['messages'])
            with pytest.raises(CoreDataError) as error:
                await service.start({'conversation_id': conversation['id'], 'content': '你好', 'idempotency_key': 'forced', 'rag_mode': 'required'})
            assert error.value.code == 'rag_library_required'
            assert len(core.get_conversation(conversation['id'])['messages']) == before
        finally:
            await service.shutdown()
    asyncio.run(exercise())
from test_tool_continuation_retry import configuration, fixture as base_fixture


async def fixture(*args, **kwargs):
    core, project, conversation, service = await base_fixture(*args, **kwargs)
    # These tests exercise retrieval and citation generation. Semantic routing
    # has its own integration coverage and is supplied at its service boundary.
    async def advisor(**request):
        from agent.rag.routing import decide_evidence
        decision = decide_evidence(request['content'])
        return {'rag_requirement': 'required' if decision['required'] else 'not_needed',
                'files': 'none', 'tools': 'available', 'web': 'not_needed',
                'web_evidence': 'optional', 'reasoning': 'standard',
                'task_kinds': ['document_export'] if decision['task_type'] == 'document_export' else ['general'],
                'explanation_code': 'citation_test'}, None
    service._preflight_intent_advisor = advisor
    return core, project, conversation, service


@pytest.mark.parametrize("mode", ["text_only", "native", "structured"])
@pytest.mark.parametrize("corrupt_quote", [False, True])
@pytest.mark.parametrize("repair_first", [False, True])
@pytest.mark.parametrize("select_quote", [False, True])
def test_project_chat_retrieves_before_model_and_validates_answer(tmp_path, monkeypatch, mode, corrupt_quote, repair_first, select_quote):
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
        if mode == 'text_only':
            assert all('Use the current runtime capability observation when planning requested work and continuations.'
                       not in message.get('content', '') for message in payload['messages'])
        if mode == 'native' and 'tools' in payload:
            assert 'fs_list' in json.dumps(payload['tools'])
        constrained = payload.get("response_format", {}).get("type") == "json_schema"
        if mode == "structured" and not constrained:
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
            original_text = source.get('text') or ''.join(option['text'] for option in source['quote_options'])
            citation = {"id": source["id"], "quote": "伪造原文" if corrupt_quote else original_text}
            if select_quote and not corrupt_quote:
                citation = {"id": source["id"], "quote_id": source["quote_options"][0]["quote_id"]}
            delta = {"content": json.dumps({"claims": [{"text": "需要核查失效状态安全性要求。", "evidence": [
                citation]}]}, ensure_ascii=False)}
            finish = "stop"
            if mode == "structured" and not constrained:
                delta["content"] = json.dumps({"type": "text", "content": delta["content"]})
        if repair_first and not corrupt_quote and not any("invalid_answer" in message.get("content", "") for message in payload["messages"]) and (mode == "text_only" or constrained):
            delta = {"content": "普通文字，未生成 JSON。"}
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
        config["model_capabilities"]["model"]["max_output_tokens"] = 512
        core, project, conversation, service = await fixture(tmp_path, sink, config)
        await service.rag.close()
        service.rag = RagService(core, Engines())
        service.rag_grounding = RagGrounding(service.rag)
        service.context.rag_context = service.rag_grounding.evidence
        library = (await service.rag.handle("library.create", {"name": "法规", "embedding_model_id": "embed", "reranker_model_id": "rank"}))["library"]["id"]
        await ready(service.rag, library)
        assert (await wait_job(service.rag, await service.rag.handle("index.build", {"library_id": library})))["status"] == "completed"
        await service.rag.handle("library.bind", {"library_id": library, "project_id": project["id"], "enabled": True})
        response = await service.start({"conversation_id": conversation["id"], "content": "请依据当前项目资料库，引用原文说明25.1309 对失效有何要求，资料版本未标注请明确说明",
            "idempotency_key": "rag-chat", "web_search_global_enabled": True, "web_search_mode": "off"})
        run_id = response["run"]["id"]
        plan = service.execution_repository.get_for_run(run_id)
        assert not any(item['capability_id'] in {'web_search', 'structured_web_evidence'}
                       for item in plan.get('requirements', {}).get('capability_advisories', []))
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
        assert len(requests) == (1 if mode == "text_only" else 3) + int(corrupt_quote or repair_first)
        assert requests[-1]["response_format"]["type"] == "json_schema"
        assert "tools" not in requests[-1]
        assert requests[-1]["max_tokens"] <= 512
        if corrupt_quote or repair_first:
            repair = next(json.loads(message['content']) for message in requests[-1]['messages']
                          if 'invalid_answer' in message.get('content', ''))
            assert 'validation_details' in repair
            assert repair['allowed_quotes']
            with core._connect() as connection:
                diagnostics = connection.execute("SELECT a.content_json FROM execution_artifacts a JOIN execution_steps s ON s.id=a.step_id JOIN execution_plans p ON p.id=s.plan_id WHERE p.run_id=? AND a.artifact_type='rag_validation'", (run_id,)).fetchall()
            assert diagnostics
            diagnostic = json.loads(diagnostics[-1][0])
            assert len(diagnostic['answer_hash']) == 64
            assert diagnostic['error_code'] in {'rag_quote_invalid', 'rag_answer_invalid'}
            assert '伪造原文' not in diagnostics[-1][0]
            with core._connect() as connection:
                assert connection.execute("SELECT COUNT(*) FROM execution_steps s JOIN execution_artifacts a ON a.id=s.artifact_id JOIN execution_plans p ON p.id=s.plan_id WHERE p.run_id=? AND a.artifact_type='rag_validation'", (run_id,)).fetchone()[0] == 0
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


def test_schema_unsupported_falls_back_without_repeating_retrieval(tmp_path, monkeypatch):
    original = httpx.AsyncClient
    formats = []
    def respond(request):
        payload = json.loads(request.content)
        formats.append(payload.get("response_format", {}).get("type"))
        if len(formats) == 1:
            return httpx.Response(400, text='response_format json_schema is unsupported')
        assert formats[-1] == "json_object"
        records = []
        for message in payload["messages"]:
            try: value = json.loads(message.get("content", ""))
            except (ValueError, TypeError): continue
            if isinstance(value, dict) and isinstance(value.get("evidence"), dict): records.append(value["evidence"])
        source = records[-1]
        answer = json.dumps({"claims":[{"text":"核查失效要求", "evidence":[{"id":source["id"], "quote_id":source['quote_options'][0]['quote_id']}]}]})
        chunk = {"choices":[{"delta":{"content":answer},"finish_reason":"stop"}]}
        return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    async def exercise():
        async def sink(event): pass
        config = configuration()
        config["base_url"] = "http://127.0.0.1:12345/v1"
        config["model_tool_modes"]["model"] = "text_only"
        core, project, conversation, service = await fixture(tmp_path, sink, config)
        await service.rag.close()
        service.rag = RagService(core, Engines())
        service.rag_grounding = RagGrounding(service.rag)
        service.context.rag_context = service.rag_grounding.evidence
        try:
            library = (await service.rag.handle("library.create", {"name":"Rules","embedding_model_id":"embed","reranker_model_id":"rank"}))["library"]["id"]
            await service.rag.handle("library.bind", {"library_id":library,"project_id":project["id"],"enabled":True})
            await ready(service.rag, library)
            assert (await wait_job(service.rag, await service.rag.handle("index.build", {"library_id":library})))["status"] == "completed"
            response=await service.start({"conversation_id":conversation["id"], "content":"失效要求", "idempotency_key":"schema-fallback"})
            await service.tasks[response["run"]["id"]]
            run=core.get_model_run(response["run"]["id"])
            assert run["status"] == "completed", run.get("error_message")
            with core._connect() as connection:
                assert connection.execute("SELECT COUNT(*) FROM rag_searches WHERE run_id=?", (run["id"],)).fetchone()[0] == 1
            assert formats == ["json_schema", "json_object"]
        finally:
            await service.shutdown()
    asyncio.run(exercise())
