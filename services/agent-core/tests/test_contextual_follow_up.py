import asyncio
from pathlib import Path

from agent.providers import ProviderError, ProviderEvent
from agent.runtime import ChatRunService
from agent.storage import CoreRepository


def test_completed_offer_is_persisted_as_pending_action(
    tmp_path: Path, monkeypatch,
) -> None:
    async def scenario() -> None:
        repository = CoreRepository(tmp_path / "offer-runtime.db")
        conversation = repository.create_conversation("standard_chat", "Chat", "casual")

        class Provider:
            def __init__(self, profile):
                self.profile = profile

            async def stream_events(self, _model_id, _messages, **_kwargs):
                yield ProviderEvent(
                    "text_delta",
                    text="要不要我这轮实际搜一次「西安事变纪念馆官网」，搜完再给你准话？",
                )

        monkeypatch.setattr("agent.runtime.create_provider", Provider)
        events = []

        async def sink(event):
            events.append(event)

        service = ChatRunService(repository, sink)
        started = await service.start({
            "conversation_id": conversation["id"],
            "content": "西安事变纪念馆官网是什么？",
            "idempotency_key": "offer-runtime",
            "provider_profile_id": "builtin-demo",
            "model_id": "demo",
        })
        await service.tasks[started["run"]["id"]]

        conversation_id = started["conversation"]["id"]
        pending = repository.get_pending_follow_up_action(conversation_id)
        assert pending is not None
        assert pending["action_type"] == "web_search"
        assert pending["query_hint"] == "西安事变纪念馆官网是什么？"
        assert pending["source_run_id"] == started["run"]["id"]
        assert "run.intent_follow_up_pending" in [event["event_type"] for event in events]
        assert repository.get_model_run(started["run"]["id"])["assistant_message"]

    asyncio.run(scenario())


def test_confirmed_follow_up_is_consumed_and_keeps_original_request(
    tmp_path: Path, monkeypatch,
) -> None:
    async def scenario() -> None:
        repository = CoreRepository(tmp_path / "confirm-runtime.db")
        conversation = repository.create_conversation("standard_chat", "Chat", "casual")
        source = repository.start_model_run(
            content="西安事变纪念馆官网是什么？", idempotency_key="source",
            provider_profile_id="builtin-demo", model_id="demo",
            conversation_id=conversation["id"],
        )
        source_message = repository.get_conversation(conversation["id"])["messages"][0]
        pending = repository.create_pending_follow_up_action(
            conversation_id=conversation["id"], source_run_id=source["id"],
            source_message_id=source_message["id"], action_type="web_search",
            query_hint="西安事变纪念馆官网是什么？", purpose="核对当前信息并返回可核验来源",
            evidence="required",
        )
        captured: list[list[dict]] = []

        class Provider:
            def __init__(self, profile):
                self.profile = profile

            async def stream_events(self, _model_id, messages, **_kwargs):
                captured.append(messages)
                yield ProviderEvent("text_delta", text="已根据确认继续处理。")

        monkeypatch.setattr("agent.runtime.create_provider", Provider)
        async def sink(_event):
            return None

        service = ChatRunService(repository, sink)
        started = await service.start({
            "conversation_id": conversation["id"], "content": "好的",
            "idempotency_key": "confirm-runtime", "provider_profile_id": "builtin-demo",
            "model_id": "demo", "web_search_request_mode": "auto",
            "web_search_global_enabled": True,
        })
        resolved = started["execution_plan"]["resolved_intent"]
        assert resolved["reason_code"] == "confirmed_pending_web_search"
        assert resolved["pending_action_id"] == pending["id"]
        await service.tasks[started["run"]["id"]]
        assert repository.get_pending_follow_up_action(conversation["id"]) is None
        assert captured
        assert any("西安事变纪念馆官网是什么？" in str(message) for message in captured[0])

    asyncio.run(scenario())


def test_model_semantic_offer_is_persisted_when_wording_is_not_in_rules(
    tmp_path: Path, monkeypatch,
) -> None:
    async def scenario() -> None:
        repository = CoreRepository(tmp_path / "semantic-offer-runtime.db")
        conversation = repository.create_conversation("standard_chat", "Chat", "casual")
        calls = 0

        class Provider:
            def __init__(self, profile):
                self.profile = profile

            async def stream_events(self, _model_id, _messages, **_kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    yield ProviderEvent(
                        "text_delta", text="如果你愿意，我可以帮你确认这个梗的来源。",
                    )
                else:
                    yield ProviderEvent(
                        "text_delta",
                        text='{"action":"offer_confirmation","capability":"web_search",'
                        '"query_hint":"小狗叫是什么梗？","evidence":"required",'
                        '"confidence":0.96}',
                    )

        monkeypatch.setattr("agent.runtime.create_provider", Provider)
        async def sink(_event):
            return None

        service = ChatRunService(repository, sink)
        started = await service.start({
            "conversation_id": conversation["id"], "content": "小狗叫是什么梗？",
            "idempotency_key": "semantic-offer-runtime",
            "provider_profile_id": "builtin-demo", "model_id": "demo",
        })
        await service.tasks[started["run"]["id"]]
        pending = repository.get_pending_follow_up_action(conversation["id"])
        assert pending is not None
        assert pending["query_hint"] == "小狗叫是什么梗？"
        assert calls == 2

    asyncio.run(scenario())


def test_model_semantic_offer_retries_without_json_response_format(
    tmp_path: Path, monkeypatch,
) -> None:
    async def scenario() -> None:
        repository = CoreRepository(tmp_path / "semantic-offer-fallback.db")
        conversation = repository.create_conversation("standard_chat", "Chat", "casual")
        calls = 0

        class Provider:
            def __init__(self, profile):
                self.profile = profile

            async def stream_events(self, _model_id, _messages, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    yield ProviderEvent(
                        "text_delta", text="如果你想知道，我可以帮你确认更准确的来源。",
                    )
                elif calls == 2:
                    assert kwargs["options"].response_format == "json_object"
                    raise ProviderError(
                        "response_format_unsupported", "JSON output is not supported.",
                    )
                else:
                    assert kwargs["options"].response_format is None
                    yield ProviderEvent(
                        "text_delta",
                        text='{"action":"offer_confirmation","capability":"web_search",'
                        '"query_hint":"大狗叫又是什么","evidence":"required",'
                        '"confidence":0.91}',
                    )

        monkeypatch.setattr("agent.runtime.create_provider", Provider)
        service = ChatRunService(repository, lambda _event: asyncio.sleep(0))
        started = await service.start({
            "conversation_id": conversation["id"], "content": "大狗叫又是什么",
            "idempotency_key": "semantic-offer-format-fallback",
            "provider_profile_id": "builtin-demo", "model_id": "demo",
        })
        await service.tasks[started["run"]["id"]]
        pending = repository.get_pending_follow_up_action(conversation["id"])
        assert pending is not None
        assert pending["query_hint"] == "大狗叫又是什么"
        assert calls == 3

    asyncio.run(scenario())


def test_model_semantic_confirmation_triggers_pending_search(
    tmp_path: Path, monkeypatch,
) -> None:
    async def scenario() -> None:
        repository = CoreRepository(tmp_path / "semantic-confirm-runtime.db")
        conversation = repository.create_conversation("standard_chat", "Chat", "casual")
        source = repository.start_model_run(
            content="小狗叫是什么梗？", idempotency_key="semantic-source",
            provider_profile_id="builtin-demo", model_id="demo",
            conversation_id=conversation["id"],
        )
        source_message = repository.get_conversation(conversation["id"])["messages"][0]
        pending = repository.create_pending_follow_up_action(
            conversation_id=conversation["id"], source_run_id=source["id"],
            source_message_id=source_message["id"], action_type="web_search",
            query_hint="小狗叫是什么梗？", purpose="核对当前信息并返回可核验来源",
            evidence="required",
        )
        calls = 0

        class Provider:
            def __init__(self, profile):
                self.profile = profile

            async def stream_events(self, _model_id, _messages, **_kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    yield ProviderEvent(
                        "text_delta",
                        text='{"action":"confirm","capability":"web_search",'
                        '"evidence":"required","confidence":0.95}',
                    )
                else:
                    yield ProviderEvent("text_delta", text="已按你的确认继续处理。")

        monkeypatch.setattr("agent.runtime.create_provider", Provider)
        async def sink(_event):
            return None

        service = ChatRunService(repository, sink)
        started = await service.start({
            "conversation_id": conversation["id"], "content": "就按你说的查一下吧",
            "idempotency_key": "semantic-confirm-runtime",
            "provider_profile_id": "builtin-demo", "model_id": "demo",
            "web_search_request_mode": "auto",
            "web_search_global_enabled": True,
        })
        assert started["execution_plan"]["resolved_intent"]["reason_code"] == (
            "confirmed_pending_web_search"
        )
        assert started["execution_plan"]["resolved_intent"]["pending_action_id"] == pending["id"]
        await service.tasks[started["run"]["id"]]
        assert repository.get_pending_follow_up_action(conversation["id"]) is None
        assert calls == 2

    asyncio.run(scenario())


def test_short_contextual_retry_uses_model_intent_advisor(
    tmp_path: Path, monkeypatch,
) -> None:
    async def scenario() -> None:
        repository = CoreRepository(tmp_path / "contextual-retry-search.db")
        conversation = repository.create_conversation("standard_chat", "Chat", "casual")
        repository._insert_message(conversation["id"], "user", "大狗叫又是什么")
        repository._insert_message(
            conversation["id"], "assistant",
            "上一轮没有联网核实出处。",
        )
        calls: list[str] = []

        class Provider:
            async def stream_events(self, _model_id, messages, **kwargs):
                purpose = getattr(kwargs.get("options"), "purpose", "")
                calls.append(purpose)
                if purpose == "intent_analysis":
                    yield ProviderEvent(
                        "text_delta",
                        text=(
                            "```json\n"
                            '{"files":"available","tools":"available",'
                            '"web":"required","web_evidence":"required",'
                            '"reasoning":"standard","task_kinds":["research"],'
                            '"explanation_code":"contextual_retry"}'
                            "\n```"
                        ),
                    )
                    return
                yield ProviderEvent("text_delta", text="已联网核实。")

        provider = Provider()

        def create_provider_for_test(profile):
            provider.profile = profile
            return provider

        monkeypatch.setattr("agent.runtime.create_provider", create_provider_for_test)
        service = ChatRunService(repository, lambda _event: asyncio.sleep(0))
        repository.update_conversation_routing(
            conversation["id"], "automatic", "balanced", "builtin-demo", "demo",
        )
        started = await service.start({
            "conversation_id": conversation["id"], "content": "再试一次",
            "idempotency_key": "contextual-retry-search",
            "web_search_request_mode": "auto", "web_search_global_enabled": True,
            "selection": {"mode": "automatic", "routing_policy": {"preset": "balanced"}},
        })

        assert started["execution_plan"]["resolved_intent"]["advisor"]["web"] == "required"
        await service.tasks[started["run"]["id"]]
        assert "intent_analysis" in calls

    asyncio.run(scenario())


def test_preflight_intent_advisor_retries_invalid_json_without_response_format(
    tmp_path: Path, monkeypatch,
) -> None:
    async def scenario() -> None:
        repository = CoreRepository(tmp_path / "intent-advisor-json-retry.db")
        calls: list[str | None] = []

        class Provider:
            def __init__(self, profile):
                self.profile = profile

            async def stream_events(self, _model_id, _messages, **kwargs):
                response_format = kwargs["options"].response_format
                calls.append(response_format)
                if response_format == "json_object":
                    yield ProviderEvent("text_delta", text="我认为需要联网核实。")
                    return
                yield ProviderEvent(
                    "text_delta",
                    text=(
                        '{"files":"available","tools":"available",'
                        '"web":"required","web_evidence":"required",'
                        '"reasoning":"standard","task_kinds":["research"],'
                        '"explanation_code":"contextual_retry"}'
                    ),
                )

        monkeypatch.setattr("agent.runtime.create_provider", Provider)
        service = ChatRunService(repository, lambda _event: asyncio.sleep(0))
        intent, error = await service._preflight_intent_advisor(
            content="再试一次", attachments=[],
            profile=service.profiles["builtin-demo"], model_id="demo",
            conversation_kind="standard_chat",
            recent_messages=[{"role": "assistant", "content": "上一轮没有联网核实。"}],
        )

        assert error is None
        assert intent is not None and intent["web"] == "required"
        assert calls == ["json_object", None]

    asyncio.run(scenario())
