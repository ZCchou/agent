import asyncio
import sqlite3
from pathlib import Path

import pytest
import httpx

from agent.providers import (
    GenerationOptions, OpenAICompatibleProvider, ProviderError, ProviderProfile,
    model_list_base_candidates,
)
from agent.runtime import ChatRunService
from agent.storage import CoreDataError, CoreRepository


def test_v5_to_v6_adds_model_scope_without_changing_records(tmp_path: Path) -> None:
    path = tmp_path / "v5.db"
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        CoreRepository._create_schema_v4(connection)
        connection.execute("CREATE TABLE schema_info(version INTEGER NOT NULL)")
        connection.execute("INSERT INTO schema_info VALUES (5)")
        CoreRepository._migrate_v4_to_v5(connection)
        connection.execute(
            """INSERT INTO projects(id,name,sort_order,category,created_at,updated_at,deleted_at,
                                     model_access_policy)
               VALUES ('p','Project',0,'coding','now','now',NULL,'local_only')"""
        )
        connection.execute(
            """INSERT INTO conversations(id,kind,title,category,chat_group_id,project_id,sort_order,
                                          created_at,updated_at,deleted_at)
               VALUES ('c','project_work','Work',NULL,NULL,'p',0,'now','now',NULL)"""
        )
        connection.execute(
            """INSERT INTO messages(id,conversation_id,role,content,event_type,source_message_id,created_at)
               VALUES ('m','c','user','hello',NULL,NULL,'now')"""
        )
        before = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("projects", "conversations", "messages")
        }
    CoreRepository(path)
    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(projects)")}
        run_columns = {row[1] for row in connection.execute("PRAGMA table_info(model_runs)")}
        after = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("projects", "conversations", "messages")
        }
        version = connection.execute("SELECT version FROM schema_info").fetchone()[0]
        policy = connection.execute("SELECT model_scope_policy FROM projects WHERE id = 'p'").fetchone()[0]
        assert version == 27
    assert before == after
    assert policy == "inherit_global"
    assert "model_scope_policy" in columns
    assert {"selection_source", "selection_reason_code", "routing_policy_version"} <= run_columns


def test_v4_to_v6_defaults_to_global_scope_and_records_fixed_decision(tmp_path: Path) -> None:
    repository = CoreRepository(tmp_path / "model-scope.db")
    project = repository.create_project("Project", "coding")
    assert repository.get_project_model_scope(project["id"]) == {
        "project_id": project["id"], "policy": "inherit_global", "models": [],
    }
    conversation = repository.create_conversation("project_work", "Work", project_id=project["id"])
    run = repository.start_model_run(
        content="hello", idempotency_key="decision", conversation_id=conversation["id"],
        provider_profile_id="builtin-demo", model_id="demo",
        selection_source="conversation_override", selection_reason_code="fixed_selection",
    )
    assert run["selection_source"] == "conversation_override"
    assert run["selection_reason_code"] == "fixed_selection"
    assert run["routing_policy_version"] is None


def test_fixed_selector_enforces_admission_and_project_intersection(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = CoreRepository(tmp_path / "admission.db")
        project = repository.create_project("Project", "coding")
        work = repository.create_conversation("project_work", "Work", project_id=project["id"])

        async def sink(_event):
            return None

        service = ChatRunService(repository, sink)
        service.configure_provider({
            "id": "local", "name": "Local", "kind": "ollama",
            "base_url": "http://127.0.0.1:11434", "allowed_model_ids": ["a", "b"],
            "model_capabilities": {"a": {"text": "yes"}, "b": {"text": "yes"}},
        })
        repository.update_project_model_scope(
            project["id"], "restricted",
            [{"provider_profile_id": "local", "model_id": "a"}],
        )
        with pytest.raises(CoreDataError) as denied:
            await service.start({
                "conversation_id": work["id"], "content": "hello", "idempotency_key": "denied",
                "provider_profile_id": "local", "model_id": "b",
            })
        assert denied.value.code == "model_not_enabled"
        with pytest.raises(CoreDataError) as routing:
            await service.start({
                "conversation_id": work["id"], "content": "hello", "idempotency_key": "route",
                "selection": {"mode": "automatic"},
            })
        assert routing.value.code == "no_compatible_execution_plan"

    asyncio.run(scenario())


def test_fixed_selector_rejects_a_model_without_text_capability(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = CoreRepository(tmp_path / "capability.db")
        conversation = repository.create_conversation("standard_chat", "Chat", "casual")

        async def sink(_event):
            return None

        service = ChatRunService(repository, sink)
        service.configure_provider({
            "id": "local", "name": "Local", "kind": "ollama",
            "base_url": "http://127.0.0.1:11434", "allowed_model_ids": ["embedding"],
            "model_capabilities": {"embedding": {"text": "no"}},
        })
        with pytest.raises(CoreDataError) as denied:
            await service.start({
                "conversation_id": conversation["id"], "content": "hello",
                "idempotency_key": "capability", "provider_profile_id": "local",
                "model_id": "embedding",
            })
        assert denied.value.code == "model_capability_unsupported"

    asyncio.run(scenario())


def test_idempotent_replay_reuses_original_selection_after_admission_changes(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = CoreRepository(tmp_path / "idempotency.db")
        conversation = repository.create_conversation("standard_chat", "Chat", "casual")

        async def sink(_event):
            return None

        service = ChatRunService(repository, sink)
        first = await service.start({
            "conversation_id": conversation["id"], "content": "hello", "idempotency_key": "same",
            "provider_profile_id": "builtin-demo", "model_id": "demo",
            "selection_source": "user_once",
        })
        service.profiles["builtin-demo"] = ProviderProfile(
            "builtin-demo", "Built-in Demo", "fake", "memory://fake", allowed_model_ids=(),
        )
        replay = await service.start({
            "conversation_id": conversation["id"], "content": "hello", "idempotency_key": "same",
            "provider_profile_id": "builtin-demo", "model_id": "demo",
        })
        assert replay["run"]["id"] == first["run"]["id"]
        assert replay["selection_decision"]["selection_source"] == "user_once"
        await service.shutdown()

    asyncio.run(scenario())


def test_api_base_candidates_never_duplicate_v1_or_guess_custom_paths() -> None:
    assert model_list_base_candidates("https://api.moonshot.cn/v1", "preset") == [
        "https://api.moonshot.cn/v1"
    ]
    assert model_list_base_candidates("https://api.deepseek.com", "preset") == [
        "https://api.deepseek.com", "https://api.deepseek.com/v1"
    ]
    assert model_list_base_candidates("https://gateway.example/team/api", "exact") == [
        "https://gateway.example/team/api"
    ]
    assert model_list_base_candidates("https://legacy.example", "legacy_auto_v1") == [
        "https://legacy.example/v1", "https://legacy.example"
    ]
    assert model_list_base_candidates("https://legacy.example/v1", "legacy_auto_v1") == [
        "https://legacy.example/v1"
    ]


def test_custom_root_falls_back_only_after_404(monkeypatch: pytest.MonkeyPatch) -> None:
    requested: list[str] = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url: str, **_kwargs):
            requested.append(url)
            if url.endswith("/v1/models"):
                return httpx.Response(200, json={"data": [{"id": "model-a"}]})
            return httpx.Response(404, json={"error": "missing"})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: Client())
    provider = OpenAICompatibleProvider(ProviderProfile(
        "custom", "Custom", "openai_compatible", "https://gateway.example", endpoint_mode="exact",
    ))
    models, resolved = asyncio.run(provider.discover_models())
    assert requested == ["https://gateway.example/models", "https://gateway.example/v1/models"]
    assert resolved == "https://gateway.example/v1"
    assert models[0]["id"] == "model-a"


def test_authentication_failure_does_not_guess_another_path(monkeypatch: pytest.MonkeyPatch) -> None:
    requested: list[str] = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url: str, **_kwargs):
            requested.append(url)
            return httpx.Response(401, json={"error": "invalid key"})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: Client())
    provider = OpenAICompatibleProvider(ProviderProfile(
        "custom", "Custom", "openai_compatible", "https://gateway.example", endpoint_mode="exact",
    ))
    with pytest.raises(ProviderError) as denied:
        asyncio.run(provider.discover_models())
    assert denied.value.code == "authentication_failed"
    assert requested == ["https://gateway.example/models"]


def test_invalid_model_catalog_has_a_stable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _url: str, **_kwargs):
            return httpx.Response(200, json={"data": "not-an-array"})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: Client())
    provider = OpenAICompatibleProvider(ProviderProfile(
        "custom", "Custom", "openai_compatible", "https://gateway.example/v1",
    ))
    with pytest.raises(ProviderError) as denied:
        asyncio.run(provider.discover_models())
    assert denied.value.code == "invalid_provider_response"


def test_openai_compatible_json_mode_and_finish_reason_are_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    class StreamResponse:
        is_error = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def aiter_lines(self):
            yield 'data: {"choices":[{"delta":{"content":"{}"},"finish_reason":null}]}'
            yield 'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'
            yield "data: [DONE]"

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def stream(self, method: str, url: str, **kwargs):
            captured.update({"method": method, "url": url, **kwargs})
            return StreamResponse()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: Client())
    provider = OpenAICompatibleProvider(ProviderProfile(
        "custom", "Custom", "openai_compatible", "https://gateway.example/v1",
    ))

    async def collect():
        return [event async for event in provider.stream_events(
            "model", [{"role": "user", "content": "Return JSON"}], tools=False,
            options=GenerationOptions(
                purpose="context_compaction", max_output_tokens=8_192,
                response_format="json_object",
            ),
        )]

    events = asyncio.run(collect())
    assert captured["json"]["max_tokens"] == 8_192
    assert captured["json"]["response_format"] == {"type": "json_object"}
    assert [(event.kind, event.finish_reason) for event in events] == [
        ("text_delta", None), ("finish", "stop"),
    ]


def _responses_provider_with_lines(
    monkeypatch: pytest.MonkeyPatch, lines: list[str],
) -> OpenAICompatibleProvider:
    class StreamResponse:
        is_error = False
        headers = {"x-request-id": "request-secret-value"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def aiter_lines(self):
            for line in lines:
                yield line

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def stream(self, *_args, **_kwargs):
            return StreamResponse()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: Client())
    return OpenAICompatibleProvider(ProviderProfile(
        "custom", "Custom", "openai_compatible", "https://gateway.example/v1",
        primary_protocol="openai_responses",
    ))


def test_responses_uses_completed_output_when_gateway_omits_text_deltas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _responses_provider_with_lines(monkeypatch, [
        'data: {"type":"response.completed","response":{"status":"completed",'
        '"output":[{"type":"message","content":[{"type":"output_text",'
        '"text":"final-only answer"}]}],"usage":{"input_tokens":12,"output_tokens":3}}}',
        "data: [DONE]",
    ])

    async def collect():
        return [event async for event in provider.stream_events(
            "model", [{"role": "user", "content": "hello"}], tools=False,
            options=GenerationOptions(),
        )]

    events = asyncio.run(collect())
    assert "".join(event.text for event in events if event.kind == "text_delta") \
        == "final-only answer"
    diagnostic = next(event.diagnostic for event in events if event.kind == "provider_diagnostic")
    assert diagnostic["output_item_types"] == ["message", "output_text"]
    assert diagnostic["usage_present"] is True
    assert diagnostic["request_id_hash"] != "request-secret-value"


@pytest.mark.parametrize(("terminal", "expected_code"), [
    (
        '{"type":"response.incomplete","response":{"status":"incomplete",'
        '"incomplete_details":{"reason":"max_output_tokens"}}}',
        "provider_response_incomplete",
    ),
    (
        '{"type":"response.completed","response":{"status":"completed",'
        '"output":[{"type":"message","content":[{"type":"refusal",'
        '"refusal":"not allowed"}]}]}}',
        "provider_refusal",
    ),
    ('{"type":"response.failed","response":{"status":"failed"}}', "provider_response_failed"),
])
def test_responses_terminal_failures_have_precise_codes(
    monkeypatch: pytest.MonkeyPatch, terminal: str, expected_code: str,
) -> None:
    provider = _responses_provider_with_lines(
        monkeypatch, [f"data: {terminal}", "data: [DONE]"],
    )

    async def collect():
        return [event async for event in provider.stream_events(
            "model", [{"role": "user", "content": "hello"}], tools=False,
            options=GenerationOptions(),
        )]

    with pytest.raises(ProviderError) as denied:
        asyncio.run(collect())
    assert denied.value.code == expected_code
    assert "event_types" in denied.value.details


def test_responses_unknown_terminal_shape_is_not_reported_as_generic_empty_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _responses_provider_with_lines(monkeypatch, [
        'data: {"type":"response.vendor_message.done","message":{"value":"hidden"}}',
        'data: {"type":"response.completed","response":{"status":"completed","output":[]}}',
        "data: [DONE]",
    ])

    async def collect():
        return [event async for event in provider.stream_events(
            "model", [{"role": "user", "content": "hello"}], tools=False,
            options=GenerationOptions(),
        )]

    with pytest.raises(ProviderError) as denied:
        asyncio.run(collect())
    assert denied.value.code == "provider_event_unsupported"
    assert denied.value.details["unknown_event_types"] == ["response.vendor_message.done"]


def test_openai_tool_stream_preserves_reasoning_and_ignores_repeated_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StreamResponse:
        is_error = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def aiter_lines(self):
            yield 'data: {"choices":[{"delta":{"reasoning_content":"private-state"}}]}'
            yield (
                'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-1",'
                '"function":{"name":"fs_list","arguments":"{\\"path\\":"}}]}}]}'
            )
            # Some compatible gateways repeat id and name in every chunk. They
            # are stable metadata, not string fragments to concatenate.
            yield (
                'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-1",'
                '"function":{"name":"fs_list","arguments":"\\".\\"}"}}]}}]}'
            )
            yield "data: [DONE]"

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def stream(self, *_args, **_kwargs):
            return StreamResponse()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: Client())
    provider = OpenAICompatibleProvider(ProviderProfile(
        "deepseek", "DeepSeek", "openai_compatible", "https://api.deepseek.com",
    ))

    async def collect():
        return [event async for event in provider.stream_events(
            "deepseek-chat", [{"role": "user", "content": "List files"}], tools=True,
        )]

    events = asyncio.run(collect())
    assert events[0].kind == "activity"
    assert events[0].opaque_reasoning_delta == "private-state"
    assert events[1].tool_call is not None
    assert events[1].tool_call.provider_call_id == "call-1"
    assert events[1].tool_call.name == "fs.list"
    assert events[1].tool_call.arguments == {"path": "."}


def test_kimi_formula_search_is_not_exposed_as_a_host_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        is_error = False

        def __init__(self, body):
            self.body = body

        async def aread(self):
            return b""

        def json(self):
            return self.body

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def __init__(self):
            self.chat_round = 0

        async def get(self, *_args, **_kwargs):
            return Response({
                "tools": [{
                    "type": "function", "function": {
                        "name": "web_search", "parameters": {
                            "type": "object", "properties": {"query": {"type": "string"}},
                            "required": ["query"],
                        },
                    },
                }],
            })

        async def post(self, url, *_args, **_kwargs):
            if url.endswith("/fibers"):
                return Response({"id": "fiber-1", "context": {"encrypted_output": "opaque"}})
            self.chat_round += 1
            if self.chat_round == 1:
                return Response({"choices": [{"finish_reason": "tool_calls", "message": {
                    "role": "assistant", "content": None, "tool_calls": [{
                        "id": "search-1", "type": "function", "function": {
                            "name": "web_search", "arguments": '{"query":"latest anime"}',
                        },
                    }],
                }}], "usage": {"prompt_tokens": 5, "completion_tokens": 2}})
            return Response({"choices": [{"finish_reason": "stop", "message": {
                "role": "assistant", "content": "results",
            }}], "usage": {"prompt_tokens": 5, "completion_tokens": 3}})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: Client())
    provider = OpenAICompatibleProvider(ProviderProfile(
        "kimi", "Kimi", "openai_compatible", "https://api.moonshot.cn/v1", api_key="test",
    ))

    async def collect():
        return [event async for event in provider.stream_events(
            "kimi-k3", [{"role": "user", "content": "Search"}], tools=False,
            options=GenerationOptions(
                    purpose="chat", invocation_adapter_id="kimi.formula_search",
                capability_signature=("native_web_search", "text"),
                native_search_mode="required",
            ),
        )]

    events = asyncio.run(collect())
    assert [event.kind for event in events] == [
        "usage", "web_search_started", "web_search_result", "usage", "text_delta", "finish",
    ]
    assert events[2].web_search["sources"] == []
