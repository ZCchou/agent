import asyncio
import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from agent.providers import ProviderEvent
from agent.runtime import ChatRunService
from agent.storage import CoreRepository
from agent.tool_contracts import ToolCall


FIXTURE = json.loads((Path(__file__).parents[3] / "contracts" / "reliability-repair-fixtures.json").read_text(encoding="utf-8"))


def review_fixture(tmp_path, monkeypatch, *, fake=False):
    repository = CoreRepository(tmp_path / "review.db")

    async def sink(_event):
        pass

    service = ChatRunService(repository, sink)
    project = repository.create_project("Synthetic", "coding")
    conversation = repository.create_conversation("project_work", "Work", project_id=project["id"])
    run = repository.start_model_run(content="Write notes", idempotency_key="review",
        provider_profile_id="builtin-demo", model_id="demo", conversation_id=conversation["id"])
    repository.transition_model_run(run["id"], {"queued"}, "building_context")
    repository.transition_model_run(run["id"], {"building_context"}, "generating")
    attempt = repository.begin_run_attempt(run["id"])
    tools = service.tool_loop.repository
    plan = copy.deepcopy(FIXTURE["change_set"]["plan"])
    plan["effective_risk"] = "medium"
    mutations = [step["mutation"] for step in plan["steps"]]
    operation = tools.create(run["id"], attempt["id"], 1, ToolCall("review-call", "fs.propose_changes", {
        "title": "Write synthetic notes", "mutations": mutations,
    }))
    tools.transition(operation["id"], {"requested"}, "reviewing")

    if not fake:
        service.profiles["builtin-demo"] = replace(service.profiles["builtin-demo"], kind="openai_compatible", base_url="http://localhost")
    prompts = []

    class CapturingProvider:
        async def stream_events(self, _model, messages, *, tools, options):
            assert tools is False
            assert options.purpose == "tool_review"
            prompts.append(json.loads(messages[-1]["content"]))
            yield ProviderEvent("text_delta", text='{"decision":"approve","reason_code":"bounded_change"}')

    monkeypatch.setattr("agent.runtime.create_provider", lambda _profile: CapturingProvider())
    return service, {"run_id": run["id"], "operation_id": operation["id"], "risk": "medium",
                     "title": "Write synthetic notes", "mutations": mutations, "plan": plan}, prompts


def test_ai_review_sees_canonical_plan_and_returns_matching_identity(tmp_path, monkeypatch):
    service, params, prompts = review_fixture(tmp_path, monkeypatch)
    result = asyncio.run(service.review_tool(params))
    assert result["decision"] == "approve"
    assert prompts[0]["plan"] == params["plan"]
    assert prompts[0]["mutations"] == [step["mutation"] for step in params["plan"]["steps"]]
    assert result["plan_id"] == params["plan"]["plan_id"]
    assert result["plan_hash"] == params["plan"]["plan_hash"]


@pytest.mark.parametrize("damage", ["missing", "identity", "risk", "mutations"])
@pytest.mark.parametrize("fake", [False, True])
def test_unbound_or_inconsistent_plan_never_receives_automatic_approval(tmp_path, monkeypatch, damage, fake):
    service, params, prompts = review_fixture(tmp_path, monkeypatch, fake=fake)
    if damage == "missing":
        params.pop("plan")
    elif damage == "identity":
        params["plan"]["plan_hash"] = ""
    elif damage == "risk":
        params["plan"]["effective_risk"] = "high"
    else:
        params["mutations"] = [{"kind": "delete", "path": "unreviewed.txt"}]
    result = asyncio.run(service.review_tool(params))
    assert result["decision"] == "uncertain"
    assert result["reason_code"] == "file_review_plan_invalid"
    assert not prompts
