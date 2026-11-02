"""C4 review: asynchronous assembly must not retain stale dispatch authority."""
import asyncio
import json
import threading
from dataclasses import replace
from decimal import Decimal

import pytest

from agent.context_engine.domain import ContextEngineError
from agent.execution.domain import InvocationTarget, ModelCandidate
from agent.providers import ProviderEvent
from agent.runtime import ChatRunService
from agent.storage import CoreRepository
from test_attachment_references import seed


def configured_service(tmp_path, monkeypatch, sink, *, project=False):
    repo, old = seed(tmp_path, monkeypatch)
    with repo._connect() as connection:
        connection.execute("UPDATE message_attachments SET kind='image',media_type='image/png',width=20,height=20 WHERE id='a0'")
    project_id = None
    if project:
        project_id = repo.create_project("Offline project", "coding")["id"]
        with repo._connect() as connection:
            connection.execute("UPDATE conversations SET kind='project_work', project_id=?,category=NULL WHERE id=?", (project_id, old["conversation_id"]))
            connection.execute("UPDATE projects SET model_access_policy='allow_online',web_access_policy='allow_search' WHERE id=?", (project_id,))
    calls = []
    class Provider:
        def __init__(self, profile):
            self.profile = profile
        async def stream_events(self, model_id, messages, **kwargs):
            calls.append((model_id, messages))
            text = json.dumps(dict(files="none", tools="none", web="off", web_evidence="optional",
                reasoning="standard", task_kinds=["general"], explanation_code="offline")) \
                if getattr(kwargs.get("options"), "purpose", None) == "intent_analysis" else "Offline observation."
            yield ProviderEvent("text_delta", text=text)
    monkeypatch.setattr("agent.runtime.create_provider", Provider)
    service = ChatRunService(repo, sink)
    capabilities = {
        "general": {"text": "yes", "vision": "no", "vision_status": "unsupported", "context_length": 32768},
        "vision": {"text": "yes", "vision": "yes", "vision_status": "verified", "context_length": 32768},
    }
    route = dict(route_id="offline.main", route_role="primary", protocol="memory",
                 adapter_id="fake.deterministic", config_fingerprint="original", endpoint_base_url="https://offline.invalid")
    service.profiles["builtin-demo"] = replace(service.profiles["builtin-demo"],
        kind="openai_compatible", base_url="https://offline.invalid",
        allowed_model_ids=("general", "vision"), automatic_model_ids=("general", "vision"),
        model_capabilities=capabilities, invocation_routes=(route,),
        model_route_capabilities={model: (dict(route_id="offline.main", capability_signature=signature,
            status="verified", route_config_fingerprint="original"),)
            for model, signature in (("general", ["text"]), ("vision", ["text", "vision"]))})
    repo.update_conversation_routing(old["conversation_id"], "automatic", "balanced", "builtin-demo", "general")
    candidates = [ModelCandidate(provider_profile_id="builtin-demo", model_id=model,
        protocol_id="memory", location="online", tool_mode="text_only", capabilities=capabilities[model],
        estimated_input_cost_per_million=Decimal("0"), estimated_output_cost_per_million=Decimal("0"),
        invocation_target=InvocationTarget("builtin-demo", model, "offline.main", "primary", "memory",
            "fake.deterministic", signature, "v1", "original", capability_status="verified"))
        for model, signature in (("general", ("text",)), ("vision", ("text", "vision")))]
    monkeypatch.setattr(service, "_execution_candidates", lambda *args, **kwargs: candidates)
    return repo, old, service, calls, project_id


def revoke(service, repo, run, kind, project_id):
    profile = service.profiles["builtin-demo"]
    if kind == "provider":
        service.profiles[profile.id] = replace(profile, enabled=False)
    elif kind == "model":
        service.profiles[profile.id] = replace(profile, allowed_model_ids=())
    elif kind in {"route", "endpoint"}:
        route = dict(profile.invocation_routes[0])
        route["config_fingerprint" if kind == "route" else "endpoint_base_url"] = "changed" if kind == "route" else "https://changed.invalid"
        service.profiles[profile.id] = replace(profile, invocation_routes=(route,))
    else:
        # Deliberately leave generation unchanged: this probes the independent
        # authority gate, not the already-covered generation mismatch gate.
        with repo._connect() as connection:
            if kind == "project":
                connection.execute("UPDATE projects SET model_access_policy='local_only' WHERE id=?", (project_id,))
            elif kind == "scope":
                connection.execute("UPDATE projects SET model_scope_policy='restricted' WHERE id=?", (project_id,))
            elif kind == "web":
                connection.execute("UPDATE projects SET web_access_policy='disabled' WHERE id=?", (project_id,))
                connection.execute("UPDATE model_runs SET web_search_route='host_tool' WHERE id=?", (run["id"],))
            elif kind == "cancel":
                connection.execute("UPDATE model_runs SET status='cancelled' WHERE id=?", (run["id"],))


@pytest.mark.parametrize("phase", ["final_response", "vision_analysis"])
@pytest.mark.parametrize("kind", ["provider", "model", "route", "endpoint", "project", "scope", "web", "cancel"])
def test_authority_changed_during_worker_blocks_snapshot_binding_and_dispatch(tmp_path, monkeypatch, phase, kind):
    async def scenario():
        events = []
        async def sink(event):
            events.append(event)
        repo, old, service, calls, project_id = configured_service(tmp_path, monkeypatch, sink,
            project=kind in {"project", "scope", "web"})
        entered, release = threading.Event(), threading.Event()
        original = service.context._assemble_once
        def delayed(*args, **kwargs):
            packed = original(*args, **kwargs)
            if kwargs["serialization_identity"]["step_type"] == phase:
                entered.set()
                assert release.wait(5)
            return packed
        monkeypatch.setattr(service.context, "_assemble_once", delayed)
        result = await service.start(dict(conversation_id=old["conversation_id"], content="describe",
            idempotency_key="authority", attachment_references=[dict(attachment_id="a0")] if phase == "vision_analysis" else []))
        run = result["run"]
        calls.clear()  # Preflight advisory calls precede the assembly under test.
        task = service.run_tasks[run["id"]]
        try:
            async with asyncio.timeout(3):
                while not entered.is_set():
                    if task.done():
                        pytest.fail(str(repo.get_model_run(run["id"])))
                    await asyncio.sleep(.005)
            plan_before = service.execution_repository.get_for_run(run["id"])
            step_before = next(step for step in plan_before["steps"] if step["step_type"] == phase)
            revoke(service, repo, run, kind, project_id)
        finally:
            release.set()
        if kind == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
        current = repo.get_model_run(run["id"])
        assert calls == [], current
        assert service.context.context_repository.preview(run["id"]) is None
        assert not any(event["event_type"] == "run.invocation_dispatched" for event in events)
        step_after = next(step for step in service.execution_repository.get_for_run(run["id"])["steps"] if step["step_type"] == phase)
        assert step_after["input_hash"] == step_before["input_hash"]
        assert current["status"] in {"failed", "cancelled"}
        if kind != "cancel":
            assert current["error_code"] in {"execution_plan_stale", "invocation_route_stale"}
    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["final_response", "vision_analysis"])
def test_revocation_in_dispatch_event_stops_captured_provider(tmp_path, monkeypatch, phase):
    async def scenario():
        async def sink(event):
            if event["event_type"] == "run.invocation_dispatched":
                service.profiles["builtin-demo"] = replace(service.profiles["builtin-demo"], enabled=False)
        repo, old, service, calls, _ = configured_service(tmp_path, monkeypatch, sink)
        result = await service.start(dict(conversation_id=old["conversation_id"], content="describe",
            idempotency_key="event-authority", attachment_references=[dict(attachment_id="a0")] if phase == "vision_analysis" else []))
        await service.run_tasks[result["run"]["id"]]
        assert calls == []
        assert repo.get_model_run(result["run"]["id"])["error_code"] == "execution_plan_stale"
    asyncio.run(scenario())


def test_compaction_required_worker_error_rechecks_authority_before_started_event(tmp_path, monkeypatch):
    async def scenario():
        events = []
        async def sink(event):
            events.append(event)
        repo, old, service, calls, _ = configured_service(tmp_path, monkeypatch, sink)
        entered, release = threading.Event(), threading.Event()
        def delayed_error(*args, **kwargs):
            entered.set()
            assert release.wait(5)
            raise ContextEngineError("context_compaction_required", "required")
        monkeypatch.setattr(service.context, "_assemble_once", delayed_error)
        result = await service.start(dict(conversation_id=old["conversation_id"], content="describe", idempotency_key="compact"))
        task = service.run_tasks[result["run"]["id"]]
        try:
            async with asyncio.timeout(3):
                while not entered.is_set():
                    await asyncio.sleep(.005)
            revoke(service, repo, result["run"], "provider", None)
        finally:
            release.set()
        await task
        assert not any(event["event_type"] == "run.context_compaction_started" for event in events)
        assert service.context.context_repository.compaction_status(result["run"]["conversation_id"]) is None
        assert calls == []
        assert repo.get_model_run(result["run"]["id"])["error_code"] == "execution_plan_stale"
    asyncio.run(scenario())


def test_routing_advisor_receives_six_prior_messages_without_future_or_foreign_rows(tmp_path, monkeypatch):
    async def scenario():
        async def sink(event):
            pass
        repo, old, service, calls, _ = configured_service(tmp_path, monkeypatch, sink)
        for index in range(1, 13):
            repo._insert_message(old["conversation_id"], "user" if index % 2 else "assistant", f"prior {index}")
        repo._insert_message(old["conversation_id"], "system_event", "internal event")
        result = await service.start(dict(conversation_id=old["conversation_id"], content="describe", idempotency_key="advisor"))
        run = result["run"]
        task = service.run_tasks.pop(run["id"])
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        repo._insert_message(old["conversation_id"], "assistant", "FUTURE")
        other = repo.create_conversation("standard_chat", "Other", "casual")
        repo._insert_message(other["id"], "user", "FOREIGN")
        plan = service.execution_repository.get_for_run(run["id"])
        # Exercise the real persisted advisor endpoint with an isolated synthetic
        # step; no preflight classifier or provider stub supplies its history.
        with repo._connect() as connection:
            connection.execute("UPDATE execution_steps SET step_type='intent_analysis',phase='analysis' WHERE id=?", (plan["steps"][0]["id"],))
        plan = service.execution_repository.get_for_run(run["id"])
        calls.clear()
        def forbidden(*args):
            raise AssertionError("advisor loaded UI history")
        monkeypatch.setattr(repo, "get_conversation", forbidden)
        await service._execute_routing_intent(run, plan, plan["steps"][0])
        payload = json.loads(calls[0][1][-1]["content"])
        assert payload["recent_conversation"] == [
            {"role": "user", "content": "prior 7"}, {"role": "assistant", "content": "prior 8"},
            {"role": "user", "content": "prior 9"}, {"role": "assistant", "content": "prior 10"},
            {"role": "user", "content": "prior 11"}, {"role": "assistant", "content": "prior 12"},
        ]
        assert payload["request"] == "describe"
        assert "FUTURE" not in json.dumps(payload) and "FOREIGN" not in json.dumps(payload)
    asyncio.run(scenario())
