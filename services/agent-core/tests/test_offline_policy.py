import asyncio
from dataclasses import replace

import httpx
import pytest

from agent.providers import ProviderProfile
from agent.runtime import ChatRunService
from agent.storage import CoreDataError, CoreRepository


@pytest.mark.parametrize("endpoint", ["https://cloud.invalid/v1", "http://0.0.0.0:8080/v1",
    "http://user:password@127.0.0.1:8080/v1", "http://127.0.0.1:8080/v1?redirect=cloud"])
def test_offline_profile_rejects_nonlocal_or_ambiguous_endpoints(endpoint):
    from agent.offline import local_profile
    with pytest.raises(CoreDataError) as error:
        local_profile(ProviderProfile("p", "local", "openai_compatible", endpoint))
    assert error.value.code == "offline_provider_forbidden"


def test_offline_profile_canonicalizes_localhost_and_checks_every_route():
    from agent.offline import local_profile
    profile = local_profile(ProviderProfile("p", "local", "openai_compatible", "http://localhost:8080/v1",
        invocation_routes=({"endpoint_base_url": "http://localhost:8080/v1"},)))
    assert profile.base_url == "http://127.0.0.1:8080/v1"
    assert profile.local_strict
    assert profile.invocation_routes[0]["endpoint_base_url"] == profile.base_url
    with pytest.raises(CoreDataError):
        local_profile(replace(profile, invocation_routes=({"endpoint_base_url": "https://cloud.invalid"},)))


def test_production_offline_service_blocks_legacy_remote_profiles(tmp_path):
    async def scenario():
        async def sink(event):
            pass
        service = ChatRunService(CoreRepository(tmp_path / "agent.db"), sink, offline=True)
        status = service.configure_provider({"id": "legacy", "name": "legacy", "kind": "openai_compatible",
            "base_url": "https://cloud.invalid/v1", "allowed_model_ids": ["model"]})
        assert status["offline_blocked"]
        with pytest.raises(CoreDataError):
            await service.list_models("legacy")
        await service.shutdown()
    asyncio.run(scenario())


def test_offline_discovery_disables_environment_proxy(tmp_path, monkeypatch):
    original = httpx.AsyncClient
    configurations = []
    def client(**kwargs):
        configurations.append(kwargs)
        return original(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"data": []})), **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", client)
    async def scenario():
        async def sink(event):
            pass
        service = ChatRunService(CoreRepository(tmp_path / "agent.db"), sink, offline=True)
        service.configure_provider({"id": "local", "name": "local", "kind": "openai_compatible",
            "base_url": "http://localhost:8080/v1", "allowed_model_ids": ["model"]})
        await service.list_models("local")
        assert configurations and all(item["trust_env"] is False for item in configurations)
        await service.shutdown()
    asyncio.run(scenario())


def test_offline_chat_forces_web_off_and_rejects_managed_profile_spoofing(tmp_path):
    async def scenario():
        async def sink(event):
            pass
        core = CoreRepository(tmp_path / "agent.db")
        service = ChatRunService(core, sink, offline=True)
        with pytest.raises(CoreDataError) as denied:
            service.configure_provider({"id": "managed:spoof", "name": "spoof", "kind": "fake", "base_url": "memory://fake"})
        assert denied.value.code == "managed_provider_reserved"
        result = await service.start({"kind": "standard_chat", "category": "casual", "content": "Explain a safety requirement", "idempotency_key": "offline",
            "provider_profile_id": "builtin-demo", "model_id": "demo", "web_search_global_enabled": True,
            "web_search_request_mode": "force_once"})
        await service.tasks[result["run"]["id"]]
        assert core.get_model_run(result["run"]["id"])["web_search_route"] == "none"
        await service.shutdown()
    asyncio.run(scenario())


def test_offline_protocol_rejects_direct_web_requests(tmp_path):
    from agent.protocol import PROTOCOL_VERSION, handle_async_message
    async def scenario():
        async def sink(event):
            pass
        service = ChatRunService(CoreRepository(tmp_path / "agent.db"), sink, offline=True)
        response = await handle_async_message({"v": PROTOCOL_VERSION, "id": "web", "type": "web.operation.prepare",
            "params": {}}, service)
        assert response["error"]["code"] == "offline_network_forbidden"
        await service.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("strict", [False, True])
def test_anthropic_transport_policy_survives_continuation(monkeypatch, strict):
    import json
    from agent.offline import local_profile
    from agent.providers import create_provider

    configurations = []
    requests = []
    original = httpx.AsyncClient

    def respond(request):
        requests.append(request)
        stop = "pause_turn" if len(requests) == 1 else "end_turn"
        frames = [
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "local"}},
            {"type": "message_delta", "delta": {"stop_reason": stop}},
        ]
        return httpx.Response(200, text="".join("data: " + json.dumps(frame) + "\n\n" for frame in frames))

    def client(**kwargs):
        configurations.append(kwargs)
        return original(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    configured = ProviderProfile("p", "local", "openai_compatible", "http://localhost:8080/v1",
                                 api_key="secret", primary_protocol="anthropic_messages")
    if strict:
        configured = local_profile(configured)

    async def scenario():
        events = [event async for event in create_provider(configured).stream_events("model", [], tools=False)]
        assert "".join(event.text for event in events) == "locallocal"
        assert len(configurations) == 2
        assert all(item.get("trust_env", True) is (not strict) for item in configurations)
        assert all(item["follow_redirects"] is False for item in configurations)
        assert all(request.headers["x-api-key"] == "secret" for request in requests)
        assert all(json.loads(request.content)["tools"] == [] for request in requests)

    asyncio.run(scenario())
