import asyncio
from dataclasses import replace

import pytest

from agent.execution.domain import InvocationSpec
from agent.storage import CoreDataError
from test_local_models_runtime import fake_runtime, imported
from test_local_models_catalog import service_at, finish


def spec(**values):
    base = dict(spec_id="s", plan_id="p", step_id="step", round_no=1, attempt_no=1,
        kind="model", provider_or_backend_id="managed:m", model_id="m", route_id="route",
        protocol_id="openai_chat", adapter_id="openai.chat", endpoint_role="final_response",
        route_fingerprint="fp", capability_signature=("text",), activity_timeout_ms=60000)
    return InvocationSpec(**{**base, **values})


def test_first_output_budget_is_separate_and_does_not_reset_on_heartbeats():
    request = spec(first_output_timeout_ms=300000)
    assert request.stream_wait_seconds(False, 120) == 180
    assert request.stream_wait_seconds(False, 301) == 0
    assert request.stream_wait_seconds(True, 120) == 60
    assert request.hash_payload()["limits"]["first_output_timeout_ms"] == 300000
    assert replace(request, first_output_timeout_ms=120000, spec_hash="").spec_hash != request.spec_hash
    assert spec().stream_wait_seconds(False, 120) == 60


def test_save_and_reload_can_switch_resident_model_to_auto_gpu(tmp_path):
    async def exercise():
        service = service_at(tmp_path, runtime_root=fake_runtime(tmp_path / "runtime", modes={"cpu":"normal", "cuda":"gpu"}))
        try:
            model = await imported(service, tmp_path / "model.gguf")
            await service.handle("model.configure", {"model_id":model["id"], "device":"cpu"})
            assert (await finish(service, await service.handle("runtime.load", {"model_id":model["id"]})))["status"] == "completed"
            previous = service._runtime.session_id
            response = await service.handle("model.configure_load", {"model_id":model["id"], "device":"auto"})
            assert (await finish(service, response))["status"] == "completed"
            assert service.catalog.get(model["id"])["device"] == "auto"
            assert service._runtime.backend == "cuda"
            assert service._runtime.session_id != previous
        finally:
            await service.close()
    asyncio.run(exercise())


def test_invalid_reload_configuration_keeps_resident_session(tmp_path):
    async def exercise():
        service = service_at(tmp_path, runtime_root=fake_runtime(tmp_path / "runtime"))
        try:
            model = await imported(service, tmp_path / "model.gguf")
            await finish(service, await service.handle("runtime.load", {"model_id":model["id"]}))
            previous = service._runtime.session_id
            with pytest.raises(CoreDataError) as error:
                await service.handle("model.configure_load", {"model_id":model["id"], "device":"invalid"})
            assert error.value.code == "invalid_device"
            assert service._runtime.session_id == previous
        finally:
            await service.close()
    asyncio.run(exercise())


def test_local_streaming_ignores_legacy_total_deadline(tmp_path):
    from agent.providers import FakeModelProvider, ProviderEvent
    from agent.rag.service import RagService
    from agent.rag.grounding import RagGrounding
    from test_tool_continuation_retry import fixture, configuration
    from test_rag_service import Engines, ready, wait_job
    async def exercise():
        async def sink(event): pass
        config=configuration()
        config["base_url"]="http://127.0.0.1:12345/v1"
        config["model_tool_modes"]["model"]="text_only"
        core, project, conversation, service=await fixture(tmp_path,sink,config)
        await service.rag.close()
        service.rag=RagService(core,Engines())
        service.rag_grounding=RagGrounding(service.rag)
        service.context.rag_context=service.rag_grounding.evidence
        exceeded_legacy_deadline = asyncio.Event()
        class ActiveProvider:
            def __init__(self, profile): self.profile=profile
            async def stream_events(self, *args, **kwargs):
                if kwargs.get('options') and kwargs['options'].purpose == 'intent_analysis':
                    async for event in FakeModelProvider(self.profile).stream_events(*args, **kwargs):
                        yield event
                    return
                output_count = 0
                while True:
                    await asyncio.sleep(.005)
                    yield ProviderEvent("text_delta",text="x")
                    output_count += 1
                    if output_count == 12:
                        exceeded_legacy_deadline.set()
        service._provider=lambda profile: ActiveProvider(profile)
        original=service._create_model_invocation_spec
        service._create_model_invocation_spec=lambda *args,**kwargs: replace(original(*args,**kwargs),total_timeout_ms=30,activity_timeout_ms=1000,first_output_timeout_ms=None,spec_hash="")
        try:
            library=(await service.rag.handle("library.create",{"name":"rules","embedding_model_id":"embed","reranker_model_id":"rank"}))["library"]["id"]
            await service.rag.handle("library.bind",{"library_id":library,"project_id":project["id"],"enabled":True})
            await ready(service.rag,library)
            assert (await wait_job(service.rag,await service.rag.handle("index.build",{"library_id":library})))["status"]=="completed"
            response=await asyncio.wait_for(service.start({"conversation_id":conversation["id"],"content":"失效要求","idempotency_key":"total-deadline"}),15)
            await asyncio.wait_for(exceeded_legacy_deadline.wait(),15)
            run=core.get_model_run(response["run"]["id"])
            assert run["status"] == "generating"
            assert run["error_code"] is None
            assert not service.tasks[run["id"]].done()
            with core._connect() as connection:
                invocations=connection.execute("SELECT status,error_code FROM invocation_specs WHERE plan_id=?",(run["execution_plan_id"],)).fetchall()
            assert invocations
            assert all(row["status"] == "dispatched" and row["error_code"] is None for row in invocations)
            await asyncio.wait_for(service.cancel(run["id"]),5)
            assert core.get_model_run(run["id"])["status"] == "cancelled"
        finally:
            await asyncio.wait_for(service.shutdown(),5)
    asyncio.run(exercise())


def test_managed_queue_does_not_consume_first_output_or_total_budget(tmp_path):
    from agent.local_provider import ManagedLocalProvider
    from agent.model_invocation import ModelInvocationService
    from agent.execution.compiler import InvocationCompiler
    from agent.providers import ProviderEvent
    async def exercise():
        service=service_at(tmp_path,runtime_root=fake_runtime(tmp_path/"runtime"))
        pending=None
        try:
            model=await imported(service,tmp_path/"model.gguf")
            await finish(service,await service.handle("runtime.load",{"model_id":model["id"]}))
            profile=service.get_profile(model["provider_profile_id"])
            class QuickProvider:
                def __init__(self): self.profile=profile
                async def stream_events(self,*args,**kwargs):
                    yield ProviderEvent("text_delta",text="ready")
                    yield ProviderEvent("finish",finish_reason="stop")
            provider=ManagedLocalProvider(profile,QuickProvider(),service)
            request=spec(provider_or_backend_id=profile.id,model_id=model["id"],first_output_timeout_ms=20,total_timeout_ms=100)
            prepared=InvocationCompiler().compile_model(request,[{"role":"user","content":"hello"}])
            async with service._permit:
                pending=asyncio.create_task(ModelInvocationService().complete_prepared(provider,request,prepared))
                await asyncio.sleep(.15)
                assert not pending.done(), "waiting for the model slot must not use inference timeout"
            assert (await pending)[0]=="ready"
        finally:
            if pending and not pending.done():
                pending.cancel()
                await asyncio.gather(pending,return_exceptions=True)
            elif pending:
                pending.exception()
            await service.close()
    asyncio.run(exercise())
