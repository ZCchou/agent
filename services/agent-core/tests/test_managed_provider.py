import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest

from agent.providers import ProviderError, ProviderEvent, ProviderProfile


def profile():
    return ProviderProfile("managed:model", "model", "openai_compatible", "http://127.0.0.1:8080/v1",
                           api_key="session-secret", local_strict=True, allowed_model_ids=("model",))


class Sessions:
    def __init__(self):
        self.current = profile()
        self.lock = asyncio.Lock()
        self.active = 0
        self.maximum = 0

    @asynccontextmanager
    async def invocation(self, identity):
        assert identity == "managed:model"
        async with self.lock:
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            try:
                yield self.current
            finally:
                self.active -= 1


class Inner:
    calls = 0

    async def stream_events(self, model, messages, *, tools, options=None):
        self.calls += 1
        await asyncio.sleep(.01)
        yield ProviderEvent("text_delta", text="local answer")

    async def list_models(self):
        return [{"id": "model"}]


def test_managed_provider_serializes_normal_streaming_entrypoint():
    from agent.local_provider import ManagedLocalProvider
    async def scenario():
        sessions = Sessions()
        inner = Inner()
        provider = ManagedLocalProvider(profile(), inner, sessions)
        async def stream():
            return [event.text async for event in provider.stream_events("model", [], tools=False)]
        assert await asyncio.gather(stream(), stream()) == [["local answer"], ["local answer"]]
        assert sessions.maximum == 1
        assert sessions.active == 0
        assert await provider.list_models() == [{"id": "model"}]
    asyncio.run(scenario())


def test_managed_provider_rejects_changed_session_before_sending_content():
    from agent.local_provider import ManagedLocalProvider
    async def scenario():
        sessions = Sessions()
        inner = Inner()
        provider = ManagedLocalProvider(profile(), inner, sessions)
        sessions.current = replace(sessions.current, api_key="restarted-session")
        with pytest.raises(ProviderError) as error:
            _ = [event async for event in provider.stream_events("model", [], tools=False)]
        assert error.value.code == "managed_session_changed"
        assert inner.calls == 0
        assert sessions.active == 0
    asyncio.run(scenario())


def test_managed_provider_cancellation_releases_invocation():
    from agent.local_provider import ManagedLocalProvider
    async def scenario():
        sessions = Sessions()
        entered = asyncio.Event()
        class Slow(Inner):
            async def stream_events(self, model, messages, *, tools, options=None):
                entered.set()
                await asyncio.sleep(60)
                yield ProviderEvent("text_delta", text="late")
        provider = ManagedLocalProvider(profile(), Slow(), sessions)
        async def stream():
            return [event async for event in provider.stream_events("model", [], tools=False)]
        task = asyncio.create_task(stream())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert sessions.active == 0
        assert not sessions.lock.locked()
    asyncio.run(scenario())


def test_managed_openai_transport_requests_and_retains_stream_token_usage(monkeypatch):
    import json
    import httpx
    from agent.providers import create_provider
    captured = []
    def respond(request):
        captured.append(json.loads(request.content))
        frames = [
            {"choices": [{"delta": {"content": "ready"}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 1}},
        ]
        stream = "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames) + "data: [DONE]\n\n"
        return httpx.Response(200, text=stream, headers={"content-type": "text/event-stream"})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    async def scenario():
        events = [event async for event in create_provider(profile()).stream_events("model", [], tools=False)]
        assert captured[0].get("stream_options") == {"include_usage": True}
        assert next(event.usage for event in events if event.kind == "usage")["output_tokens"] == 1
    asyncio.run(scenario())
