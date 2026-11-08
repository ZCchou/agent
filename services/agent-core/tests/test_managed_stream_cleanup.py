import asyncio
from dataclasses import replace

import pytest

from agent.local_provider import ManagedLocalProvider
from agent.providers import ProviderEvent
from test_managed_provider import Inner, Sessions, profile


@pytest.mark.parametrize("method", ["stream", "stream_events"])
def test_closing_stream_finishes_transport_before_releasing_lease(method):
    async def scenario():
        sessions = Sessions()
        cleanup_started = asyncio.Event()
        cleanup_allowed = asyncio.Event()
        active = 0

        class Transport(Inner):
            async def stream(self, model, messages):
                nonlocal active
                active += 1
                try:
                    yield "answer"
                finally:
                    cleanup_started.set()
                    await cleanup_allowed.wait()
                    active -= 1

            async def stream_events(self, model, messages, *, tools, options=None):
                async for text in self.stream(model, messages):
                    yield ProviderEvent("text_delta", text=text)

        # Use each transport entry point directly so a nested test generator
        # does not itself need the production closing contract.
        transport = Transport()
        if method == "stream_events":
            transport.stream_events = lambda *args, **kwargs: transport.stream(*args)
        provider = ManagedLocalProvider(profile(), transport, sessions)
        kwargs = {"tools": False} if method == "stream_events" else {}
        first = getattr(provider, method)("model", [], **kwargs)
        await anext(first)
        try:
            closing = asyncio.create_task(first.aclose())
            await asyncio.sleep(0)
            assert sessions.lock.locked()
            await asyncio.wait_for(cleanup_started.wait(), 1)
            second = getattr(provider, method)("model", [], **kwargs)
            entering = asyncio.create_task(anext(second))
            closing.cancel()
            await asyncio.sleep(0)
            closing.cancel()
            await asyncio.sleep(0)
            assert not entering.done()
            assert active == 1
            cleanup_allowed.set()
            with pytest.raises(asyncio.CancelledError):
                await closing
            await entering
            assert active == 1
            await second.aclose()
            assert active == 0
            assert sessions.active == 0
        finally:
            cleanup_allowed.set()

    asyncio.run(scenario())


@pytest.mark.parametrize("prepared", [False, True])
def test_hidden_invocation_event_error_closes_stream(prepared):
    from agent.execution.compiler import InvocationCompiler
    from agent.model_invocation import ModelInvocationService
    from agent.providers import GenerationOptions
    from test_unified_invocation_v3 import model_spec

    async def scenario():
        sessions = Sessions()
        sessions.current = replace(sessions.current, primary_protocol="openai_responses")
        closed = False

        class Transport(Inner):
            async def stream_events(self, *args, **kwargs):
                nonlocal closed
                try:
                    yield ProviderEvent("text_delta", text=object())
                finally:
                    closed = True

        provider = ManagedLocalProvider(sessions.current, Transport(), sessions)
        invocation = ModelInvocationService()
        with pytest.raises(TypeError):
            if prepared:
                spec = model_spec()
                request = InvocationCompiler().compile_model(spec, [{"role": "user", "content": "hello"}])
                await invocation.complete_prepared(provider, spec, request)
            else:
                await invocation.complete_with_events(provider, "model", [], options=GenerationOptions())
        assert closed
        assert not sessions.lock.locked()

    asyncio.run(scenario())


@pytest.mark.parametrize("ending", ["complete", "close", "cancel"])
@pytest.mark.parametrize("protocol", ["openai_chat", "anthropic_messages"])
def test_managed_real_httpx_stream_closes_socket(protocol, ending):
    import json
    from agent.providers import create_provider

    async def scenario():
        disconnected = asyncio.Event()
        handler_tasks = set()

        async def serve(reader, writer):
            handler_tasks.add(asyncio.current_task())
            try:
                headers = await reader.readuntil(b"\r\n\r\n")
                length = next(int(line.split(b":", 1)[1]) for line in headers.split(b"\r\n")
                              if line.lower().startswith(b"content-length:"))
                await reader.readexactly(length)
                if protocol == "anthropic_messages":
                    event = {"type": "content_block_delta", "index": 0,
                             "delta": {"type": "text_delta", "text": "answer"}}
                    finish = {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}
                else:
                    event = {"choices": [{"delta": {"content": "answer"}, "finish_reason": None}]}
                    finish = {"choices": [{"delta": {}, "finish_reason": "stop"}]}
                frames = [event, finish] if ending == "complete" else [event]
                body = "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames).encode()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n")
                writer.write(f"{len(body):x}\r\n".encode() + body + b"\r\n")
                if ending == "complete":
                    writer.write(b"0\r\n\r\n")
                await writer.drain()
                await reader.read()
                disconnected.set()
            finally:
                writer.close()
                await writer.wait_closed()
                handler_tasks.discard(asyncio.current_task())

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        sessions = Sessions()
        sessions.current = replace(profile(), base_url=f"http://127.0.0.1:{port}/v1", primary_protocol=protocol)
        provider = ManagedLocalProvider(sessions.current, create_provider(sessions.current), sessions)
        stream = provider.stream_events("model", [], tools=False)
        try:
            if ending == "complete":
                events = [event async for event in stream]
                assert "".join(event.text for event in events) == "answer"
            else:
                while (await anext(stream)).kind != "text_delta":
                    pass
                if ending == "close":
                    await stream.aclose()
                else:
                    reading = asyncio.create_task(anext(stream))
                    await asyncio.sleep(0)
                    reading.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await reading
            assert sessions.active == 0
            await asyncio.wait_for(disconnected.wait(), 2)
        finally:
            await stream.aclose()
            server.close()
            await server.wait_closed()
            if handler_tasks:
                await asyncio.gather(*handler_tasks)

    asyncio.run(scenario())


@pytest.mark.parametrize("ending", ["error", "cancel"])
def test_runtime_event_consumer_closes_managed_stream(tmp_path, monkeypatch, ending):
    from contextlib import asynccontextmanager
    from agent.runtime import ChatRunService
    from agent.storage import CoreDataError, CoreRepository

    async def scenario():
        lock = asyncio.Lock()
        handling = asyncio.Event()
        closed = False

        class Transport(Inner):
            async def stream_events(self, *args, **kwargs):
                nonlocal closed
                try:
                    yield ProviderEvent("text_delta", text="answer")
                    await asyncio.Event().wait()
                finally:
                    closed = True

        @asynccontextmanager
        async def invocation(identity):
            async with lock:
                yield current_profile

        class Manager:
            pass

        manager = Manager()
        manager.invocation = invocation

        def provider_for_profile(value):
            nonlocal current_profile
            current_profile = value
            return ManagedLocalProvider(value, Transport(), manager)

        current_profile = None
        monkeypatch.setattr("agent.runtime.create_provider", provider_for_profile)
        repository = CoreRepository(tmp_path / "runtime.db")

        async def sink(event):
            if event["event_type"] == "run.chunk":
                handling.set()
                if ending == "error":
                    raise CoreDataError("consumer_failed", "event consumer failed")
                await asyncio.Event().wait()

        service = ChatRunService(repository, sink)
        service.configure_provider({
            "id": "manual", "name": "Local", "kind": "openai_compatible",
            "base_url": "http://127.0.0.1:8080/v1", "api_key": "secret",
            "allowed_model_ids": ["model"], "model_tool_modes": {"model": "text_only"},
        })
        conversation = repository.create_conversation("standard_chat", "Chat", "casual")
        repository.update_conversation_routing(conversation["id"], "fixed", "balanced", "manual", "model")
        try:
            started = await service.start({
                "conversation_id": conversation["id"], "content": "hello",
                "idempotency_key": "cleanup", "provider_profile_id": "manual", "model_id": "model",
            })
            task = service.tasks[started["run"]["id"]]
            await asyncio.wait_for(handling.wait(), 5)
            if ending == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                await task
            assert closed
            assert not lock.locked()
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_repeated_cancellation_during_inner_read_keeps_lease_until_cleanup():
    async def scenario():
        sessions = Sessions()
        started = asyncio.Event()
        cleaning = asyncio.Event()
        release = asyncio.Event()

        class Transport(Inner):
            async def stream_events(self, *args, **kwargs):
                try:
                    started.set()
                    await asyncio.Event().wait()
                    yield ProviderEvent("text_delta", text="unused")
                finally:
                    cleaning.set()
                    await release.wait()

        provider = ManagedLocalProvider(profile(), Transport(), sessions)
        stream = provider.stream_events("model", [], tools=False)
        task = asyncio.create_task(anext(stream))
        await started.wait()
        try:
            task.cancel()
            await cleaning.wait()
            task.cancel()
            await asyncio.sleep(0)
            assert sessions.lock.locked()
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not sessions.lock.locked()
        finally:
            release.set()

    asyncio.run(scenario())


def test_native_tool_probe_closes_stream_before_return(tmp_path, monkeypatch):
    from agent.runtime import ChatRunService
    from agent.storage import CoreRepository
    from agent.tool_contracts import ToolCall

    async def scenario():
        async def sink(event):
            pass

        service = ChatRunService(CoreRepository(tmp_path / "agent.db"), sink)
        local = replace(profile(), id="manual")
        sessions = Sessions()
        closed = False

        class Transport(Inner):
            async def stream_events(self, *args, **kwargs):
                nonlocal closed
                try:
                    yield ProviderEvent("tool_call", tool_call=ToolCall("call", "fs.list", {"path": "."}))
                finally:
                    closed = True

        provider = ManagedLocalProvider(profile(), Transport(), sessions)
        monkeypatch.setattr(service, "_profile", lambda identity: local)
        monkeypatch.setattr(service, "_provider", lambda value: provider)
        try:
            assert await service.probe_tool_capability("manual", "model") == {"tool_mode": "native", "verified": True}
            assert closed
            assert not sessions.lock.locked()
        finally:
            await service.shutdown()

    asyncio.run(scenario())
