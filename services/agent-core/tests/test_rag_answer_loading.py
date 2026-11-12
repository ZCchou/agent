import asyncio
from types import SimpleNamespace

import pytest

from agent.execution.run_recovery import safe_code, failure_notice
from agent.local_models.service import LocalModelService
from agent.storage import CoreDataError


@pytest.mark.parametrize("code", ["rag_answer_invalid", "rag_citation_invalid", "rag_quote_invalid", "rag_evidence_missing", "document_template_required", "document_target_ambiguous", "document_publish_failed", "terminal_isolation_unavailable", "rag_library_required"])
def test_rag_error_notice_keeps_real_code(code):
    assert safe_code(CoreDataError(code, "failed")) == code
    assert "internal_error" not in failure_notice(code, [], terminal=True)


def test_prepare_model_coalesces_load_and_releases_only_last_waiter():
    async def exercise():
        service = LocalModelService.__new__(LocalModelService)
        service._closed = False
        service._run_pins = {}
        service._preparations = {}
        service._operation = None
        service._auto_load_job = None
        service._runtime = None
        service.catalog = SimpleNamespace(get=lambda model_id: {"id": model_id}, jobs=lambda: [{"id": "job", "model_id": "m", "status": "running"}])
        calls = []
        def schedule(kind, **kwargs):
            service._operation = kind
            calls.append(kwargs["model_id"])
            return {"job": {"id": "job", "status": "queued"}}
        service._schedule = schedule
        service._request_cancel = lambda job: calls.append("cancel")
        first = await service.handle("runtime.prepare", {"model_id": "m"})
        second = await service.handle("runtime.prepare", {"model_id": "m"})
        assert first["job"]["id"] == second["job"]["id"]
        assert calls == ["m"]
        await service.handle("runtime.release", {"preparation_id": first["preparation_id"]})
        assert calls == ["m"]
        await service.handle("runtime.release", {"preparation_id": second["preparation_id"]})
        assert calls == ["m", "cancel"]
    asyncio.run(exercise())


def test_rag_schema_limits_ids_to_admitted_evidence():
    from agent.rag.grounding import RagGrounding
    grounding = RagGrounding(None)
    grounding.runs["r"] = {"admitted": {"allowed": "text"}, "evidence": {"allowed": {}, "excluded": {}}}
    schema = grounding.answer_schema("r")
    citation = schema["properties"]["claims"]["items"]["properties"]["evidence"]["items"]
    assert citation["properties"]["id"]["enum"] == ["allowed"]
    assert schema["additionalProperties"] is False


def test_invocation_schema_changes_request_identity():
    from agent.execution.domain import InvocationSpec
    import dataclasses
    spec = InvocationSpec(spec_id="s", plan_id="p", step_id="step", round_no=1, attempt_no=1,
        kind="model", provider_or_backend_id="provider", model_id="m", route_id="route",
        protocol_id="openai_chat_completions", adapter_id="openai.chat", endpoint_role="final_response",
        route_fingerprint="fp", capability_signature=("text",))
    schema_spec = dataclasses.replace(spec, response_format="json_schema", response_schema_json='{"type":"object"}', spec_hash="")
    assert schema_spec.spec_hash != spec.spec_hash
    assert schema_spec.hash_payload()["generation"]["response_schema"] == {"type": "object"}


def test_releasing_other_model_does_not_cancel_current_load():
    service = LocalModelService.__new__(LocalModelService)
    service._preparations = {"old": ("a", float("inf"))}
    service._run_pins = {}
    service._auto_load_job = "loading-b"
    service.catalog = SimpleNamespace(jobs=lambda: [{"id": "loading-b", "model_id": "b", "status": "running"}])
    cancelled = []
    service._request_cancel = cancelled.append
    service.release_preparation("old")
    assert cancelled == []


def test_selecting_registered_cold_model_does_not_load_it(tmp_path):
    from test_tool_continuation_retry import fixture, configuration
    from test_local_models_runtime import imported
    async def exercise():
        async def sink(event):
            pass
        core, project, conversation, service = await fixture(tmp_path, sink, configuration())
        try:
            model = await imported(service.local_models, tmp_path / "model.gguf")
            result = service.update_conversation_routing(conversation["id"], "fixed", "balanced", model["provider_profile_id"], model["id"])
            assert result["model_id"] == model["id"]
            assert service.local_models._runtime is None
        finally:
            await service.shutdown()
    asyncio.run(exercise())


def test_cancelled_load_is_terminal_for_existing_waiter():
    service = LocalModelService.__new__(LocalModelService)
    service._preparations = {"lease": ("m", float("inf"))}
    service._run_pins = {}
    service._auto_load_job = "job"
    service._operation = None
    service._runtime = None
    service.catalog = SimpleNamespace(get=lambda identity: {}, jobs=lambda: [{"id": "job", "model_id": "m", "status": "cancelled"}])
    scheduled = []
    service._schedule = lambda *args, **kwargs: scheduled.append(args)
    result = service.prepare("m", "lease")
    assert result["state"] == "failed"
    assert result["job"]["status"] == "cancelled"
    assert scheduled == []


def test_job_fixture_waits_for_process_startup_under_load():
    import time
    from test_rag_service import wait_job
    async def exercise():
        started = time.monotonic()
        class SlowJob:
            async def handle(self, action, params):
                status = "completed" if time.monotonic()-started > 2.5 else "running"
                return {"jobs": [{"id": "delayed", "status": status}]}
        result = await wait_job(SlowJob(), {"job": {"id": "delayed"}})
        assert result["status"] == "completed"
    asyncio.run(exercise())
