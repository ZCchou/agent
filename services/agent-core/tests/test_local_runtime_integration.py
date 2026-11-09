import asyncio
import json
from pathlib import Path

from agent.protocol import handle_message, handle_async_message
from agent.runtime import ChatRunService
from agent.rag.service import RagService
from agent.storage import CoreRepository
from test_rag_service import Engines, ready, wait_job


def test_local_runtime_contract_upgrades_host_and_core_together():
    response = handle_message({"v": 21, "id": "contract", "type": "ping", "params": {}})
    assert response["v"] == 21
    assert response["result"]["schema_version"] == 28
    assert response["result"]["build_id"] == "schema28-rag-workspace-v1"
    root = Path(__file__).resolve().parents[3]
    host = (root / "apps/agent-desktop/src-tauri/src/core_bridge.rs").read_text(encoding="utf-8")
    assert "PROTOCOL_VERSION: u64 = 21;" in host
    assert "EXPECTED_SCHEMA_VERSION: u64 = 28;" in host
    assert '"schema28-rag-workspace-v1"' in host


def test_schema24_upgrade_keeps_project_dialogue_knowledge_and_rag(tmp_path):
    async def scenario():
        core = CoreRepository(tmp_path / "agent.db")
        project = core.create_project("适航", "work")
        conversation = core.create_conversation("project_work", "资料", project_id=project["id"])
        run = core.start_model_run(content="安全性要求", idempotency_key="migration", provider_profile_id="local",
            model_id="model", conversation_id=conversation["id"])
        proposal = core.propose_knowledge(conversation["id"], "fact", "项目事实", "项目采用R1", run["user_message_id"])
        core.publish_knowledge(proposal["id"])
        rag = RagService(core, Engines())
        library = (await rag.handle("library.create", {"name": "规章", "embedding_model_id": "embed", "reranker_model_id": "rank"}))["library"]["id"]
        version = await ready(rag, library)
        await rag.handle("library.bind", {"library_id": library, "project_id": project["id"], "enabled": True})
        assert (await wait_job(rag, await rag.handle("index.build", {"library_id": library})))["status"] == "completed"
        evidence = (await rag.search(project["id"], "失效"))["evidence"][0]
        await rag.close()
        tables = ["projects", "conversations", "messages", "project_knowledge", "project_knowledge_revisions", "rag_libraries", "rag_versions",
                  "rag_chunks", "rag_bindings", "rag_indexes", "rag_index_chunks", "rag_evidence"]
        with core._connect() as connection:
            before = {table: [tuple(row) for row in connection.execute(f'SELECT * FROM "{table}"')]
                      for table in tables}
            for table in ("local_model_jobs", "local_model_settings", "local_models"):
                connection.execute(f'DROP TABLE IF EXISTS "{table}"')
            connection.execute("UPDATE schema_info SET version=24")
        upgraded = CoreRepository(tmp_path / "agent.db")
        with upgraded._connect() as connection:
            assert connection.execute("SELECT version FROM schema_info").fetchone()[0] == 28
            assert {table: [tuple(row) for row in connection.execute(f'SELECT * FROM "{table}"')]
                    for table in tables} == before
            assert connection.execute("SELECT COUNT(*) FROM local_models").fetchone()[0] == 0
            assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        backups = list(tmp_path.glob("agent.db.pre-schema28-*.bak"))
        assert len(backups) == 1
        service = RagService(upgraded, Engines())
        assert service.read_evidence(project["id"], evidence["id"])["version_id"] == version
        await service.close()
    asyncio.run(scenario())


def test_managed_model_management_uses_public_core_ipc_without_credentials(tmp_path):
    async def scenario():
        async def sink(event):
            pass
        service = ChatRunService(CoreRepository(tmp_path / "agent.db"), sink, offline=True)
        response = await handle_async_message({"v": 21, "id": "models", "type": "local_model.request",
            "params": {"action": "model.list", "params": {}}}, service)
        assert response.get("result", {}).get("models") == [], response
        assert "root" in response["result"]
        assert "api_key" not in json.dumps(response)
        await service.shutdown()
    asyncio.run(scenario())


def test_runtime_exposes_managed_default_and_protects_its_private_transport(tmp_path):
    from agent.providers import ProviderProfile
    class Models:
        def __init__(self):
            self.profile = ProviderProfile("managed:model", "Managed", "openai_compatible", "http://127.0.0.1:12345/v1",
                api_key="private-session-token", local_strict=True, allowed_model_ids=("model",), automatic_model_ids=("model",))
        def provider_profiles(self):
            return {self.profile.id: self.profile}
        def get_profile(self, identity):
            assert identity == self.profile.id
            return self.profile
        def default_provider_id(self):
            return self.profile.id
        def owns_profile(self, identity):
            return identity.startswith("managed:")
        async def close(self):
            pass
    async def scenario():
        async def sink(event):
            pass
        service = ChatRunService(CoreRepository(tmp_path / "agent.db"), sink, offline=True)
        await service.local_models.close()
        service.local_models = Models()
        service.configure_routing({"new_conversation_mode": "automatic"})
        assert service.routing_preferences["global_default_provider_profile_id"] == "managed:model"
        assert service._profile("managed:model").api_key == "private-session-token"
        public = service.profile_status("managed:model")
        assert public["source"] == "managed_local"
        assert "12345" not in json.dumps(public)
        assert "private-session-token" not in json.dumps(public)
        await service.shutdown()
    asyncio.run(scenario())


def test_verified_managed_tool_probe_updates_catalog_capability(tmp_path, monkeypatch):
    from agent.providers import ProviderEvent, ProviderProfile
    from agent.tool_contracts import ToolCall
    class Models:
        def __init__(self):
            self.saved = []
        def set_tool_mode(self, identity, mode, verified=True):
            self.saved.append((identity, mode, verified))
        def provider_profiles(self):
            return {}
        def default_provider_id(self):
            return None
        async def close(self):
            pass
    class Provider:
        async def stream_events(self, *args, **kwargs):
            yield ProviderEvent(kind="tool_call", tool_call=ToolCall("probe", "fs.list", {"path": "."}))
    async def scenario():
        service = ChatRunService(CoreRepository(tmp_path / "agent.db"), lambda event: asyncio.sleep(0))
        await service.local_models.close()
        models = service.local_models = Models()
        monkeypatch.setattr(service, "_profile", lambda _: ProviderProfile("managed:model", "Managed", "openai_compatible",
            "http://127.0.0.1:12345/v1", allowed_model_ids=("model",)))
        monkeypatch.setattr(service, "_provider", lambda _: Provider())
        assert await service.probe_tool_capability("managed:model", "model") == {"tool_mode": "native", "verified": True}
        assert models.saved == [("managed:model", "native", True)]
        await service.shutdown()
    asyncio.run(scenario())


def test_update_preparation_blocks_model_and_document_mutations_but_allows_status(tmp_path):
    async def scenario():
        service = ChatRunService(CoreRepository(tmp_path / "agent.db"), lambda event: asyncio.sleep(0))
        async def rpc(kind, params):
            return await handle_async_message({"v": 21, "id": "update", "type": kind, "params": params}, service)
        assert (await rpc("app.update.prepare", {"update_id": "upgrade"}))["result"]["ready"]
        for kind, action in (("local_model.request", "model.import"), ("local_model.request", "runtime.load"),
                             ("rag.request", "document.import"), ("rag.request", "index.build")):
            response = await rpc(kind, {"action": action, "params": {}})
            assert response["error"]["code"] == "app_update_installing", response
        assert "result" in await rpc("local_model.request", {"action": "model.list", "params": {}})
        await service.shutdown()
    asyncio.run(scenario())


def test_background_model_and_rag_jobs_prevent_update(tmp_path):
    core = CoreRepository(tmp_path / "agent.db")
    with core._connect() as connection:
        connection.execute("INSERT INTO local_model_jobs(id,kind,status,created_at,updated_at) VALUES ('import','import','running','now','now')")
        connection.execute("INSERT INTO rag_libraries(id,name,embedding_model_id,reranker_model_id,created_at) VALUES ('library','library','embed','rank','now')")
        connection.execute("INSERT INTO rag_jobs(id,kind,library_id,status,created_at,updated_at) VALUES ('index','index.build','library','queued','now','now')")
    state = core.app_update_readiness()
    assert state["active_local_model_jobs"] == 1
    assert state["active_rag_jobs"] == 1
