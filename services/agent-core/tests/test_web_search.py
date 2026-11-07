from __future__ import annotations

import asyncio
import sqlite3
import json
from pathlib import Path

import pytest

from agent.storage import CoreDataError, CoreRepository
from agent.providers import ProviderProfile, validate_profile
from agent.web_search.domain import (
    NativeSearchStatus,
    WebSearchCandidate,
    WebSearchRequestMode,
    WebSearchRoute,
    WebSearchPolicyError,
)
from agent.web_search.routing import resolve_web_search_route
from agent.web_search.service import WebSearchService
from agent.web_search.native_adapters import (
    _anthropic_messages, _anthropic_search_tool_choice,
    _is_structured_native_search_unsupported, _tool_call_from_block,
    stream_anthropic_web_search,
)
from agent.web_search import native_adapters
from agent.providers import _anthropic_function_tools


def test_schema_v10_adds_web_policy_without_losing_records(tmp_path) -> None:
    path = tmp_path / "v9.db"
    CoreRepository(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version FROM schema_info").fetchone()[0] == 24
        project_columns = {row[1] for row in connection.execute("PRAGMA table_info(projects)")}
        conversation_columns = {row[1] for row in connection.execute("PRAGMA table_info(conversations)")}
        assert "web_access_policy" in project_columns
        assert "web_search_mode" in conversation_columns
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN ('web_operations','web_sources')"
        ).fetchone()[0] == 2


def test_web_search_pure_policy_modules_do_not_import_storage_provider_or_http() -> None:
    package = Path(__file__).parents[1] / "src" / "agent" / "web_search"
    for name in ("domain.py", "policy.py", "routing.py"):
        source = (package / name).read_text(encoding="utf-8")
        assert "agent.storage" not in source
        assert "agent.providers" not in source
        assert "httpx" not in source


def test_web_search_contract_matches_python_domain() -> None:
    contract = json.loads(
        (Path(__file__).parents[3] / "contracts" / "web-search-v2.json").read_text(encoding="utf-8")
    )
    assert set(contract["routes"]) == {item.value for item in WebSearchRoute}
    from agent.web_search.domain import WebOperationStatus
    assert set(contract["operation_statuses"]) == {item.value for item in WebOperationStatus}


def test_real_v9_to_v10_migration_preserves_projects_conversations_messages_and_runs(tmp_path) -> None:
    path = tmp_path / "real-v9.db"
    repository = CoreRepository(path)
    project = repository.create_project("Research", "research")
    conversation = repository.create_conversation(
        "project_work", "Sources", project_id=project["id"],
    )
    repository.start_model_run(
        content="Find sources", idempotency_key="migration-run",
        provider_profile_id="builtin-demo", model_id="demo",
        conversation_id=conversation["id"],
    )
    with sqlite3.connect(path) as connection:
        before = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("projects", "conversations", "messages", "model_runs")
        }
        connection.executescript("""
            DROP TABLE web_sources;
            DROP TABLE web_operations;
            ALTER TABLE projects DROP COLUMN web_access_policy;
            ALTER TABLE conversations DROP COLUMN web_search_mode;
            ALTER TABLE model_runs DROP COLUMN web_search_policy_version;
            ALTER TABLE model_runs DROP COLUMN web_search_reason_code;
            ALTER TABLE model_runs DROP COLUMN web_search_route;
            ALTER TABLE model_runs DROP COLUMN web_search_request_mode;
            UPDATE schema_info SET version = 9;
        """)

    CoreRepository(path)

    with sqlite3.connect(path) as connection:
        after = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("projects", "conversations", "messages", "model_runs")
        }
        assert after == before
        assert connection.execute("SELECT version FROM schema_info").fetchone()[0] == 24
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_route_prefers_verified_native_search() -> None:
    decision = resolve_web_search_route(WebSearchRequestMode.AUTO, WebSearchCandidate(
        native_status=NativeSearchStatus.VERIFIED,
        native_adapter="kimi_builtin_web_search",
        native_compatible=True,
        host_backend_id="tavily",
        tool_mode="native",
    ))
    assert decision.route is WebSearchRoute.NATIVE


def test_route_accepts_declared_official_native_search_without_probe() -> None:
    decision = resolve_web_search_route(WebSearchRequestMode.AUTO, WebSearchCandidate(
        native_status=NativeSearchStatus.DECLARED,
        native_adapter="anthropic_messages_web_search",
        native_compatible=True,
        host_backend_id=None,
        tool_mode="native",
    ))
    assert decision.route is WebSearchRoute.NATIVE
    assert decision.reason_code == "native_search_declared"


def test_web_search_service_preserves_domain_error_without_storage_dependency() -> None:
    service = WebSearchService()

    with pytest.raises(WebSearchPolicyError) as error:
        service.decide(
            params={
                "web_search_request_mode": "force_once",
                "web_search_global_enabled": True,
            },
            conversation={
                "kind": "standard_chat",
                "web_search_mode": "inherit",
                "web_access_policy": "disabled",
            },
            native_status="unsupported",
            native_adapter="anthropic_messages_web_search",
            native_compatible=True,
            host_backend_id=None,
            tool_mode="native",
        )

    assert error.value.code == "external_search_setup_required"


def test_explicit_search_under_inherited_disabled_global_requires_consent() -> None:
    service = WebSearchService()

    with pytest.raises(WebSearchPolicyError) as error:
        service.authorize(
            params={"web_search_global_enabled": False},
            conversation={
                "kind": "standard_chat", "web_search_mode": "inherit",
                "web_access_policy": "disabled",
            },
            required=True,
        )

    assert error.value.code == "web_privacy_consent_required"


def test_v10_to_v11_changes_off_to_inherit_and_preserves_counts(tmp_path) -> None:
    path = tmp_path / "v10.db"
    repository = CoreRepository(path)
    chat = repository.create_conversation("standard_chat", "Chat", "casual")
    repository.update_conversation_web_search(chat["id"], "off")
    with sqlite3.connect(path) as connection:
        before = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("projects", "conversations", "messages", "model_runs", "web_operations")
        }
        connection.execute("UPDATE schema_info SET version = 10")
    CoreRepository(path)
    with sqlite3.connect(path) as connection:
        after = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("projects", "conversations", "messages", "model_runs", "web_operations")
        }
        mode = connection.execute(
            "SELECT web_search_mode FROM conversations WHERE id = ?", (chat["id"],)
        ).fetchone()[0]
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='conversations'"
        ).fetchone()[0]
    assert before == after
    assert mode == "inherit"
    assert "'inherit','off','auto'" in sql


def test_deepseek_anthropic_binding_is_accepted_as_a_separate_search_protocol() -> None:
    profile = ProviderProfile(
        id="deepseek", name="DeepSeek", kind="openai_compatible",
        base_url="https://api.deepseek.com", primary_protocol="openai_chat",
        native_search={
            "adapter_id": "anthropic_messages_web_search",
            "protocol": "anthropic_messages",
            "endpoint_base_url": "https://api.deepseek.com/anthropic",
        },
    )
    validate_profile(profile)
    assert profile.native_web_adapter == "anthropic_messages_web_search"
    assert profile.native_search_endpoint.endswith("/anthropic")


def test_anthropic_native_search_keeps_allowed_file_tools() -> None:
    definitions = _anthropic_function_tools((
        "fs.list", "fs.read", "fs.propose_changes", "native.web_search",
    ))

    assert [item["name"] for item in definitions] == [
        "fs_list", "fs_read", "fs_propose_changes",
    ]
    assert all("input_schema" in item for item in definitions)


def test_required_anthropic_search_is_a_protocol_level_tool_choice() -> None:
    assert _anthropic_search_tool_choice(True) == {
        "type": "tool", "name": "web_search",
    }
    assert _anthropic_search_tool_choice(False) is None


def test_anthropic_search_keepalive_and_ping_are_normalized_as_activity(monkeypatch) -> None:
    class FakeResponse:
        status_code = 200
        is_error = False
        headers: dict[str, str] = {}

        async def aiter_lines(self):
            for line in (
                ": keepalive",
                "event: ping",
                'data: {"type":"ping"}',
                "event: message_stop",
                'data: {"type":"message_stop"}',
            ):
                yield line

    class StreamContext:
        async def __aenter__(self):
            return FakeResponse()

        async def __aexit__(self, *_args):
            return False

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def stream(self, *_args, **_kwargs):
            return StreamContext()

    monkeypatch.setattr(native_adapters.httpx, "AsyncClient", FakeClient)

    async def collect() -> list[dict]:
        return [event async for event in stream_anthropic_web_search(
            endpoint_base_url="https://example.test/anthropic",
            api_key="secret", model_id="model",
            messages=[{"role": "user", "content": "search"}],
            max_output_tokens=128, force_search=True,
            function_tools=[], enable_web_search=True,
        )]

    assert [event["kind"] for event in asyncio.run(collect())] == [
        "activity", "activity", "finish",
    ]


def test_anthropic_tool_round_preserves_calls_and_merges_results() -> None:
    system, messages = _anthropic_messages([
        {"role": "system", "content": "Keep the workspace safe."},
        {"role": "user", "content": "Read two files."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-a", "type": "function",
                    "function": {"name": "fs_read", "arguments": '{"path":"a.md"}'},
                },
                {
                    "id": "call-b", "type": "function",
                    "function": {"name": "fs_read", "arguments": '{"path":"b.md"}'},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "call-a", "content": "A"},
        {"role": "tool", "tool_call_id": "call-b", "content": "B"},
    ])

    assert system == "Keep the workspace safe."
    assert [item["type"] for item in messages[1]["content"]] == ["tool_use", "tool_use"]
    assert messages[1]["content"][0]["input"] == {"path": "a.md"}
    assert [item["tool_use_id"] for item in messages[2]["content"]] == ["call-a", "call-b"]


def test_anthropic_tool_use_stream_block_becomes_a_normalized_call() -> None:
    call = _tool_call_from_block({
        "type": "tool_use",
        "id": "call-write",
        "name": "fs_propose_changes",
        "input": {},
        "_partial_json": '{"title":"Save report","mutations":[]}',
    })

    assert call == {
        "id": "call-write",
        "name": "fs_propose_changes",
        "arguments": {"title": "Save report", "mutations": []},
    }


def test_native_search_is_not_permanently_rejected_from_unstructured_error_text() -> None:
    assert not _is_structured_native_search_unsupported(
        '{"error":{"type":"invalid_request_error","message":"web search failed"}}'
    )
    assert _is_structured_native_search_unsupported(
        '{"error":{"code":"unsupported_web_search","param":"web_search"}}'
    )


def test_route_uses_host_tool_and_text_only_requires_force() -> None:
    tool = resolve_web_search_route(WebSearchRequestMode.AUTO, WebSearchCandidate(
        native_status=NativeSearchStatus.UNSUPPORTED, native_adapter="none",
        native_compatible=True, host_backend_id="searx", tool_mode="native",
    ))
    assert tool.route is WebSearchRoute.HOST_TOOL
    with pytest.raises(WebSearchPolicyError) as error:
        resolve_web_search_route(WebSearchRequestMode.AUTO, WebSearchCandidate(
            native_status=NativeSearchStatus.UNSUPPORTED, native_adapter="none",
            native_compatible=True, host_backend_id="searx", tool_mode="text_only",
        ))
    assert error.value.code == "native_web_search_unsupported"
    presearch = resolve_web_search_route(WebSearchRequestMode.FORCE_ONCE, WebSearchCandidate(
        native_status=NativeSearchStatus.UNSUPPORTED, native_adapter="none",
        native_compatible=True, host_backend_id="searx", tool_mode="text_only",
    ))
    assert presearch.route is WebSearchRoute.HOST_PRESEARCH
    forced_tool_model = resolve_web_search_route(WebSearchRequestMode.FORCE_ONCE, WebSearchCandidate(
        native_status=NativeSearchStatus.UNSUPPORTED, native_adapter="none",
        native_compatible=True, host_backend_id="searx", tool_mode="native",
    ))
    assert forced_tool_model.route is WebSearchRoute.HOST_PRESEARCH


def test_cancelling_a_run_cancels_its_active_web_operations(tmp_path) -> None:
    repository = CoreRepository(tmp_path / "cancel.db")
    conversation = repository.create_conversation("standard_chat", "Chat", "casual")
    run = repository.start_model_run(
        content="Search for a source",
        idempotency_key="cancel-web-run",
        provider_profile_id="provider",
        model_id="model",
        conversation_id=conversation["id"],
        web_search_request_mode="force_once",
        web_search_route="host_tool",
        web_search_reason_code="forced_host_search",
        web_search_policy_version="web-search-policy-v1",
    )
    operation = repository.create_web_operation(
        run["id"], 1, "cancel-web-operation", "host_tool", "backend", "query"
    )
    repository.transition_web_operation(operation["id"], "dispatched")
    repository.transition_web_operation(operation["id"], "searching")

    cancelled = repository.cancel_active_web_operations(run["id"])

    assert [item["status"] for item in cancelled] == ["cancelled"]
    assert repository.get_web_operation(operation["id"])["error_code"] == "cancelled_by_user"


def test_web_sources_require_https_and_derive_their_domain(tmp_path) -> None:
    repository = CoreRepository(tmp_path / "sources.db")
    conversation = repository.create_conversation("standard_chat", "Chat", "casual")
    run = repository.start_model_run(
        content="Search", idempotency_key="source-run",
        provider_profile_id="provider", model_id="model",
        conversation_id=conversation["id"], web_search_request_mode="force_once",
        web_search_route="host_presearch", web_search_reason_code="forced",
    )
    blocked = repository.create_web_operation(
        run["id"], 1, "blocked-source", "host_presearch", "backend", "query",
    )
    repository.transition_web_operation(blocked["id"], "dispatched")
    with pytest.raises(CoreDataError) as error:
        repository.complete_web_operation(blocked["id"], [{
            "title": "local", "url": "file:///secret", "domain": "forged",
        }])
    assert error.value.code == "web_url_blocked"

    private_address = repository.create_web_operation(
        run["id"], 2, "private-source", "host_presearch", "backend", "query private",
    )
    repository.transition_web_operation(private_address["id"], "dispatched")
    with pytest.raises(CoreDataError) as error:
        repository.complete_web_operation(private_address["id"], [{
            "title": "router", "url": "https://127.0.0.1/admin",
        }])
    assert error.value.code == "web_url_blocked"

    valid = repository.create_web_operation(
        run["id"], 3, "valid-source", "host_presearch", "backend", "query 2",
    )
    repository.transition_web_operation(valid["id"], "dispatched")
    completed = repository.complete_web_operation(valid["id"], [{
        "title": "Example", "url": "https://docs.example.com/page?q=1#section", "domain": "forged",
    }, {
        "title": "Duplicate", "url": "https://docs.example.com/page?q=1", "domain": "forged",
    }])
    assert completed["sources"][0]["domain"] == "docs.example.com"
    assert completed["sources"][0]["trust"] == "external_untrusted"
    assert completed["sources"][0]["url"] == "https://docs.example.com/page?q=1"
    assert completed["evidence_status"] == "traceable_sources"
    assert completed["verified_source_count"] == 1
    assert completed["result_count"] == 1


def test_provider_reference_without_url_is_not_traceable_evidence(tmp_path) -> None:
    repository = CoreRepository(tmp_path / "provider-reference.db")
    conversation = repository.create_conversation("standard_chat", "Chat", "casual")
    run = repository.start_model_run(
        content="Search", idempotency_key="provider-reference-run",
        provider_profile_id="provider", model_id="model",
        conversation_id=conversation["id"], web_search_request_mode="force_once",
        web_search_route="native", web_search_reason_code="forced",
    )
    operation = repository.create_web_operation(
        run["id"], 1, "provider-reference-operation", "native", "adapter", "query",
    )
    repository.transition_web_operation(operation["id"], "dispatched")
    completed = repository.complete_web_operation(
        operation["id"], [], provider_reference_id="provider-search-id",
    )
    assert completed["status"] == "completed"
    assert completed["evidence_status"] == "provider_reference"
    assert completed["verified_source_count"] == 0


def test_v16_to_v17_backfills_evidence_without_changing_records(tmp_path) -> None:
    path = tmp_path / "v16-evidence.db"
    repository = CoreRepository(path)
    conversation = repository.create_conversation("standard_chat", "Chat", "casual")
    run = repository.start_model_run(
        content="Search", idempotency_key="v16-evidence-run",
        provider_profile_id="provider", model_id="model",
        conversation_id=conversation["id"], web_search_request_mode="force_once",
        web_search_route="native", web_search_reason_code="forced",
    )
    operation = repository.create_web_operation(
        run["id"], 1, "v16-evidence-operation", "native", "adapter", "query",
    )
    repository.transition_web_operation(operation["id"], "dispatched")
    repository.complete_web_operation(operation["id"], [{
        "title": "Source", "url": "https://example.com/source",
    }])
    zero_source_conversation = repository.create_conversation(
        "standard_chat", "Zero source", "research",
    )
    zero_source_run = repository.start_model_run(
        content="Search again", idempotency_key="v16-zero-source-run",
        provider_profile_id="provider", model_id="model",
        conversation_id=zero_source_conversation["id"], web_search_request_mode="force_once",
        web_search_route="native", web_search_reason_code="forced",
    )
    zero_source_operation = repository.create_web_operation(
        zero_source_run["id"], 1, "v16-zero-source-operation", "native", "adapter", "query",
    )
    repository.transition_web_operation(zero_source_operation["id"], "dispatched")
    repository.complete_web_operation(zero_source_operation["id"], [])
    with sqlite3.connect(path) as connection:
        before = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("conversations", "messages", "model_runs", "web_operations", "web_sources")
        }
        connection.execute("ALTER TABLE web_operations DROP COLUMN provider_reference_id")
        connection.execute("ALTER TABLE web_operations DROP COLUMN evidence_manifest_hash")
        connection.execute("ALTER TABLE web_operations DROP COLUMN verified_source_count")
        connection.execute("ALTER TABLE web_operations DROP COLUMN evidence_status")
        connection.execute("ALTER TABLE model_runs DROP COLUMN web_grounding_status")
        connection.execute("UPDATE schema_info SET version = 16")

    migrated = CoreRepository(path)
    with sqlite3.connect(path) as connection:
        after = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("conversations", "messages", "model_runs", "web_operations", "web_sources")
        }
        version = connection.execute("SELECT version FROM schema_info").fetchone()[0]
    restored = migrated.get_web_operation(operation["id"])
    restored_zero_source = migrated.get_web_operation(zero_source_operation["id"])
    assert before == after
    assert version == 24
    assert restored["evidence_status"] == "traceable_sources"
    assert restored["verified_source_count"] == 1
    assert restored_zero_source["evidence_status"] == "legacy_unknown"
    assert migrated.get_model_run(zero_source_run["id"])["web_grounding_status"] == "failed"


def test_database_uses_wal_and_waits_for_short_writer_contention(tmp_path) -> None:
    repository = CoreRepository(tmp_path / "wal.db")
    with repository._connect() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5_000


def test_active_ui_read_does_not_block_final_assistant_message_commit(tmp_path) -> None:
    repository = CoreRepository(tmp_path / "concurrent-read.db")
    conversation = repository.create_conversation("standard_chat", "Chat", "casual")
    run = repository.start_model_run(
        content="Search", idempotency_key="concurrent-read-run",
        provider_profile_id="provider", model_id="model",
        conversation_id=conversation["id"],
    )
    repository.transition_model_run(run["id"], {"queued"}, "building_context")
    repository.transition_model_run(run["id"], {"building_context"}, "generating")
    attempt = repository.begin_run_attempt(run["id"])
    reader = repository._connect()
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM model_runs WHERE id = ?", (run["id"],)).fetchone()

        completed = repository.complete_model_run(
            run["id"], attempt["id"], "Search completed safely.",
        )
    finally:
        reader.rollback()
        reader.close()

    assert completed["status"] == "completed"
    assert completed["assistant_message"]["content"] == "Search completed safely."


def test_terminal_run_reconciles_searching_native_operations_on_restart(tmp_path) -> None:
    path = tmp_path / "reconcile.db"
    repository = CoreRepository(path)
    conversation = repository.create_conversation("standard_chat", "Chat", "casual")
    run = repository.start_model_run(
        content="Search", idempotency_key="reconcile-run",
        provider_profile_id="provider", model_id="model",
        conversation_id=conversation["id"], web_search_request_mode="force_once",
        web_search_route="native", web_search_reason_code="native_search_declared",
    )
    repository.transition_model_run(run["id"], {"queued"}, "building_context")
    repository.transition_model_run(run["id"], {"building_context"}, "generating")
    operation = repository.create_web_operation(
        run["id"], 1, "reconcile-operation", "native", "adapter", "Pending query",
    )
    repository.transition_web_operation(operation["id"], "dispatched")
    repository.transition_web_operation(operation["id"], "searching")
    repository.transition_model_run(
        run["id"], {"generating"}, "failed", "persistence_failed", "Could not save.",
    )

    restarted = CoreRepository(path)
    repaired = restarted.get_web_operation(operation["id"])

    assert repaired["status"] == "failed"
    assert repaired["error_code"] == "persistence_failed"
    assert repaired["finished_at"] is not None


def test_native_search_query_can_replace_the_provider_placeholder(tmp_path) -> None:
    repository = CoreRepository(tmp_path / "query.db")
    conversation = repository.create_conversation("standard_chat", "Chat", "casual")
    run = repository.start_model_run(
        content="Search", idempotency_key="query-run",
        provider_profile_id="provider", model_id="model",
        conversation_id=conversation["id"], web_search_request_mode="force_once",
        web_search_route="native", web_search_reason_code="native_search_declared",
    )
    operation = repository.create_web_operation(
        run["id"], 1, "query-operation", "native", "adapter", "Provider-managed search",
    )
    repository.transition_web_operation(operation["id"], "dispatched")
    repository.transition_web_operation(operation["id"], "searching")

    updated = repository.update_web_operation_query(
        operation["id"], "latest computer vision papers",
    )

    assert updated["query_text"] == "latest computer vision papers"
