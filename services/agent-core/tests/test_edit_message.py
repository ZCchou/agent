"""Editing continues from a history prefix without destroying the original."""
import asyncio
import json

import pytest

from agent.storage import CoreDataError
from test_image_capability_fallback import setup
from test_tool_continuation_retry import install_http


def seed(repository, chat, content, answer, key, attachments=None):
    run = repository.start_model_run(conversation_id=chat["id"], content=content,
        idempotency_key=key, provider_profile_id="synthetic", model_id="model", attachments=attachments)
    attempt = repository.begin_run_attempt(run["id"])
    repository.transition_model_run(run["id"], {"queued"}, "generating")
    repository.complete_model_run(run["id"], attempt["id"], answer)
    return run


def test_edit_regenerates_from_prefix_and_preserves_original_attachments_and_selection(tmp_path, monkeypatch):
    requests = install_http(monkeypatch, [({"content": "Regenerated answer"}, "stop")])
    async def scenario():
        repository, chat, service, image = await setup(tmp_path, monkeypatch, vision="yes")
        seed(repository, chat, "Earlier question", "Earlier answer", "earlier")
        target = seed(repository, chat, "Original question", "ORIGINAL_ANSWER_SECRET", "target", [image])
        seed(repository, chat, "LATER_QUESTION_SECRET", "LATER_ANSWER_SECRET", "later")
        before = repository.get_conversation(chat["id"])
        params = dict(conversation_id=chat["id"], content="Edited question", edit_message_id=target["user_message_id"],
            idempotency_key="edit-request", selection=dict(mode="fixed", provider_profile_id="synthetic", model_id="model"))
        result = await service.start(params)
        task = service.tasks.get(result["run"]["id"])
        if task: await task
        run = repository.get_model_run(result["run"]["id"])
        assert run["status"] == "completed", run
        assert run["conversation_id"] != chat["id"], "keep the original version intact"
        assert repository.get_conversation(chat["id"])["messages"] == before["messages"]
        branch = repository.get_conversation(run["conversation_id"])
        assert branch["model_selection_mode"] == "fixed" and branch["model_id"] == "model"
        assert [m["content"] for m in branch["messages"] if m["role"] in {"user", "assistant"}] == [
            "Earlier question", "Earlier answer", "Edited question", "Regenerated answer"]
        edited = next(m for m in branch["messages"] if m["id"] == run["user_message_id"])
        assert edited["attachments"][0]["sha256"] == image["sha256"]
        assert edited["attachments"][0]["id"] != image["id"]
        prompt = json.dumps(requests[0][1]["messages"])
        assert "Earlier question" in prompt and "Edited question" in prompt
        assert "Original question" not in prompt and "_SECRET" not in prompt
        assert "image_url" in prompt and requests[0][1]["model"] == "model"
        retry = await service.start(params)
        assert retry["run"]["id"] == run["id"] and len(requests) == 1
        assert len(repository.list_conversations()) == 2
    asyncio.run(scenario())


@pytest.mark.parametrize("target", ["missing", "foreign", "assistant", 42])
def test_invalid_edit_targets_do_not_create_a_branch_or_dispatch(tmp_path, monkeypatch, target):
    requests = install_http(monkeypatch, [])
    async def scenario():
        repository, chat, service, _ = await setup(tmp_path, monkeypatch)
        old = seed(repository, chat, "Question", "Answer", "original")
        foreign = repository.create_conversation("standard_chat", "Other", "casual")
        foreign_run = seed(repository, foreign, "Foreign", "Foreign answer", "foreign")
        identity = {"missing": "missing", "foreign": foreign_run["user_message_id"],
            "assistant": repository.get_model_run(old["id"])["assistant_message"]["id"]}.get(target, target)
        with pytest.raises(CoreDataError) as error:
            await service.start(dict(conversation_id=chat["id"], content="Edited", edit_message_id=identity,
                idempotency_key="invalid-edit", provider_profile_id="synthetic", model_id="model"))
        assert error.value.code == "invalid_message_edit"
        assert len(repository.list_conversations()) == 2 and not requests
    asyncio.run(scenario())


def test_active_generation_blocks_edit_without_mutating_history(tmp_path, monkeypatch):
    requests = install_http(monkeypatch, [])
    async def scenario():
        repository, chat, service, _ = await setup(tmp_path, monkeypatch)
        old = seed(repository, chat, "Question", "Answer", "original")
        repository.start_model_run(conversation_id=chat["id"], content="Pending", idempotency_key="pending",
            provider_profile_id="synthetic", model_id="model")
        with pytest.raises(CoreDataError) as error:
            await service.start(dict(conversation_id=chat["id"], content="Edited", edit_message_id=old["user_message_id"],
                idempotency_key="edit", provider_profile_id="synthetic", model_id="model"))
        assert error.value.code == "run_already_active"
        assert len(repository.list_conversations()) == 1 and not requests
    asyncio.run(scenario())


def test_edit_preserves_unread_attachment_receipts_from_prefix_and_question(tmp_path, monkeypatch):
    install_http(monkeypatch, [({"content": "Edited reply"}, "stop"), ({"content": "Edited later reply"}, "stop")])
    async def scenario():
        repository, chat, service, _ = await setup(tmp_path, monkeypatch)
        rejection = dict(receipt_id="rejected-one", display_name="unsupported.bin",
            detected_media_type="application/octet-stream", error_code="attachment_processing_failed", processor_hint=None)
        run = repository.start_model_run(conversation_id=chat["id"], content="Question with unread file",
            idempotency_key="receipt-original", provider_profile_id="synthetic", model_id="model", attachment_rejections=[rejection])
        repository.transition_model_run(run["id"], {"queued"}, "cancelled")
        result = await service.start(dict(conversation_id=chat["id"], content="Edited question",
            edit_message_id=run["user_message_id"], idempotency_key="edit-receipt", provider_profile_id="synthetic", model_id="model"))
        task = service.tasks.get(result["run"]["id"])
        if task: await task
        user = result["user_message"]
        assert user["attachment_rejections"][0]["display_name"] == "unsupported.bin"
        # Editing a later question also clones receipts in the preceding history.
        later = seed(repository, chat, "Later", "Answer", "receipt-later")
        later_result = await service.start(dict(conversation_id=chat["id"], content="Edited later",
            edit_message_id=later["user_message_id"], idempotency_key="edit-receipt-prefix", provider_profile_id="synthetic", model_id="model"))
        prefix = later_result["conversation"]["messages"]
        assert any(m.get("attachment_rejections") for m in prefix)
        task = service.tasks.get(later_result["run"]["id"])
        if task: await task
        assert repository.get_model_run(later_result["run"]["id"])["status"] == "completed"
    asyncio.run(scenario())
