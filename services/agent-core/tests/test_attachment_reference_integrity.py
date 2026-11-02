"""Saved-provenance corruption regressions; no public SQL attack is assumed."""
import asyncio
import json
from dataclasses import replace

import pytest

from agent.runtime import ChatRunService
from agent.storage import CoreDataError
from agent.context_engine.service import ContextEngineService
from agent.repositories.context_repository import ContextRepository
from agent.execution.domain import stable_hash
from agent.providers import ProviderEvent
from test_attachment_references import seed


def completed_reference(tmp_path, monkeypatch, *, image=False, project=False):
    class Provider:
        def __init__(self, profile):
            self.profile = profile
        async def stream_events(self, model_id, messages, **kwargs):
            yield ProviderEvent("text_delta", text="Offline response.")
    monkeypatch.setattr("agent.runtime.create_provider", Provider)
    repo, old = seed(tmp_path, monkeypatch)
    if image:
        with repo._connect() as connection:
            connection.execute("UPDATE message_attachments SET kind='image', media_type='image/png', width=20, height=20 WHERE id='a0'")
    source_id = "a0"
    if project:
        workspace = repo.create_project("Private", "work")
        repo.update_project_model_policy(workspace["id"], "local_only")
        work = repo.derive_work(old["conversation_id"], workspace["id"])
        old = {**old, "conversation_id": work["id"]}
        source_id = work["messages"][0]["attachments"][0]["id"]
    async def scenario():
        async def sink(event):
            pass
        service = ChatRunService(repo, sink)
        reference = {"attachment_id": source_id}
        if not image:
            reference.update(source_start=0, source_end=15)
        response = await service.start(dict(conversation_id=old["conversation_id"], content="read",
            idempotency_key="selected", provider_profile_id="builtin-demo", model_id="demo",
            attachment_references=[reference]))
        await service.tasks[response["run"]["id"]]
        assert repo.get_model_run(response["run"]["id"])["status"] == "completed"
        return repo, response["run"], service
    return asyncio.run(scenario())


@pytest.mark.parametrize("mutation,refresh_resolution_hash", [
    ("range", False), ("range", True), ("source", False), ("source", True),
    ("effective_ids", False),
])
def test_saved_selection_changes_fail_closed(tmp_path, monkeypatch, mutation, refresh_resolution_hash):
    repo, run, service = completed_reference(tmp_path, monkeypatch)
    plan = service.execution_repository.get_for_run(run["id"])
    intent = plan["resolved_intent"]
    if mutation == "range":
        intent["attachment_selection"][0]["source_end"] = 20
    elif mutation == "source":
        # Remap to the same clone/bytes, rather than relying on a scope/hash error.
        intent["attachment_selection"][0]["source_attachment_id"] = intent["attachment_selection"][0]["current_attachment_id"]
    else:
        intent["effective_attachment_ids"] = []
    with repo._connect() as connection:
        connection.execute("UPDATE execution_plans SET resolved_intent_json=?, resolution_hash=? WHERE id=?",
            (json.dumps(intent), stable_hash(intent) if refresh_resolution_hash else plan["resolution_hash"], plan["id"]))
    with pytest.raises(CoreDataError) as failure:
        ContextEngineService(repo).assemble(run)
    assert failure.value.code == "attachment_reference_selection_changed"


@pytest.mark.parametrize("mutation,expected_code", [
    ("source_hash", "attachment_changed"),
    ("saved_mapping", "attachment_reference_selection_changed"),
    ("context_item_mapping", "context_source_changed"),
    ("artifact_changed", "context_source_changed"),
])
def test_image_preview_uses_current_saved_selection(tmp_path, monkeypatch, mutation, expected_code):
    repo, run, service = completed_reference(tmp_path, monkeypatch, image=True)
    context = ContextRepository(repo)
    item = next(i for i in context.preview(run["id"])["items"] if i["source_type"] == "attachment")
    plan = service.execution_repository.get_for_run(run["id"])
    if mutation == "artifact_changed":
        payload = (tmp_path / "a0.txt").read_bytes()
        normalized = tmp_path / "message-attachments" / "normalized.png"
        normalized.parent.mkdir()
        normalized.write_bytes(payload)
        repo.update_attachment_model_copy(item["source_id"], "message-attachments/normalized.png",
            "image/png", len(payload), item["source_hash"], 20, 20, "image-normalization-v1")
    with repo._connect() as connection:
        if mutation == "source_hash":
            connection.execute("UPDATE message_attachments SET sha256=? WHERE id='a0'", ("f" * 64,))
        elif mutation == "saved_mapping":
            intent = plan["resolved_intent"]
            intent["attachment_selection"][0]["source_attachment_id"] = item["source_id"]
            connection.execute("UPDATE execution_plans SET resolved_intent_json=? WHERE id=?", (json.dumps(intent), plan["id"]))
        elif mutation == "context_item_mapping":
            metadata = item["metadata"]
            metadata["source_attachment_id"] = item["source_id"]
            connection.execute("UPDATE context_items SET metadata_json=? WHERE id=?", (json.dumps(metadata), item["id"]))
    with pytest.raises(CoreDataError) as failure:
        context.source_preview(run["id"], item["id"])
    assert failure.value.code == expected_code


def test_expanded_reference_retry_rechecks_project_policy(tmp_path, monkeypatch):
    repo, run, service = completed_reference(tmp_path, monkeypatch, project=True)
    service.profiles["builtin-demo"] = replace(service.profiles["builtin-demo"],
        kind="openai_compatible", base_url="https://example.invalid")
    def forbidden_provider(profile):
        raise AssertionError("A provider made online after the original run must not receive local-only material")
    monkeypatch.setattr("agent.runtime.create_provider", forbidden_provider)
    async def scenario():
        with pytest.raises(CoreDataError) as failure:
            await service.retry(run["id"], context_strategy="expanded_retry")
        assert failure.value.code == "execution_plan_stale"
    asyncio.run(scenario())


def test_nonselection_intent_refresh_preserves_valid_selection(tmp_path, monkeypatch):
    repo, run, service = completed_reference(tmp_path, monkeypatch)
    plan = service.execution_repository.get_for_run(run["id"])
    intent = plan["resolved_intent"]
    intent["advisor"] = {"web": "unnecessary"}
    with repo._connect() as connection:
        connection.execute("UPDATE execution_plans SET resolved_intent_json=?, resolution_hash=? WHERE id=?",
            (json.dumps(intent), stable_hash(intent), plan["id"]))
    service.execution_repository.update_resolved_intent(plan["id"], {"requirement": "off", "reason_code": "test-refresh"})
    packed = ContextEngineService(repo).assemble(run)
    assert "SELECTED_MARKER" in json.dumps(packed.messages)
    assert " tail" not in json.dumps(packed.messages)


@pytest.mark.parametrize("tamper_source", [False, True])
def test_interrupted_linked_retry_requires_verified_ancestor_selection_before_dispatch(tmp_path, monkeypatch, tamper_source):
    repo, run, service = completed_reference(tmp_path, monkeypatch)
    child = repo.create_expanded_retry(run["id"])
    repo.transition_model_run(child["id"], {"queued"}, "interrupted")
    parent_plan = service.execution_repository.get_for_run(run["id"])
    if tamper_source:
        with repo._connect() as connection:
            connection.execute("UPDATE message_attachments SET sha256=? WHERE id='a0'", ("b" * 64,))
    dispatched = []
    class Provider:
        def __init__(self, profile):
            self.profile = profile
        async def stream_events(self, model_id, messages, **kwargs):
            assert service.execution_repository.get_for_run(child["id"])
            dispatched.append(model_id)
            assert "SELECTED_MARKER" in json.dumps(messages)
            yield ProviderEvent("text_delta", text="Recovered only after validating the saved selection.")
    monkeypatch.setattr("agent.runtime.create_provider", Provider)
    async def scenario():
        if tamper_source:
            with pytest.raises(CoreDataError) as failure:
                await service.retry(child["id"])
            assert failure.value.code == "attachment_changed"
            assert not dispatched
            assert service.execution_repository.get_for_run(child["id"]) is None
            return
        retried = await service.retry(child["id"])
        assert retried["execution_plan_id"]
        await service.tasks[child["id"]]
        assert repo.get_model_run(child["id"])["status"] == "completed"
        assert dispatched == [run["model_id"]]
        assert service.execution_repository.get_for_run(child["id"])["resolved_intent"]["attachment_selection"] == parent_plan["resolved_intent"]["attachment_selection"]
    asyncio.run(scenario())
