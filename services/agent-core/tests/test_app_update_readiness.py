"""Updating must not race task admission or interrupt work in another chat."""
import asyncio

from agent.protocol import handle_async_message
from test_tool_continuation_retry import fixture, install_http


async def rpc(service, kind, **params):
    return await handle_async_message({"v": 16, "id": kind, "type": kind, "params": params}, service)


def test_idle_prepare_blocks_new_work_until_matching_abort(tmp_path, monkeypatch):
    requests = install_http(monkeypatch, [({"content": "Done"}, "stop")])
    async def scenario():
        repo, _, work, service = await fixture(tmp_path, lambda _event: asyncio.sleep(0))
        before = repo.get_conversation(work["id"])["messages"]
        prepared = await rpc(service, "app.update.prepare", update_id="update-1")
        assert prepared["result"]["ready"] is True
        start = dict(conversation_id=work["id"], content="Do work", idempotency_key="update-test",
                     selection=dict(mode="fixed", provider_profile_id="synthetic", model_id="model"))
        blocked = await rpc(service, "model.run.start", **start)
        assert blocked["error"]["code"] == "app_update_installing"
        assert repo.get_conversation(work["id"])["messages"] == before and not requests
        wrong = await rpc(service, "app.update.abort", update_id="different")
        assert "error" in wrong
        assert (await rpc(service, "model.run.start", **start))["error"]["code"] == "app_update_installing"
        assert (await rpc(service, "app.update.abort", update_id="update-1"))["result"]["resumed"] is True
        started = await rpc(service, "model.run.start", **start)
        await service.tasks[started["result"]["run"]["id"]]
        assert len(requests) == 1
    asyncio.run(scenario())


def test_a_start_not_yet_persisted_prevents_update_preparation(tmp_path, monkeypatch):
    async def scenario():
        repo, _, work, service = await fixture(tmp_path, lambda _event: asyncio.sleep(0))
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed_start(_params):
            entered.set()
            await release.wait()
            return {"synthetic_admission_completed": True}
        monkeypatch.setattr(service, "start", delayed_start)
        task = asyncio.create_task(rpc(service, "model.run.start", conversation_id=work["id"]))
        await entered.wait()
        assert not repo.active_model_runs()
        prepared = await rpc(service, "app.update.prepare", update_id="update-2")
        assert prepared["result"]["ready"] is False
        assert prepared["result"]["active_admissions"] == 1
        release.set()
        await task
        assert (await rpc(service, "app.update.prepare", update_id="update-2"))["result"]["ready"] is True
    asyncio.run(scenario())


def test_work_in_another_conversation_keeps_update_unprepared(tmp_path, monkeypatch):
    async def scenario():
        repo, project, work, service = await fixture(tmp_path, lambda _event: asyncio.sleep(0))
        other = repo.create_conversation("project_work", "Other", project_id=project["id"])
        run = repo.start_model_run(content="Still working", idempotency_key="other-active",
                                   provider_profile_id="synthetic", model_id="model", conversation_id=other["id"])
        prepared = await rpc(service, "app.update.prepare", update_id="update-3")
        assert prepared["result"]["ready"] is False and prepared["result"]["active_runs"] == 1
        repo.transition_model_run(run["id"], {"queued"}, "cancelled")
        assert (await rpc(service, "app.update.prepare", update_id="update-3"))["result"]["ready"] is True
    asyncio.run(scenario())


def test_checkpoint_failure_never_closes_new_work_admission(tmp_path, monkeypatch):
    from agent.storage import CoreDataError
    async def scenario():
        repo, _, _, service = await fixture(tmp_path, lambda _event: asyncio.sleep(0))
        def fail():
            raise CoreDataError("app_update_checkpoint_failed", "Synthetic checkpoint busy")
        monkeypatch.setattr(repo, "flush_for_app_update", fail)
        response = await rpc(service, "app.update.prepare", update_id="busy-checkpoint")
        assert response["error"]["code"] == "app_update_checkpoint_failed"
        assert service.update_prepared_id is None
        monkeypatch.setattr(service, "start", lambda _params: asyncio.sleep(0, result={"admitted": True}))
        assert (await rpc(service, "model.run.start"))["result"]["admitted"] is True
    asyncio.run(scenario())


def test_background_compaction_also_delays_installation(tmp_path):
    async def scenario():
        _, _, _, service = await fixture(tmp_path, lambda _event: asyncio.sleep(0))
        release = asyncio.Event()
        task = asyncio.create_task(release.wait())
        service.compaction_tasks["synthetic"] = task
        response = await rpc(service, "app.update.prepare", update_id="compacting")
        assert response["result"]["ready"] is False
        assert response["result"]["active_compactions"] == 1
        release.set()
        await task
        assert (await rpc(service, "app.update.prepare", update_id="compacting"))["result"]["ready"] is True
    asyncio.run(scenario())
