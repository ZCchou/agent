from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

from agent.execution.compiler import InvocationCompiler, canonical_adapter_id
from agent.execution.domain import InvocationSpec, invocation_limits_for
from agent.execution.native_search import NativeSearchExecutor
from agent.providers import ProviderError, ProviderEvent, ProviderProfile
from agent.runtime import ChatRunService
from agent.storage import CoreDataError, CoreRepository
from agent.tool_contracts import ToolCall


def model_spec(**overrides) -> InvocationSpec:
    values = dict(
        spec_id="spec", plan_id="plan", step_id="step", round_no=1,
        attempt_no=1, kind="model", provider_or_backend_id="provider",
        model_id="model", route_id="openai.responses",
        protocol_id="openai_responses", adapter_id="openai.responses",
        endpoint_role="final_response", route_fingerprint="route-v3",
        capability_signature=("text",),
    )
    values.update(overrides)
    return InvocationSpec(**values)


def test_compiler_rejects_protocol_adapter_mismatch_before_dispatch() -> None:
    with pytest.raises(CoreDataError, match="does not match") as error:
        InvocationCompiler().compile_model(
            model_spec(protocol_id="anthropic_messages"),
            [{"role": "user", "content": "hello"}],
        )
    assert error.value.code == "invocation_protocol_mismatch"


def test_compiler_cannot_add_tools_or_native_search_outside_the_spec() -> None:
    with pytest.raises(CoreDataError) as tool_error:
        InvocationCompiler().compile_model(
            model_spec(allowed_tool_names=("fs.read",)),
            [{"role": "user", "content": "hello"}],
        )
    assert tool_error.value.code == "capability_combination_unsupported"

    with pytest.raises(CoreDataError) as search_error:
        InvocationCompiler().compile_model(
            model_spec(native_search_mode="required"),
            [{"role": "user", "content": "hello"}],
        )
    assert search_error.value.code == "invocation_spec_invalid"


def test_v2_adapter_names_are_normalized_before_spec_persistence() -> None:
    assert canonical_adapter_id(
        "deepseek.anthropic_web", "anthropic_messages",
        "anthropic_messages_web_search",
    ) == "deepseek.anthropic_web"
    assert canonical_adapter_id(
        "kimi.openai_chat", "openai_chat", "kimi_builtin_web_search",
    ) == "kimi.openai_chat"


def test_deepseek_native_search_compiles_to_one_anthropic_dialect() -> None:
    prepared = InvocationCompiler().compile_model(
        model_spec(
            route_id="deepseek.anthropic_web",
            protocol_id="anthropic_messages",
            adapter_id="deepseek.anthropic_web",
            capability_signature=("native_web_search", "text"),
            native_search_mode="required",
        ),
        [{"role": "user", "content": "latest computer vision papers"}],
    )

    assert prepared.adapter_id == "deepseek.anthropic_web"
    assert prepared.protocol_id == "anthropic_messages"
    assert prepared.options.invocation_adapter_id == "deepseek.anthropic_web"
    assert prepared.options.native_search_required is True
    assert prepared.options.native_transport_adapter == "anthropic_messages_web_search"


def test_native_search_uses_a_search_specific_activity_window() -> None:
    assert invocation_limits_for(("text",)) == (10_000, 30_000, 120_000)
    assert invocation_limits_for(("function_tools", "text")) == (
        10_000, 60_000, 180_000,
    )
    assert invocation_limits_for(("native_web_search", "text")) == (
        10_000, 60_000, 180_000,
    )


def test_native_search_adapters_declare_an_executable_transport() -> None:
    from agent.execution.compiler import ADAPTER_DIALECTS

    assert ADAPTER_DIALECTS["kimi.openai_chat"].native_search_transport \
        == "client_continuation"
    assert ADAPTER_DIALECTS["openai.responses"].native_search_transport \
        == "server_managed"
    assert ADAPTER_DIALECTS["deepseek.anthropic_web"].native_search_transport \
        == "server_managed"
    assert ADAPTER_DIALECTS["openai.chat"].native_search_transport == "none"


def test_native_search_executor_never_uses_the_host_tool_allowlist() -> None:
    executor = NativeSearchExecutor()
    event = ProviderEvent(
        "native_search_call",
        tool_call=ToolCall("search-1", "native.web_search", {"query": "latest anime"}),
    )

    assert executor.accept_event(event, ("native_web_search", "text")) == event.tool_call
    with pytest.raises(CoreDataError) as denied:
        executor.accept_event(event, ("text",))
    assert denied.value.code == "native_search_event_unauthorized"


def test_empty_model_result_fails_invocation_before_it_can_be_marked_completed() -> None:
    class ExecutionRepositoryStub:
        def __init__(self) -> None:
            self.finished: list[tuple[str, str, str | None]] = []

        def save_invocation_spec(self, _spec):
            return None

        def mark_invocation_dispatched(self, _spec_id, _fingerprint):
            return None

        def finish_invocation(self, spec_id, status, error_code=None):
            self.finished.append((spec_id, status, error_code))

    class InvocationServiceStub:
        async def complete_prepared(self, _provider, _spec, _prepared):
            return "", {}, [ProviderEvent("finish", finish_reason="completed")]

    async def scenario() -> None:
        service = object.__new__(ChatRunService)
        service.execution_repository = ExecutionRepositoryStub()
        service.repository = SimpleNamespace(
            bind_attempt_invocation_spec=lambda *_args: None,
            get_model_run=lambda _run_id: {"status": "generating", "web_search_route": "none"},
            get_run_conversation=lambda _run_id: {"kind": "standard_chat"},
        )
        service.profiles = {"provider": ProviderProfile(
            "provider", "Offline", "fake", "memory://fake", allowed_model_ids=("model",),
            primary_protocol="openai_responses",
        )}
        service.invocation_compiler = SimpleNamespace(compile_model=lambda *_args: SimpleNamespace(
            request_fingerprint="request-fingerprint",
        ))
        service.model_invocation = InvocationServiceStub()
        service._create_model_invocation_spec = lambda *_args, **_kwargs: model_spec()

        async def emit(*_args, **_kwargs):
            return None

        service._emit = emit
        with pytest.raises(ProviderError) as denied:
            await service._invoke_planned_step(
                {"id": "run"}, {"id": "plan"}, {
                    "id": "step", "provider_profile_id": "provider", "model_id": "model",
                    "protocol_id": "openai_responses",
                },
                {"id": "attempt", "attempt_number": 1}, object(),
                [{"role": "user", "content": "hello"}], max_output_tokens=32,
            )
        assert denied.value.code == "provider_empty_output"
        assert service.execution_repository.finished == [
            ("spec", "failed", "provider_empty_output"),
        ]

    asyncio.run(scenario())


def test_builtin_search_argument_sources_are_explicit_https_only() -> None:
    sources = ChatRunService._sources_from_native_search_arguments({
        "query": "latest papers",
        "results": [
            {"title": "Paper", "url": "https://example.test/paper", "snippet": "Summary"},
            {"title": "Local", "url": "file:///secret"},
            {"title": "Duplicate", "link": "https://example.test/paper"},
        ],
    })

    assert sources == [{
        "provider_source_id": "https://example.test/paper",
        "title": "Paper",
        "url": "https://example.test/paper",
        "snippet": "Summary",
    }]


def test_search_specialist_completes_builtin_tool_continuation_on_same_target() -> None:
    async def scenario() -> None:
        service = object.__new__(ChatRunService)
        service.native_search_executor = NativeSearchExecutor()
        rounds: list[tuple[int, str | None]] = []

        async def invoke(*_args, **kwargs):
            rounds.append((kwargs["round_no"], kwargs["native_search_mode"]))
            if kwargs["round_no"] == 1:
                return "", {"input_tokens": 10}, [ProviderEvent(
                    "native_search_call", tool_call=ToolCall(
                        "search-1", "native.web_search", {
                            "query": "latest papers",
                            "results": [{
                                "title": "Paper", "url": "https://example.test/paper",
                            }],
                        },
                    ),
                )]
            return "researched answer", {"output_tokens": 5}, []

        service._invoke_planned_step = invoke
        text, usage, events = await service._invoke_search_specialist_step(
            {"id": "run"}, {"id": "plan"}, {
                "id": "step", "capability_signature": ["native_web_search", "text"],
            },
            {"attempt_number": 1}, object(), [{"role": "user", "content": "search"}],
            max_output_tokens=128,
        )

        assert rounds == [(1, "required"), (2, "auto")]
        assert text == "researched answer"
        assert usage == {"input_tokens": 10, "output_tokens": 5}
        assert [event.kind for event in events] == [
            "native_search_call", "web_search_started", "web_search_result",
        ]

    asyncio.run(scenario())


def test_required_search_without_search_event_is_not_reported_as_invalid_result() -> None:
    async def scenario() -> None:
        service = object.__new__(ChatRunService)
        service.native_search_executor = NativeSearchExecutor()

        async def invoke(*_args, **_kwargs):
            return "I answered from memory", {"output_tokens": 4}, [ProviderEvent(
                "finish", finish_reason="stop",
            )]

        service._invoke_planned_step = invoke
        with pytest.raises(ProviderError) as error:
            await service._invoke_search_specialist_step(
                {"id": "run"}, {"id": "plan"}, {
                    "id": "step", "capability_signature": ["native_web_search", "text"],
                }, {"attempt_number": 1}, object(),
                [{"role": "user", "content": "search"}], max_output_tokens=128,
            )
        assert error.value.code == "web_search_not_executed"

    asyncio.run(scenario())


def test_web_specialist_receives_prior_vision_artifact_without_the_image() -> None:
    artifacts = [{
        "artifact_id": "vision-artifact",
        "step_type": "vision_analysis",
        "text": "The image shows a person named Example Person.",
    }]

    selected = ChatRunService._prior_artifacts_for_specialist("web_research", artifacts)
    messages = ChatRunService._messages_with_specialist_artifacts(
        [{"role": "user", "content": [
            {"type": "text", "text": "Who is this? Search the web."},
            {"type": "image", "data_base64": "not-forwarded"},
        ]}],
        selected,
        strip_images=True,
    )

    assert selected == artifacts
    assert all(
        part.get("type") != "image"
        for message in messages if isinstance(message.get("content"), list)
        for part in message["content"] if isinstance(part, dict)
    )
    assert "Example Person" in messages[-1]["content"]
    assert ChatRunService._prior_artifacts_for_specialist("vision_analysis", artifacts) == []


def test_model_run_persists_one_plan_and_a_completed_exact_spec(tmp_path) -> None:
    async def scenario() -> None:
        repository = CoreRepository(tmp_path / "unified.db")

        async def sink(_event):
            return None

        service = ChatRunService(repository, sink)
        started = await service.start({
            "content": "hello", "idempotency_key": "unified-run",
            "provider_profile_id": "builtin-demo", "model_id": "demo",
            "kind": "standard_chat", "category": "casual",
            "selection": {
                "mode": "fixed", "provider_profile_id": "builtin-demo",
                "model_id": "demo", "source": "user_once",
            },
        })
        await service.tasks[started["run"]["id"]]
        with sqlite3.connect(repository.database_path) as connection:
            connection.row_factory = sqlite3.Row
            plans = connection.execute(
                "SELECT * FROM execution_plans WHERE run_id = ?", (started["run"]["id"],),
            ).fetchall()
            specs = connection.execute(
                "SELECT * FROM invocation_specs WHERE plan_id = ?", (plans[0]["id"],),
            ).fetchall()
            attempt = connection.execute(
                "SELECT invocation_spec_id FROM run_attempts WHERE run_id = ?",
                (started["run"]["id"],),
            ).fetchone()
        assert len(plans) == 1
        assert len(specs) == 1
        assert specs[0]["protocol_id"] == "memory"
        assert specs[0]["adapter_id"] == "fake.deterministic"
        assert specs[0]["status"] == "completed"
        assert attempt["invocation_spec_id"] == specs[0]["id"]

    asyncio.run(scenario())


def test_kimi_native_search_final_response_bypasses_host_tool_scope(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        repository = CoreRepository(tmp_path / "kimi-native-final.db")
        conversation = repository.create_conversation("standard_chat", "Search", "research")
        emitted: list[dict] = []

        async def sink(event):
            emitted.append(event)

        class Provider:
            def __init__(self):
                self.round = 0

            async def stream_events(self, _model_id, _messages, **_kwargs):
                self.round += 1
                if self.round == 1:
                    yield ProviderEvent(
                        "native_search_call",
                        tool_call=ToolCall("search-1", "native.web_search", {
                            "query": "latest anime",
                            "results": [{
                                "title": "Official source",
                                "url": "https://example.test/anime",
                            }],
                        }),
                    )
                else:
                    yield ProviderEvent("text_delta", text="Search completed.")

        provider = Provider()

        def create_provider_for_test(profile):
            provider.profile = profile
            return provider

        monkeypatch.setattr("agent.runtime.create_provider", create_provider_for_test)
        service = ChatRunService(repository, sink)
        service.configure_provider({
            "id": "kimi", "name": "Kimi", "kind": "openai_compatible",
            "base_url": "https://api.moonshot.cn/v1", "api_key": "secret",
            "allowed_model_ids": ["kimi-k3"], "automatic_model_ids": ["kimi-k3"],
            "model_capabilities": {"kimi-k3": {
                "text": "yes", "tools": "yes",
                "input_cost_per_million": "1", "output_cost_per_million": "2",
            }},
            "model_tool_modes": {"kimi-k3": "native"},
            "primary_protocol": "openai_chat",
            "native_search": {
                "adapter_id": "kimi_builtin_web_search", "protocol": "openai_chat",
                "endpoint_base_url": "https://api.moonshot.cn/v1",
            },
            "model_native_web_search": {"kimi-k3": "verified"},
            "invocation_routes": [{
                "route_id": "kimi.primary", "route_role": "primary",
                "protocol": "openai_chat", "adapter_id": "kimi.openai_chat",
                "endpoint_base_url": "https://api.moonshot.cn/v1",
                "source": "official_preset", "manifest_version": "v3.2",
                "config_fingerprint": "kimi-primary", "source_provenance": "none",
            }, {
                "route_id": "kimi.openai_chat", "route_role": "native_search",
                "protocol": "openai_chat", "adapter_id": "kimi.openai_chat",
                    "endpoint_base_url": "https://api.moonshot.cn/v1",
                    "source": "official_preset", "manifest_version": "v3.1.1",
                    "config_fingerprint": "kimi-search", "source_provenance": "structured",
            }],
            "model_route_capabilities": {"kimi-k3": [{
                "route_id": "kimi.primary",
                "capability_signature": ["text"],
                "status": "verified", "route_config_fingerprint": "kimi-primary",
            }, {
                "route_id": "kimi.openai_chat",
                "capability_signature": ["native_web_search", "text"],
                "status": "verified", "route_config_fingerprint": "kimi-search",
            }]},
        })
        repository.update_conversation_routing(
            conversation["id"], "fixed", "balanced", "kimi", "kimi-k3",
        )
        started = await service.start({
            "conversation_id": conversation["id"], "content": "Search latest anime",
            "idempotency_key": "kimi-native-final", "web_search_global_enabled": True,
            "web_search_request_mode": "force_once",
            "selection": {
                "mode": "fixed", "provider_profile_id": "kimi",
                "model_id": "kimi-k3", "source": "conversation_override",
            },
        })
        await service.tasks[started["run"]["id"]]

        run = repository.get_model_run(started["run"]["id"])
        assert run["status"] == "completed", (
            run["error_code"], run["error_message"], emitted,
        )
        assert run["error_code"] is None
        assert provider.round == 3
        operations = repository.list_web_operations(run["id"])
        assert len(operations) == 1
        assert operations[0]["status"] == "completed"
        assert operations[0]["result_count"] == 1
        assert not any(
            event["event_type"] == "run.failed"
            and event["data"].get("error_code") == "tool_not_allowed"
            for event in emitted
        )

    asyncio.run(scenario())


def test_ungrounded_fallback_cannot_claim_fresh_search_evidence() -> None:
    unsafe = ChatRunService._safe_ungrounded_fallback(
        "根据搜索结果，最新番剧包括……", "搜索最新番剧",
    )
    safe = ChatRunService._safe_ungrounded_fallback(
        "番剧通常按季度播出。", "搜索最新番剧",
    )
    assert "根据搜索结果" not in unsafe
    assert "无法安全提供或确认最新事实" in unsafe
    assert safe.startswith("本次联网请求没有返回可核验来源")
    assert "番剧通常按季度播出" in safe


def test_internal_source_ids_become_links_or_are_hidden() -> None:
    known = "de8bfde6-f1db-4bf7-ab5a-f44aaef8ec46"
    unknown = "641272c8-7e31-43c6-9a3d-76f5d4954c3e"
    content = (
        f"梗来自方言空耳。（来源：{known}、{unknown}）\n\n"
        "原有链接保持不变：[背景资料](https://example.org/background)"
    )

    rendered = ChatRunService._sanitize_public_source_references(content, [{
        "id": known,
        "title": "方言空耳背景",
        "url": "https://example.com/source",
    }])

    assert rendered == (
        "梗来自方言空耳。（来源：[方言空耳背景](https://example.com/source)）\n\n"
        "原有链接保持不变：[背景资料](https://example.org/background)"
    )
    assert known not in rendered
    assert unknown not in rendered
