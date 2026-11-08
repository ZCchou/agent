"""Apply managed-process leases to the existing local HTTP provider transport."""
from contextlib import asynccontextmanager
import asyncio

from agent.providers import ModelProvider, ProviderError, ProviderProfile, closing_provider_stream


async def _next_managed_event(stream):
    # Shield the read so a second cancellation cannot interrupt its transport
    # finally block after the first cancellation has started cleanup.
    read = asyncio.ensure_future(anext(stream))
    try:
        return await asyncio.shield(read)
    except asyncio.CancelledError:
        read.cancel()
        while not read.done():
            try:
                await asyncio.shield(read)
            except asyncio.CancelledError:
                pass
        try:
            read.result()
        except BaseException:
            pass
        raise


class ManagedLocalProvider(ModelProvider):
    def __init__(self, profile: ProviderProfile, inner: ModelProvider, models):
        super().__init__(profile)
        self.inner = inner
        self.models = models

    @asynccontextmanager
    async def _session(self):
        async with self.models.invocation(self.profile.id) as current:
            if (current.base_url, current.api_key) != (self.profile.base_url, self.profile.api_key):
                raise ProviderError("managed_session_changed", "本地模型会话已变化，请重新提交当前请求。")
            yield self.inner

    async def list_models(self):
        async with self._session() as provider:
            return await provider.list_models()

    async def discover_models(self):
        async with self._session() as provider:
            if hasattr(provider, "discover_models"):
                return await provider.discover_models()
            return await provider.list_models(), self.profile.base_url

    async def stream(self, model_id, messages):
        async with self._session() as provider:
            async with closing_provider_stream(provider.stream(model_id, messages).__aiter__()) as stream:
                while True:
                    try:
                        text = await _next_managed_event(stream)
                    except StopAsyncIteration:
                        break
                    yield text

    async def stream_events(self, model_id, messages, *, tools, options=None):
        async with self._session() as provider:
            async with closing_provider_stream(
                provider.stream_events(model_id, messages, tools=tools, options=options).__aiter__(),
            ) as stream:
                while True:
                    try:
                        event = await _next_managed_event(stream)
                    except StopAsyncIteration:
                        break
                    yield event
