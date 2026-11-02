"""Capability limits yield honest replies while retaining the original image."""
import asyncio
import base64
import hashlib
import json

import httpx
import pytest

from agent.runtime import ChatRunService
from agent.storage import CoreRepository
from test_tool_continuation_retry import configuration, configure, install_http


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aFz8AAAAASUVORK5CYII=")


async def setup(tmp_path, monkeypatch, *, vision="no", context=65536, peer=False, mode="fixed", sink=None):
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    (tmp_path / "image.png").write_bytes(PNG)
    repository = CoreRepository(tmp_path / "image-fallback.db")
    chat = repository.create_conversation("standard_chat", "Images", "casual")
    repository.update_conversation_routing(chat["id"], mode, "balanced",
        "synthetic" if mode == "fixed" else None, "model" if mode == "fixed" else None)
    service = ChatRunService(repository, sink or (lambda event: asyncio.sleep(0)))
    service.production_routing_configured = True
    value = configuration()
    value["model_tool_modes"]["model"] = "text_only"
    value["model_capabilities"]["model"].update(vision=vision, vision_status={"no":"unsupported", "unknown":"unknown", "yes":"verified"}[vision], context_length=context)
    value["model_route_capabilities"]["model"] = [{"route_id":"chat", "capability_signature":["text"], "support_status":"declared", "route_config_fingerprint":"synthetic"}]
    if vision == "yes":
        value["model_route_capabilities"]["model"].append({"route_id":"chat", "capability_signature":["text","vision"], "support_status":"declared", "route_config_fingerprint":"synthetic"})
    if peer:
        value["allowed_model_ids"].append("vision-peer")
        value["automatic_model_ids"].append("vision-peer")
        value["model_tool_modes"]["vision-peer"] = "text_only"
        value["model_capabilities"]["vision-peer"] = {"text":"yes","vision":"yes","vision_status":"verified","context_length":65536}
        value["model_route_capabilities"]["vision-peer"] = [{"route_id":"chat", "capability_signature":["text","vision"], "support_status":"declared", "route_config_fingerprint":"synthetic"}]
    await configure(service, value)
    attachment = dict(id="original-image", kind="image", name="image.png", media_type="image/png",
        byte_size=len(PNG), sha256=hashlib.sha256(PNG).hexdigest(), storage_key="image.png",
        width=2048, height=2048, normalization_version="legacy_passthrough")
    return repository, chat, service, attachment


async def send(service, chat, attachment, *, content="Describe the attached image.", mode="fixed"):
    result = await service.start(dict(conversation_id=chat["id"], content=content, attachments=[attachment],
        idempotency_key="image-send", selection=dict(mode=mode, provider_profile_id="synthetic", model_id="model", routing_policy={"preset":"balanced"})))
    task = service.tasks.get(result["run"]["id"])
    if task is not None:
        await task
    return result


@pytest.mark.parametrize("vision,content,context,mode", [
    ("no", "Describe the attached image.", 65536, "fixed"),
    ("unknown", "Describe the attached image.", 65536, "fixed"),
    ("no", "", 65536, "fixed"),
    ("no", "Describe the attached image.", 4096, "fixed"),
    ("no", "Describe the attached image.", 65536, "automatic"),
])
def test_incompatible_image_is_saved_and_gets_text_reply_without_pixels(tmp_path, monkeypatch, vision, content, context, mode):
    requests = install_http(monkeypatch, [({"content":"Here are useful next steps."}, "stop")])
    async def scenario():
        events = []
        async def sink(event): events.append(event)
        repository, chat, service, image = await setup(tmp_path, monkeypatch, vision=vision, context=context,
            peer=mode == "fixed", mode=mode, sink=sink)
        result = await send(service, chat, image, content=content, mode=mode)
        run = repository.get_model_run(result["run"]["id"])
        assert run["status"] == "completed", (run["error_code"], run["error_message"])
        answer = run["assistant_message"]["content"]
        assert "Here are useful next steps." in answer
        assert "本轮未读取图片" in answer and "切换" in answer and "图片中的文字" in answer
        assert len(requests) == 1 and requests[0][1]["model"] == "model"
        prompt = json.dumps(requests[0][1]["messages"])
        assert base64.b64encode(PNG).decode() not in prompt and '"image_url"' not in prompt
        assert "capability-advisories" in prompt
        saved = repository.get_conversation(chat["id"])["messages"]
        user = next(m for m in saved if m["id"] == run["user_message_id"])
        assert user["content"] == content and user["attachments"][0]["id"] == image["id"]
        assert not any(e["event_type"] == "run.failed" for e in events)
        completed = next(e for e in events if e["event_type"] == "run.completed")
        assert completed["data"]["used_images"] is False
    asyncio.run(scenario())


def test_automatic_selection_uses_a_compatible_vision_model_without_false_unread_notice(tmp_path, monkeypatch):
    requests = install_http(monkeypatch, [({"content":"The compatible image model answered."}, "stop")])
    async def scenario():
        repository, chat, service, image = await setup(tmp_path, monkeypatch, peer=True, mode="automatic")
        result = await send(service, chat, image, mode="automatic")
        run = repository.get_model_run(result["run"]["id"])
        assert run["status"] == "completed", (run["error_code"], run["error_message"])
        assert len(requests) == 1 and requests[0][1]["model"] == "vision-peer"
        assert base64.b64encode(PNG).decode() in json.dumps(requests[0][1]["messages"])
        assert "本轮未读取图片" not in run["assistant_message"]["content"]
    asyncio.run(scenario())


@pytest.mark.parametrize("message,code", [
    ("This model does not support image input", "model_vision_unsupported"),
    ("Invalid image: unsupported media type", "image_format_unsupported"),
    ("Image payload too large", "image_payload_too_large"),
])
def test_provider_image_refusal_returns_advice_without_repeating_rejected_payload(tmp_path, monkeypatch, message, code):
    requests = []
    original = httpx.AsyncClient
    def reject(request):
        requests.append(json.loads(request.content))
        return httpx.Response(400, json={"error":{"message":message}})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(reject), **kwargs))
    async def scenario():
        repository, chat, service, image = await setup(tmp_path, monkeypatch, vision="yes")
        result = await send(service, chat, image)
        run = repository.get_model_run(result["run"]["id"])
        assert run["status"] == "completed", (run["error_code"], run["error_message"])
        assert len(requests) == 1, "do not resend the same unsupported pixels to a closing request"
        answer = run["assistant_message"]["content"]
        assert "图片" in answer and "切换" in answer and "图片中的文字" in answer
        assert code in answer and "internal_error" not in answer
    asyncio.run(scenario())


def test_automatic_vision_specialist_refusal_completes_with_honest_local_guidance(tmp_path, monkeypatch):
    requests = []
    original = httpx.AsyncClient
    def reject(request):
        requests.append(json.loads(request.content))
        return httpx.Response(400, json={"error": {"message": "This model does not support image input"}})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(reject), **kwargs))
    async def scenario():
        repository, chat, service, image = await setup(tmp_path, monkeypatch, peer=True, mode="automatic")
        repository.update_conversation_routing(chat["id"], "automatic", "balanced", "synthetic", "model")
        result = await send(service, chat, image, mode="automatic")
        assert result["run"]["status"] == "waiting_budget"
        await service.resolve_routing_budget(result["run"]["id"], result["execution_plan"]["plan_hash"], True)
        await service.tasks[result["run"]["id"]]
        run = repository.get_model_run(result["run"]["id"])
        assert run["status"] == "completed", (run["error_code"], run["error_message"])
        assert len(requests) == 1 and requests[0]["model"] == "vision-peer"
        answer = run["assistant_message"]["content"]
        assert "本轮未读取图片" in answer and "model_vision_unsupported" in answer and "图片中的文字" in answer
        plan = service.execution_repository.get_for_run(run["id"])
        assert plan["status"] == "failed", "the response completed, but the image analysis did not"
        assert next(s for s in plan["steps"] if s["step_type"] == "vision_analysis")["status"] == "failed"
        assert next(s for s in plan["steps"] if s["step_type"] == "final_response")["status"] == "skipped"
        assert not any(s.get("artifact") for s in plan["steps"]), "do not fabricate image observations"
    asyncio.run(scenario())
