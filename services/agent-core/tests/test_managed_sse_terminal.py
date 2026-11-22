import asyncio
import json

import pytest

from agent.providers import OpenAICompatibleProvider, ProviderProfile, ProviderError


@pytest.mark.parametrize("order,reason", [("uf","stop"),("fu","stop"),("both","length"),("fu","length"),("fxu","stop"),("f","cancel"),("f","stop"),("f","length")])
def test_managed_text_stream_terminal_packets(order, reason, monkeypatch):
    monkeypatch.setattr('agent.providers.MANAGED_USAGE_TRAILER_SECONDS', .2, raising=False)
    async def test():
        hold, sent, drained = asyncio.Event(), asyncio.Event(), asyncio.Event()
        usage = {"prompt_tokens": 10415, "completion_tokens": 992}
        async def serve(reader, writer):
            try:
                headers = await reader.readuntil(b"\r\n\r\n")
                length = int(next(l.split(b":")[1] for l in headers.lower().splitlines() if l.startswith(b"content-length:")))
                await reader.readexactly(length)
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: keep-alive\r\n\r\n")
                frames = [{"choices":[{"delta":{"content":"visible"}}]}]
                end = {"choices":[{"delta":{},"finish_reason":"stop" if reason=="cancel" else reason}]}
                u = {"choices":[],"usage":usage}
                bad = {"choices":[{"delta":{"tool_calls":[{"index":-1}]}}]}
                frames.extend([{**end,"usage":usage}] if order=="both" else [{"f":end,"u":u,"x":bad}[k] for k in order])
                for frame in frames:
                    writer.write(b"data: "+json.dumps(frame).encode()+b"\n\n")
                    await writer.drain()
                sent.set()
                await hold.wait()
            finally:
                writer.close()
                await writer.wait_closed()
                drained.set()
        server=await asyncio.start_server(serve,"127.0.0.1",0)
        profile=ProviderProfile("managed:test","Local","openai_compatible",f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1",local_strict=True,primary_protocol="openai_chat")
        provider=OpenAICompatibleProvider(profile)
        events=[]
        async def consume():
            async for event in provider.stream_events("local",[{"role":"user","content":"test"}],tools=False):
                events.append(event)
        task=asyncio.create_task(consume())
        try:
            await asyncio.wait_for(sent.wait(),3)
            if reason=="cancel":
                await asyncio.sleep(.02)
                assert not task.done()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                return
            if order=="fxu" or reason=="length":
                with pytest.raises(ProviderError) as exc:
                    await asyncio.wait_for(task,1)
                assert exc.value.code==("model_tool_protocol_failed" if order=="fxu" else "provider_response_incomplete")
            else:
                await asyncio.wait_for(task,1)
            if order!="fxu":
                assert [e.usage["output_tokens"] for e in events if e.kind=="usage"]==([992] if 'u' in order or order=='both' else [])
                assert [e.finish_reason for e in events if e.kind=="finish"]==[reason]
            assert not any(e.kind=="tool_call" for e in events)
        finally:
            hold.set()
            server.close()
            await server.wait_closed()
            await asyncio.wait_for(drained.wait(),2)
    asyncio.run(test())
