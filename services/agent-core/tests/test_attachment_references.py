import asyncio
import hashlib
import json
from dataclasses import replace

import pytest

from agent.storage import CoreRepository, CoreDataError
from agent.runtime import ChatRunService
from agent.context_engine.service import ContextEngineService
from agent.repositories.context_repository import ContextRepository
from agent.providers import ProviderEvent
from agent.execution.domain import ModelCandidate
from agent.context_engine.domain import ContextEngineError


def seed(tmp_path, monkeypatch, bodies=("SELECTED_MARKER tail", "UNSELECTED_MARKER")):
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    repo = CoreRepository(tmp_path / "test.db")
    attachments = []
    for index, body in enumerate(bodies):
        path = tmp_path / f"a{index}.txt"
        path.write_text(body, encoding="utf-8")
        attachments.append(dict(id=f"a{index}", kind="text", name="same.txt",
            media_type="text/plain", byte_size=len(body.encode()),
            sha256=hashlib.sha256(body.encode()).hexdigest(), storage_key=path.name))
    run = repo.start_model_run(content="old", idempotency_key="seed",
        provider_profile_id="builtin-demo", model_id="demo", kind="standard_chat",
        category="casual", attachments=attachments)
    repo.transition_model_run(run["id"], {"queued"}, "cancelled")
    return repo, run


def test_explicit_old_reference_only_send_preserves_selected_range(tmp_path, monkeypatch):
    repo, old = seed(tmp_path, monkeypatch)
    async def scenario():
        async def sink(event):
            pass
        service = ChatRunService(repo, sink)
        result = await service.start(dict(conversation_id=old["conversation_id"], content="",
            idempotency_key="new", provider_profile_id="builtin-demo", model_id="demo",
            attachment_references=[dict(attachment_id="a0", source_start=0, source_end=15)]))
        await service.tasks[result["run"]["id"]]
        packed = ContextEngineService(repo).assemble(result["run"])
        prompt = json.dumps(packed.messages)
        assert "SELECTED_MARKER" in prompt
        assert "UNSELECTED_MARKER" not in prompt
        assert " tail" not in prompt
        item = next(item for item in packed.items if item.source.source_type == "attachment")
        assert item.source.source_start == 0
        assert item.source.source_end == 15
        assert item.metadata["source_attachment_id"] == "a0"
        context = ContextRepository(repo)
        snapshot = context.preview(result["run"]["id"])
        source_item = next(item for item in snapshot["items"] if item["source_type"] == "attachment")
        preview = context.source_preview(result["run"]["id"], source_item["id"])["source"]
        assert preview["content"] == "SELECTED_MARKER"
        assert preview["source_end"] == 15
        assert "storage_key" not in preview
        plan = service.execution_repository.get_for_run(result["run"]["id"])
        selected_ids = plan["resolved_intent"]["effective_attachment_ids"]
        assert selected_ids == [item.source.source_id]
        assert all(item.source.source_id in step["input_source_ids"] for step in plan["steps"])
        retry = repo.create_expanded_retry(result["run"]["id"])
        retried = ContextEngineService(repo).assemble(retry)
        assert " tail" not in json.dumps(retried.messages)
        assert next(i for i in retried.items if i.source.source_type == "attachment").source.source_end == 15
        repo.transition_model_run(retry["id"], {"queued"}, "cancelled")
        with repo._connect() as connection:
            connection.execute("UPDATE context_items SET source_end = 19 WHERE id = ?", (source_item["id"],))
        with pytest.raises(CoreDataError) as failure:
            context.source_preview(result["run"]["id"], source_item["id"])
        assert failure.value.code == "attachment_reference_range_invalid"
        (tmp_path / "a0.txt").write_text("changed", encoding="utf-8")
        with pytest.raises(CoreDataError) as failure:
            context.source_preview(result["run"]["id"], source_item["id"])
        assert failure.value.code == "attachment_changed"
    asyncio.run(scenario())


def test_reference_scope_rechecked_at_assembly(tmp_path, monkeypatch):
    repo, old = seed(tmp_path, monkeypatch)
    async def scenario():
        async def sink(event):
            pass
        service = ChatRunService(repo, sink)
        result = await service.start(dict(conversation_id=old["conversation_id"], content="read",
            idempotency_key="scope", provider_profile_id="builtin-demo", model_id="demo",
            attachment_references=[dict(attachment_id="a0")]))
        await service.tasks[result["run"]["id"]]
        other = repo.create_conversation("standard_chat", "Other", "casual")
        with repo._connect() as connection:
            connection.execute("UPDATE messages SET conversation_id = ? WHERE id = ?", (other["id"], old["user_message_id"]))
        with pytest.raises(CoreDataError) as failure:
            service.context.assemble(result["run"])
        assert failure.value.code == "attachment_reference_forbidden"
    asyncio.run(scenario())


def test_image_excluded_from_nonvision_route_with_auditable_reason(tmp_path, monkeypatch):
    repo, old = seed(tmp_path, monkeypatch)
    with repo._connect() as connection:
        connection.execute("UPDATE message_attachments SET kind = 'image', media_type = 'image/png', width = 20, height = 20 WHERE id = 'a0'")
    async def scenario():
        async def sink(event):
            pass
        service = ChatRunService(repo, sink)
        service.profiles["builtin-demo"] = replace(service.profiles["builtin-demo"],
            model_capabilities={"demo": {"text": "yes", "vision": "no", "vision_status": "unsupported", "context_length": 32768}})
        result = await service.start(dict(conversation_id=old["conversation_id"], content="describe",
            idempotency_key="image", provider_profile_id="builtin-demo", model_id="demo",
            attachment_references=[dict(attachment_id="a0")]))
        await service.tasks[result["run"]["id"]]
        plan = service.execution_repository.get_for_run(result["run"]["id"])
        assert plan["requirements"]["vision"] == "required"
        snapshot = ContextRepository(repo).preview(result["run"]["id"])
        item = next(i for i in snapshot["items"] if i["source_type"] == "attachment")
        assert item["included"] == 0
        assert item["exclusion_reason"] == "model_vision_unsupported"
    asyncio.run(scenario())


def test_selected_image_is_sent_on_vision_route(tmp_path, monkeypatch):
    repo, old = seed(tmp_path, monkeypatch)
    with repo._connect() as connection:
        connection.execute("UPDATE message_attachments SET kind = 'image', media_type = 'image/png', width = 20, height = 20 WHERE id = 'a0'")
    async def scenario():
        async def sink(event):
            pass
        service = ChatRunService(repo, sink)
        result = await service.start(dict(conversation_id=old["conversation_id"], content="describe",
            idempotency_key="vision", provider_profile_id="builtin-demo", model_id="demo",
            attachment_references=[dict(attachment_id="a0")]))
        await service.tasks[result["run"]["id"]]
        snapshot = ContextRepository(repo).preview(result["run"]["id"])
        item = next(i for i in snapshot["items"] if i["source_type"] == "attachment")
        assert item["included"] == 1
        step = service.execution_repository.get_for_run(result["run"]["id"])["steps"][-1]
        packed = service.context.assemble(result["run"], serialization_identity=service._serialization_identity(step))
        image = next(part for m in packed.messages if isinstance(m["content"], list)
                     for part in m["content"] if part["type"] == "image")
        assert image["attachment_id"] == item["source_id"]
    asyncio.run(scenario())


@pytest.mark.parametrize("reference", [
    {"attachment_id": "a0", "source_start": -1},
    {"attachment_id": "a0", "source_end": 999},
    {"attachment_id": "a0", "source_start": True},
    {"attachment_id": "a0", "storage_key": "forged"},
])
def test_untrusted_reference_range_and_paths_rejected(tmp_path, monkeypatch, reference):
    repo, old = seed(tmp_path, monkeypatch)
    with pytest.raises(CoreDataError):
        repo.resolve_attachment_references(old["conversation_id"], [reference])


@pytest.mark.parametrize("change,code", [("delete", "attachment_unavailable"), ("change", "attachment_changed")])
def test_deleted_or_changed_reference_never_reads_stale_body(tmp_path, monkeypatch, change, code):
    repo, old = seed(tmp_path, monkeypatch)
    if change == "delete":
        (tmp_path / "a0.txt").unlink()
    else:
        (tmp_path / "a0.txt").write_text("new", encoding="utf-8")
    with pytest.raises(CoreDataError) as failure:
        repo.resolve_attachment_references(old["conversation_id"], [{"attachment_id": "a0"}])
    assert failure.value.code == code


def test_derived_and_copied_work_accept_only_their_local_ids(tmp_path, monkeypatch):
    repo, old = seed(tmp_path, monkeypatch)
    project = repo.create_project("project", "coding")
    derived = repo.derive_work(old["conversation_id"], project["id"])
    copied = repo.copy_work(derived["id"], project["id"])
    for conversation in (derived, copied):
        own = conversation["messages"][0]["attachments"][0]["id"]
        assert repo.resolve_attachment_references(conversation["id"], [{"attachment_id": own}])[0]["id"] == own
        with pytest.raises(CoreDataError) as failure:
            repo.resolve_attachment_references(conversation["id"], [{"attachment_id": "a0"}])
        assert failure.value.code == "attachment_reference_forbidden"


def test_merged_reference_and_new_attachments_count_checked_before_run(tmp_path, monkeypatch):
    repo, old = seed(tmp_path, monkeypatch)
    async def scenario():
        async def sink(event):
            pass
        service = ChatRunService(repo, sink)
        new = repo.get_attachment("a1")
        with pytest.raises(CoreDataError) as failure:
            await service.start(dict(conversation_id=old["conversation_id"], content="read",
                idempotency_key="many", provider_profile_id="builtin-demo", model_id="demo",
                attachments=[dict(new, id=f"new{i}") for i in range(8)],
                attachment_references=[dict(attachment_id="a0")]))
        assert failure.value.code == "invalid_attachments"
    asyncio.run(scenario())


def test_merged_reference_and_new_attachment_size_limit(tmp_path, monkeypatch):
    repo, old = seed(tmp_path, monkeypatch)
    with repo._connect() as connection:
        connection.execute("UPDATE message_attachments SET kind = 'image', media_type = 'image/png', byte_size = 9437184, width = 20, height = 20 WHERE id = 'a0'")
    async def scenario():
        async def sink(event):
            pass
        service = ChatRunService(repo, sink)
        image = repo.get_attachment("a0")
        with pytest.raises(CoreDataError) as failure:
            await service.start(dict(conversation_id=old["conversation_id"], content="read",
                idempotency_key="large", provider_profile_id="builtin-demo", model_id="demo",
                attachments=[dict(image, id="new1"), dict(image, id="new2")],
                attachment_references=[dict(attachment_id="a0")]))
        assert failure.value.code == "attachments_too_large"
    asyncio.run(scenario())


def test_explicit_whole_text_is_not_silently_truncated_for_small_budget(tmp_path, monkeypatch):
    repo, old = seed(tmp_path, monkeypatch, ("large material " * 3000,))
    selected = repo.resolve_attachment_references(old["conversation_id"], [{"attachment_id": "a0"}])
    run = repo.start_model_run(content="read all", idempotency_key="budget", conversation_id=old["conversation_id"],
        provider_profile_id="builtin-demo", model_id="demo", attachments=[dict(selected[0], id="fresh-whole", reference_attachment_id=None)])
    with pytest.raises(ContextEngineError) as failure:
        ContextEngineService(repo).assemble(run, context_window=4096, max_output_tokens=512)
    assert failure.value.code == "current_input_too_large"


def test_unicode_ranges_are_code_points(tmp_path, monkeypatch):
    repo, old = seed(tmp_path, monkeypatch, ("前😀后",))
    selected = repo.resolve_attachment_references(old["conversation_id"],
        [{"attachment_id": "a0", "source_start": 1, "source_end": 2}])
    prepared = ContextEngineService(repo)._prepare_message(
        {"id": "current", "content": "read", "attachments": selected}, "current")
    assert "😀" in prepared["_model_content"]
    assert "前" not in prepared["_model_content"]
    assert "后" not in prepared["_model_content"]


def test_reference_does_not_inherit_online_permission_from_prior_route(tmp_path, monkeypatch):
    repo, old = seed(tmp_path, monkeypatch)
    project = repo.create_project("Private", "work")
    repo.update_project_model_policy(project["id"], "local_only")
    work = repo.derive_work(old["conversation_id"], project["id"])
    attachment_id = work["messages"][0]["attachments"][0]["id"]
    def forbidden_provider(profile):
        raise AssertionError("A local-only reference must never dispatch to an online provider")
    monkeypatch.setattr("agent.runtime.create_provider", forbidden_provider)
    async def scenario():
        async def sink(event):
            pass
        service = ChatRunService(repo, sink)
        service.configure_provider(dict(id="online", name="Offline fixture", kind="openai_compatible",
            base_url="https://example.invalid", api_key="synthetic-offline", allowed_model_ids=["model"]))
        with pytest.raises(CoreDataError, match="local models"):
            await service.start(dict(conversation_id=work["id"], content="",
                idempotency_key="local", provider_profile_id="online", model_id="model",
                attachment_references=[dict(attachment_id=attachment_id)]))
    asyncio.run(scenario())


def test_historical_reference_manifest_does_not_include_future_attachments(tmp_path, monkeypatch):
    repo, old = seed(tmp_path, monkeypatch)
    attachment = dict(repo.get_attachment("a0"), id="future", name="FUTURE_ATTACHMENT.txt")
    repo.start_model_run(content="later", idempotency_key="future", conversation_id=old["conversation_id"],
        provider_profile_id="builtin-demo", model_id="demo", attachments=[attachment])
    assert "FUTURE_ATTACHMENT.txt" not in json.dumps(ContextEngineService(repo).assemble(old).messages)


def test_planless_clone_fails_closed_instead_of_widening_selection(tmp_path, monkeypatch):
    repo, old = seed(tmp_path, monkeypatch)
    selection = repo.resolve_attachment_references(old["conversation_id"],
        [{"attachment_id": "a0", "source_start": 0, "source_end": 15}])
    run = repo.start_model_run(content="", idempotency_key="partial-commit", conversation_id=old["conversation_id"],
        provider_profile_id="builtin-demo", model_id="demo", attachments=selection)
    with pytest.raises(CoreDataError) as failure:
        ContextEngineService(repo).assemble(run)
    assert failure.value.code == "attachment_reference_selection_unavailable"


def test_reference_cannot_cross_conversation(tmp_path, monkeypatch):
    repo, old = seed(tmp_path, monkeypatch)
    other = repo.create_conversation("standard_chat", "Other", "casual")
    resolver = getattr(repo, "resolve_attachment_references", None)
    assert callable(resolver), "Core must resolve references from its authoritative database"
    with pytest.raises(CoreDataError) as failure:
        resolver(other["id"], [dict(attachment_id="a0")])
    assert failure.value.code == "attachment_reference_forbidden"


def test_removed_chip_does_not_infer_permission_from_text(tmp_path, monkeypatch):
    repo, old = seed(tmp_path, monkeypatch)
    async def scenario():
        async def sink(event):
            pass
        service = ChatRunService(repo, sink)
        result = await service.start(dict(conversation_id=old["conversation_id"],
            content="read same.txt again", attachment_references=[], idempotency_key="removed",
            provider_profile_id="builtin-demo", model_id="demo"))
        await service.tasks[result["run"]["id"]]
        prompt = json.dumps(ContextEngineService(repo).assemble(result["run"]).messages)
        assert "SELECTED_MARKER" not in prompt
        assert "same.txt" in prompt
        assert "attachment-reference" in prompt
    asyncio.run(scenario())


@pytest.mark.parametrize("retry_guard", ["allowed", "budget", "revoked", "orphan_budget"])
def test_planner_specialist_and_final_share_selected_sources(tmp_path, monkeypatch, retry_guard):
    repo, old = seed(tmp_path, monkeypatch)
    with repo._connect() as connection:
        connection.execute("UPDATE message_attachments SET kind = 'image', media_type = 'image/png', width = 20, height = 20 WHERE id = 'a0'")
    captured = []
    class Provider:
        def __init__(self, profile):
            self.profile = profile
        async def stream_events(self, model_id, messages, **kwargs):
            captured.append((model_id, messages))
            yield ProviderEvent("text_delta", text="Offline observation.")
    monkeypatch.setattr("agent.runtime.create_provider", Provider)
    async def scenario():
        async def sink(event):
            pass
        service = ChatRunService(repo, sink)
        service.profiles["builtin-demo"] = replace(service.profiles["builtin-demo"],
            allowed_model_ids=("general", "vision"), automatic_model_ids=("general", "vision"),
            model_capabilities={
                "general": {"text": "yes", "vision": "no", "vision_status": "unsupported", "context_length": 32768},
                "vision": {"text": "yes", "vision": "yes", "vision_status": "verified", "context_length": 32768},
            })
        repo.update_conversation_routing(old["conversation_id"], "automatic", "balanced", "builtin-demo", "general")
        # Explicit offline candidate routes: the general endpoint has no image dialect.
        monkeypatch.setattr(service, "_execution_candidates", lambda *args, **kwargs: [
            ModelCandidate(provider_profile_id="builtin-demo", model_id=model,
                protocol_id="memory", location="local", tool_mode="text_only",
                capabilities=service.profiles["builtin-demo"].model_capabilities[model])
            for model in ("general", "vision")])
        result = await service.start(dict(conversation_id=old["conversation_id"], content="describe",
            idempotency_key="specialist", attachment_references=[dict(attachment_id="a0")]))
        await service.tasks[result["run"]["id"]]
        plan = service.execution_repository.get_for_run(result["run"]["id"])
        assert [s["step_type"] for s in plan["steps"]] == ["vision_analysis", "final_response"]
        selected = plan["resolved_intent"]["effective_attachment_ids"]
        assert len(selected) == 1
        assert all(selected[0] in s["input_source_ids"] for s in plan["steps"])
        assert [model for model, _ in captured] == ["vision", "general"], repo.get_model_run(result["run"]["id"])
        assert selected[0] in json.dumps(captured[0][1])
        assert "UNSELECTED_MARKER" not in json.dumps(captured)
        assert not any(p["type"] == "image" for m in captured[1][1] if isinstance(m["content"], list) for p in m["content"])
        assert "Offline observation." in json.dumps(captured[1][1])
        if retry_guard == "revoked":
            service.profiles["builtin-demo"] = replace(service.profiles["builtin-demo"], allowed_model_ids=("general",))
            with pytest.raises(CoreDataError) as failure:
                await service.retry(result["run"]["id"], context_strategy="expanded_retry")
            assert failure.value.code == "execution_plan_stale"
            assert len(captured) == 2
            return
        if retry_guard in {"budget", "orphan_budget"}:
            service.routing_preferences["max_model_calls_per_message"] = 1
        if retry_guard == "orphan_budget":
            orphan = repo.create_expanded_retry(result["run"]["id"])
            repo.transition_model_run(orphan["id"], {"queued"}, "failed", "invocation_spec_invalid")
            child = await service.retry(orphan["id"])
        else:
            child = await service.retry(result["run"]["id"], context_strategy="expanded_retry")
        assert child["execution_plan_id"]
        if retry_guard in {"budget", "orphan_budget"}:
            assert child["status"] == "waiting_budget"
            assert len(captured) == 2
            assert child["id"] not in service.tasks
            child_plan = service.execution_repository.get_for_run(child["id"])
            assert child_plan["plan_hash"] != plan["plan_hash"]
            await service.resolve_routing_budget(child["id"], child_plan["plan_hash"], True)
        await service.tasks[child["id"]]
        assert repo.get_model_run(child["id"])["status"] == "completed"
        child_plan = service.execution_repository.get_for_run(child["id"])
        assert [s["step_type"] for s in child_plan["steps"]] == ["vision_analysis", "final_response"]
        assert child_plan["resolved_intent"]["effective_attachment_ids"] == selected
        assert [model for model, _ in captured] == ["vision", "general", "vision", "general"]
    asyncio.run(scenario())
