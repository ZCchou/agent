import asyncio
import sqlite3
from pathlib import Path

import pytest
import httpx

from agent.context import ContextAssembler
from agent.context_engine.domain import ContextEngineError
from agent.providers import (
    ProviderError, ProviderEvent, ProviderProfile, _map_http_error, validate_profile,
)
from agent.runtime import ChatRunService
from agent.storage import CoreDataError, CoreRepository
from agent.tool_contracts import ToolCall


def test_v4_to_v5_preserves_existing_counts(tmp_path: Path) -> None:
    path = tmp_path / "v4.db"
    with sqlite3.connect(path) as connection:
        CoreRepository._create_schema_v4(connection)
        connection.execute("CREATE TABLE schema_info(version INTEGER NOT NULL)")
        connection.execute("INSERT INTO schema_info VALUES (4)")
        connection.execute(
            """INSERT INTO projects(id,name,sort_order,category,created_at,updated_at,deleted_at)
               VALUES ('p','Project',0,'coding','now','now',NULL)"""
        )
        connection.execute(
            """INSERT INTO conversations(id,kind,title,category,chat_group_id,project_id,sort_order,
                   created_at,updated_at,deleted_at) VALUES
                   ('c','project_work','Work',NULL,NULL,'p',0,'now','now',NULL)"""
        )
        connection.execute(
            """INSERT INTO messages(id,conversation_id,role,content,event_type,source_message_id,created_at)
               VALUES ('m','c','user','hello',NULL,NULL,'now')"""
        )
        expected = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in ("projects", "conversations", "messages")}
    CoreRepository(path)
    with sqlite3.connect(path) as connection:
        actual = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in ("projects", "conversations", "messages")}
    assert actual == expected


def test_run_start_is_idempotent_and_failure_draft_is_not_history(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = CoreRepository(tmp_path / "runs.db")
        conversation = repository.create_conversation("standard_chat", "Chat", "casual")
        events = []

        async def sink(event):
            events.append(event)

        service = ChatRunService(repository, sink)
        params = {"conversation_id": conversation["id"], "content": "hello",
                  "idempotency_key": "same", "provider_profile_id": "builtin-demo",
                  "model_id": "fault:disconnect"}
        first = await service.start(params)
        second = await service.start(params)
        assert first["run"]["id"] == second["run"]["id"]
        await service.tasks[first["run"]["id"]]
        run = repository.get_model_run(first["run"]["id"])
        assert run["status"] == "failed"
        assert run["draft"]["content"]
        history = repository.get_conversation(conversation["id"])["messages"]
        assert [message["role"] for message in history if message.get("event_type") != "assistant_progress"] == ["user"]
        assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))

    asyncio.run(scenario())


def test_persisted_fixed_conversation_ignores_stale_automatic_request(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repository = CoreRepository(tmp_path / "fixed-selection-authority.db")
        created = repository.create_conversation(
            "standard_chat", "Chat", "casual",
        )
        conversation = repository.update_conversation_routing(
            created["id"], "fixed", "balanced", "builtin-demo", "demo",
        )

        async def sink(_event):
            return None

        service = ChatRunService(repository, sink)
        started = await service.start({
            "conversation_id": conversation["id"],
            "content": "hello",
            "idempotency_key": "stale-automatic-selection",
            "provider_profile_id": "builtin-demo",
            "model_id": "demo",
            "selection": {
                "mode": "automatic",
                "provider_profile_id": "builtin-demo",
                "model_id": "demo",
                "routing_policy": {"preset": "balanced"},
            },
        })
        assert started["selection_decision"]["provider_profile_id"] == "builtin-demo"
        assert started["selection_decision"]["model_id"] == "demo"
        assert started["selection_decision"]["selection_source"] == "conversation_override"
        await service.tasks[started["run"]["id"]]
        assert repository.get_model_run(started["run"]["id"])["status"] == "completed"

    asyncio.run(scenario())


def test_new_project_work_uses_requested_automatic_selection(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repository = CoreRepository(tmp_path / "new-project-automatic.db")
        project = repository.create_project("Project", "coding")

        async def sink(_event):
            return None

        service = ChatRunService(repository, sink)
        started = await service.start({
            "kind": "project_work",
            "project_id": project["id"],
            "content": "hello",
            "idempotency_key": "new-project-automatic",
            "provider_profile_id": "builtin-demo",
            "model_id": "demo",
            "selection": {
                "mode": "automatic",
                "provider_profile_id": "builtin-demo",
                "model_id": "demo",
                "routing_policy": {"preset": "balanced"},
            },
        })
        assert started["selection_decision"]["selection_source"] == "router"
        assert started["conversation"]["model_selection_mode"] == "automatic"
        await service.tasks[started["run"]["id"]]
        assert repository.get_model_run(started["run"]["id"])["status"] == "completed"

    asyncio.run(scenario())


def test_project_static_question_with_auto_web_does_not_require_search_or_image_route(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repository = CoreRepository(tmp_path / "project-static-auto-web.db")
        project = repository.create_project("Project", "coding")

        async def sink(_event):
            return None

        service = ChatRunService(repository, sink)
        started = await service.start({
            "kind": "project_work",
            "project_id": project["id"],
            "content": "五条悟是谁",
            "idempotency_key": "project-static-auto-web",
            "provider_profile_id": "builtin-demo",
            "model_id": "demo",
            "web_search_request_mode": "auto",
            "web_search_global_enabled": True,
            "selection": {
                "mode": "automatic",
                "provider_profile_id": "builtin-demo",
                "model_id": "demo",
                "routing_policy": {"preset": "balanced"},
            },
        })
        assert started["web_search_decision"]["route"] == "none"
        assert started["execution_plan"]["requirements"] == {
            "text": True,
            "vision": "none",
                "web": "off",
                "web_evidence": "optional",
            "files": "available",
            "tools": "available",
            "reasoning": "standard",
            "minimum_context_tokens": started["execution_plan"]["requirements"][
                "minimum_context_tokens"
            ],
        }
        await service.tasks[started["run"]["id"]]
        completed = repository.get_model_run(started["run"]["id"])
        assert completed["status"] == "completed"
        assert repository.list_web_operations(started["run"]["id"]) == []

    asyncio.run(scenario())


def test_automatic_project_intent_advisor_promotes_ambiguous_file_work(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repository = CoreRepository(tmp_path / "project-intent-advisor.db")
        project = repository.create_project("Project", "coding")

        async def sink(_event):
            return None

        service = ChatRunService(repository, sink)
        started = await service.start({
            "kind": "project_work",
            "project_id": project["id"],
            "content": "处理项目里的空目录",
            "idempotency_key": "project-intent-advisor",
            "selection": {
                "mode": "automatic", "provider_profile_id": "builtin-demo", "model_id": "demo",
                "routing_policy": {"preset": "balanced"},
            },
        })
        plan = started["execution_plan"]
        # The user/rule request stays immutable; the semantic advisor is
        # recorded separately and contributes the effective plan.
        assert plan["requirements"]["files"] == "available"
        assert plan["resolved_intent"]["advisor"]["files"] == "write"
        assert plan["resolved_intent"]["advisor"]["tools"] == "required"
        assert plan["resolved_intent"]["effective_requirements"]["files"] == "write"
        assert plan["resolved_intent"]["effective_requirements"]["tools"] == "required"
        await service.tasks[started["run"]["id"]]

    asyncio.run(scenario())


def test_project_knowledge_is_published_before_sibling_can_use_it(tmp_path: Path) -> None:
    repository = CoreRepository(tmp_path / "knowledge.db")
    project = repository.create_project("Project", "research")
    first = repository.create_conversation("project_work", "First", project_id=project["id"])
    second = repository.create_conversation("project_work", "Second", project_id=project["id"])
    proposal = repository.propose_knowledge(first["id"], "decision", "Storage", "Use SQLite WAL mode")
    assert repository.list_project_knowledge(project["id"]) == []
    knowledge = repository.publish_knowledge(proposal["id"])
    repository.update_knowledge(knowledge["id"], pinned=True)
    run = repository.start_model_run(
        content="What storage mode?", idempotency_key="knowledge-run",
        provider_profile_id="builtin-demo", model_id="demo", conversation_id=second["id"],
    )
    context = ContextAssembler(repository).assemble(run)
    rendered = "\n".join(message["content"] for message in context.messages)
    assert "SQLite WAL mode" in rendered
    assert not any(message.get("conversation_id") == first["id"] for message in context.items)


def test_remote_http_provider_and_online_model_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(Exception, match="HTTPS"):
        validate_profile(ProviderProfile("remote", "Remote", "openai_compatible", "http://example.com"))

    async def scenario() -> None:
        repository = CoreRepository(tmp_path / "policy.db")
        project = repository.create_project("Private", "work")
        repository.update_project_model_policy(project["id"], "local_only")
        work = repository.create_conversation("project_work", "Work", project_id=project["id"])

        async def sink(_event):
            return None

        service = ChatRunService(repository, sink)
        service.configure_provider({"id": "online", "name": "Online", "kind": "openai_compatible",
                                    "base_url": "https://example.com", "api_key": "secret",
                                    "allowed_model_ids": ["model"]})
        with pytest.raises(CoreDataError, match="local models"):
            await service.start({"conversation_id": work["id"], "content": "hello",
                                 "idempotency_key": "online-denied",
                                 "provider_profile_id": "online", "model_id": "model"})

    asyncio.run(scenario())


def test_provider_errors_have_stable_codes() -> None:
    assert _map_http_error(httpx.Response(401)).code == "authentication_failed"
    assert _map_http_error(httpx.Response(429, headers={"retry-after": "12"})).retry_after == 12
    assert _map_http_error(httpx.Response(500, text="CUDA out of memory")).code == "resource_exhausted"
    assert _map_http_error(httpx.Response(400, text="maximum context length exceeded")).code \
        == "provider_context_limit_exceeded"
    assert _map_http_error(httpx.Response(
        400, text="Unsupported parameter: response_format",
    )).code == "response_format_unsupported"


def test_anthropic_messages_is_a_valid_bound_provider_protocol() -> None:
    validate_profile(ProviderProfile(
        "deepseek", "DeepSeek", "openai_compatible", "https://api.deepseek.com/anthropic",
        primary_protocol="anthropic_messages",
    ))


def test_successful_run_commits_one_assistant_message(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = CoreRepository(tmp_path / "success.db")
        conversation = repository.create_conversation("standard_chat", "Chat", "casual")

        async def sink(_event):
            return None

        service = ChatRunService(repository, sink)
        started = await service.start({"conversation_id": conversation["id"], "content": "hello",
                                       "idempotency_key": "success", "provider_profile_id": "builtin-demo",
                                       "model_id": "demo"})
        await service.tasks[started["run"]["id"]]
        run = repository.get_model_run(started["run"]["id"])
        assert run["status"] == "completed"
        assert run["assistant_message"]["content"].startswith("Built-in Demo · Local")
        assert run["draft"] is None
        assert [item["role"] for item in repository.get_conversation(conversation["id"])["messages"]] == ["user", "assistant"]
        snapshot = service.context.context_repository.preview(run["id"])
        current = next(item for item in snapshot["items"] if item["source_type"] == "current_message")
        preview = service.context.context_repository.source_preview(run["id"], current["id"])
        assert preview["source"]["content"] == "hello"

    asyncio.run(scenario())


def test_short_run_does_not_schedule_post_run_compaction(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = CoreRepository(tmp_path / "short-no-compaction.db")
        conversation = repository.create_conversation("standard_chat", "Chat", "casual")

        async def sink(_event):
            return None

        service = ChatRunService(repository, sink)
        started = await service.start({
            "conversation_id": conversation["id"], "content": "hello",
            "idempotency_key": "short", "provider_profile_id": "builtin-demo", "model_id": "demo",
        })
        await service.tasks[started["run"]["id"]]
        assert conversation["id"] not in service.compaction_tasks
        assert service.compaction_status(conversation["id"]) is None

    asyncio.run(scenario())


def test_active_compaction_blocks_send_before_user_message_is_saved(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = CoreRepository(tmp_path / "compaction-gate.db")
        conversation = repository.create_conversation("standard_chat", "Chat", "casual")
        run = repository.start_model_run(
            content="existing", idempotency_key="existing", provider_profile_id="builtin-demo",
            model_id="demo", conversation_id=conversation["id"],
        )
        repository.transition_model_run(run["id"], {"queued"}, "cancelled")

        async def sink(_event):
            return None

        service = ChatRunService(repository, sink)
        service.context.context_repository.begin_compaction(
            conversation_id=conversation["id"], run_id=run["id"], context_generation=1,
            source_hash="source", idempotency_key="active-compaction",
        )
        before = len(repository.get_conversation(conversation["id"])["messages"])
        with pytest.raises(CoreDataError) as captured:
            await service.start({
                "conversation_id": conversation["id"], "content": "must not be saved",
                "idempotency_key": "blocked", "provider_profile_id": "builtin-demo", "model_id": "demo",
            })
        assert captured.value.code == "context_compaction_in_progress"
        assert len(repository.get_conversation(conversation["id"])["messages"]) == before

    asyncio.run(scenario())


def test_project_work_natural_language_tool_round_resumes_generation(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = CoreRepository(tmp_path / "natural-tools.db")
        project = repository.create_project("Project", "coding")
        work = repository.create_conversation(
            "project_work", "Inspect files", project_id=project["id"],
        )
        events = []
        service: ChatRunService

        async def sink(event):
            events.append(event)
            if event["event_type"] == "run.tool_requested":
                operation = event["data"]["operation"]
                service.tool_status(operation["id"], "executing")
                service.resolve_tool(
                    operation["id"], True, "README.md\nsrc/",
                )

        service = ChatRunService(repository, sink)
        started = await service.start({
            "conversation_id": work["id"],
            "content": "请查看目录并告诉我有哪些文件",
            "idempotency_key": "natural-tool-round",
            "provider_profile_id": "builtin-demo",
            "model_id": "demo",
        })
        await service.tasks[started["run"]["id"]]

        run = repository.get_model_run(started["run"]["id"])
        assert run["status"] == "completed"
        assert "README.md" in run["assistant_message"]["content"]
        operations = service.list_tool_operations(run["id"])
        assert [(item["tool_name"], item["status"]) for item in operations] == [
            ("fs.list", "completed"),
        ]
        event_types = [event["event_type"] for event in events]
        assert "run.tool_requested" in event_types
        assert "run.tool_completed" in event_types
        assert event_types[-1] == "run.completed"
        snapshot = service.context.context_repository.preview(run["id"])
        assert snapshot is not None
        assert snapshot["round_no"] == 2
        assert snapshot['input_scope'] == 'compiled_request'
        assert any(item["source_type"] == "tool_round" for item in snapshot["items"])

    asyncio.run(scenario())


def test_reasoning_state_is_returned_only_to_the_next_tool_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        repository = CoreRepository(tmp_path / "reasoning-tool-round.db")
        project = repository.create_project("Project", "coding")
        work = repository.create_conversation(
            "project_work", "Inspect files", project_id=project["id"],
        )
        captured_messages: list[list[dict]] = []

        class Provider:
            def __init__(self, profile):
                self.profile = profile

            async def stream_events(self, _model_id, messages, **_kwargs):
                captured_messages.append(messages)
                if len(captured_messages) == 1:
                    yield ProviderEvent(
                        "activity", opaque_reasoning_delta="private-reasoning-state",
                    )
                    yield ProviderEvent(
                        "tool_call", tool_call=ToolCall("call-1", "fs.list", {"path": "."}),
                    )
                else:
                    yield ProviderEvent("text_delta", text="Finished safely.")

        monkeypatch.setattr("agent.runtime.create_provider", Provider)
        service: ChatRunService

        async def sink(event):
            if event["event_type"] == "run.tool_requested":
                operation = event["data"]["operation"]
                service.tool_status(operation["id"], "executing")
                service.resolve_tool(operation["id"], True, "README.md")

        service = ChatRunService(repository, sink)
        started = await service.start({
            "conversation_id": work["id"], "content": "Inspect the project",
            "idempotency_key": "reasoning-round", "provider_profile_id": "builtin-demo",
            "model_id": "demo",
        })
        await service.tasks[started["run"]["id"]]

        assistant_tool_message = next(
            message for message in captured_messages[1]
            if message.get("role") == "assistant" and message.get("tool_calls")
        )
        assert assistant_tool_message["reasoning_content"] == "private-reasoning-state"
        conversation = repository.get_conversation(work["id"])
        assert "private-reasoning-state" not in str(conversation["messages"])
        assert repository.get_model_run(started["run"]["id"])["status"] == "completed"

    asyncio.run(scenario())


def test_native_tool_protocol_gets_one_bounded_correction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        repository = CoreRepository(tmp_path / "native-tool-correction.db")
        project = repository.create_project("Project", "coding")
        work = repository.create_conversation(
            "project_work", "Repair tool format", project_id=project["id"],
        )
        captured_messages: list[list[dict]] = []

        class Provider:
            def __init__(self, profile):
                self.profile = profile

            async def stream_events(self, _model_id, messages, **_kwargs):
                captured_messages.append(messages)
                if len(captured_messages) == 1:
                    raise ProviderError(
                        "model_tool_protocol_failed", "Model returned an invalid tool call.",
                        details={"native_tool_correction_eligible": True},
                    )
                yield ProviderEvent("text_delta", text="Recovered after one correction.")

        monkeypatch.setattr("agent.runtime.create_provider", Provider)

        async def sink(_event):
            return None

        service = ChatRunService(repository, sink)
        started = await service.start({
            "conversation_id": work["id"], "content": "Inspect the project",
            "idempotency_key": "native-repair", "provider_profile_id": "builtin-demo",
            "model_id": "demo",
        })
        await service.tasks[started["run"]["id"]]

        run = repository.get_model_run(started["run"]["id"])
        assert run["status"] == "completed"
        assert run["assistant_message"]["content"] == "Recovered after one correction."
        assert len(captured_messages) == 2
        assert any(
            message.get("role") == "system" and "previous tool call was rejected" in message["content"]
            for message in captured_messages[1]
        )
        assert service.list_tool_operations(run["id"]) == []

    asyncio.run(scenario())


def test_fake_model_naturally_reads_then_proposes_a_file_change(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = CoreRepository(tmp_path / "natural-edit.db")
        project = repository.create_project("Project", "coding")
        work = repository.create_conversation(
            "project_work", "Edit documentation", project_id=project["id"],
        )
        service: ChatRunService

        async def sink(event):
            if event["event_type"] != "run.tool_requested":
                return
            operation = event["data"]["operation"]
            service.tool_status(operation["id"], "executing")
            if operation["tool_name"] == "fs.read":
                service.resolve_tool(operation["id"], True, (
                    '{"path":"README.md","content":"# Demo","sha256":'
                    '"2b1216f36f2f4a649f26740d859b42492f9c0f75cde1e3ee33ef35a6b059a38f"}'
                ))
            else:
                service.resolve_tool(operation["id"], True, '{"changed_files":1}')

        service = ChatRunService(repository, sink)
        started = await service.start({
            "conversation_id": work["id"],
            "content": "请读取 README.md 并补充安装说明",
            "idempotency_key": "natural-edit-round",
            "provider_profile_id": "builtin-demo",
            "model_id": "demo",
        })
        await service.tasks[started["run"]["id"]]
        run = repository.get_model_run(started["run"]["id"])
        assert run["status"] == "completed"
        operations = service.list_tool_operations(run["id"])
        assert [item["tool_name"] for item in operations] == [
            "fs.read", "fs.propose_changes",
        ]
        assert operations[1]["arguments"]["mutations"][0]["path"] == "README.md"

    asyncio.run(scenario())


def test_long_context_requires_checkpoint_without_removing_messages(tmp_path: Path) -> None:
    repository = CoreRepository(tmp_path / "summary.db")
    conversation = repository.create_conversation("standard_chat", "Long", "research")
    for index in range(12):
        repository._insert_message(
            conversation["id"], "user", f"history {index} " + "x" * 700,
        )
        repository._insert_message(
            conversation["id"], "assistant", f"answer {index} " + "y" * 700,
        )
    before = len(repository.get_conversation(conversation["id"])["messages"])
    run = repository.start_model_run(
        content="latest question", idempotency_key="summary-run",
        provider_profile_id="builtin-demo", model_id="demo", conversation_id=conversation["id"],
    )
    with pytest.raises(ContextEngineError) as captured:
        ContextAssembler(repository).assemble(run, token_budget=1_200)
    assert captured.value.code == "context_compaction_required"
    assert len(repository.get_conversation(conversation["id"])["messages"]) == before + 1
