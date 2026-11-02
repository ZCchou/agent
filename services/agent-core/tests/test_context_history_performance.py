"""Golden history, bounded SQL reads and worker authority regressions (offline)."""
import asyncio
import hashlib
import json
import sqlite3
import threading
import time

import pytest

from agent.context_engine.history import HistoryTurn, reconstruct_turns
from agent.context_engine.service import ContextEngineService
from agent.context_engine.domain import ContextEngineError
from agent.repositories.context_repository import ContextRepository
from agent.storage import CoreRepository
from agent.runtime import ChatRunService


def message(identifier, role, sequence, content=None):
    return dict(id=identifier, role=role, sequence_no=sequence, content=content or identifier)


def seed_history(tmp_path, covered=0, tail=100, partial=False):
    repo = CoreRepository(tmp_path / "history.db")
    conversation = repo.create_conversation("standard_chat", "History", "casual")
    with repo._connect() as connection:
        connection.executemany(
            "INSERT INTO messages(id,conversation_id,role,content,created_at,sequence_no) VALUES(?,?,?,?,?,?)",
            [(f"m{i}", conversation["id"], "user" if i % 2 else "assistant",
              "prefix🙂suffix" if i == covered else f"fact {i}", "2026", i)
             for i in range(1, covered + tail + 1)],
        )
        if covered:
            connection.execute(
                """INSERT INTO context_checkpoints(id,conversation_id,start_sequence,through_sequence,
                    through_offset,content_json,source_hash,provider_profile_id,model_id,policy_version,status,created_at)
                    VALUES('cp',?,1,?,?, '{}','hash','builtin-demo','demo','v2','valid','2026')""",
                (conversation["id"], covered, 7 if partial else None),
            )
    run = repo.start_model_run(conversation_id=conversation["id"], content="fact 1",
        idempotency_key="current", provider_profile_id="builtin-demo", model_id="demo")
    return repo, run


def test_retry_golden_is_deterministic_and_never_reintroduces_superseded_branch():
    messages = [message("u", "user", 1), message("old", "assistant", 2),
        message("new", "assistant", 3), message("legacy-u", "user", 4),
        message("legacy-a", "assistant", 5), message("orphan", "assistant", 6),
        message("event", "system_event", 7)]
    runs = [dict(id="new-run", status="completed", user_message_id="u", assistant_message_id="new", finished_at="2026"),
        dict(id="old-run", status="completed", user_message_id="u", assistant_message_id="old", finished_at="2025"),
        dict(id="missing-run", status="completed", user_message_id="missing", assistant_message_id="orphan", finished_at="2026")]
    expected = [HistoryTurn((messages[0], messages[2]), True),
        HistoryTurn((messages[3], messages[4]), True, True), HistoryTurn((messages[5],), False, True)]
    assert reconstruct_turns(messages, runs) == expected
    assert reconstruct_turns(messages, list(reversed(runs))) == expected


def test_superseded_assistant_is_not_resurrected_when_winner_is_outside_replay():
    messages = [message("old", "assistant", 2)]
    runs = [dict(id="r1", status="completed", user_message_id="u", assistant_message_id="old", finished_at="2025"),
            dict(id="r2", status="completed", user_message_id="u", assistant_message_id="new", finished_at="2026")]
    assert reconstruct_turns(messages, runs) == []


@pytest.mark.parametrize("partial", [False, True])
def test_covered_history_loads_only_tail_and_partial_boundary(tmp_path, partial):
    repo, run = seed_history(tmp_path, covered=20_000, tail=100, partial=partial)
    sources = ContextRepository(repo).load_context_sources(run["id"])
    rows = sources["conversation"]["messages"]
    loaded_tail_rows = len([row for row in rows if row["id"] != run["user_message_id"]])
    assert loaded_tail_rows <= 101
    after = ContextEngineService._after_checkpoint(rows, sources["checkpoint"])
    assert len(after) == 101 + int(partial)
    if partial:
        assert after[0]["content"] == "suffix"
        assert after[0]["_source_start"] == 7
    assert sources["performance"]["messages_processed"] <= 102


def test_assemble_never_silently_limits_uncompacted_tail(tmp_path):
    repo, run = seed_history(tmp_path, tail=600)
    sources = ContextRepository(repo).load_context_sources(run["id"])
    assert len(sources["conversation"]["messages"]) == 601
    with pytest.raises(ValueError):
        ContextRepository(repo).load_context_sources(run["id"], limit=10)


def test_compaction_reads_bounded_prefix_and_expanded_reads_covered_region(tmp_path):
    repo, run = seed_history(tmp_path, covered=20, tail=600)
    context = ContextRepository(repo)
    compact = context.load_context_sources(run["id"], purpose="compact", limit=16)
    assert [row["id"] for row in compact["conversation"]["messages"]][:16] == [f"m{i}" for i in range(21,37)]
    assert len(compact["conversation"]["messages"]) == 17
    with repo._connect() as connection:
        connection.execute("UPDATE messages SET content='needle' WHERE id IN (?, ?)", ("m1", run["user_message_id"]))
    expanded = context.load_context_sources(run["id"], purpose="expanded_retry")
    assert len(expanded["replay_messages"]) <= 6
    assert {row["id"] for row in expanded["replay_messages"]} >= {"m1", "m19", "m20"}
    assert all(row["sequence_no"] <= 20 for row in expanded["replay_messages"])


def test_compaction_event_rows_cannot_starve_real_history(tmp_path):
    repo, run = seed_history(tmp_path, tail=300)
    with repo._connect() as connection:
        connection.execute("UPDATE messages SET role='system_event' WHERE sequence_no<=280")
    sources = ContextRepository(repo).load_context_sources(run["id"], purpose="compact", limit=16)
    assert sources["conversation"]["messages"][0]["id"] == "m281"


def test_replay_loads_sibling_run_authority_even_when_user_is_not_selected(tmp_path):
    repo, run = seed_history(tmp_path, covered=20, tail=2)
    with repo._connect() as connection:
        connection.execute("UPDATE messages SET content='needle' WHERE id IN ('m2', ?)", (run["user_message_id"],))
        connection.executemany(
            """INSERT INTO model_runs(id,conversation_id,user_message_id,assistant_message_id,status,
               provider_profile_id,model_id,created_at,finished_at) VALUES(?,?, 'm1',?,'completed','builtin-demo','demo','2026',?)""",
            [("first", run["conversation_id"], "m2", "2025"), ("second", run["conversation_id"], "m4", "2026")],
        )
    sources = ContextRepository(repo).load_context_sources(run["id"], purpose="expanded_retry")
    assert {item["id"] for item in sources["runs"]} >= {"first", "second"}
    assert all(item["id"] != "m2" for turn in reconstruct_turns(sources["replay_messages"], sources["runs"]) for item in turn.messages)


@pytest.mark.parametrize("changed", ["generation", "revision"])
def test_worker_rechecks_generation_and_keeps_event_loop_responsive(tmp_path, monkeypatch, changed):
    repo, run = seed_history(tmp_path, tail=2)
    service = ContextEngineService(repo)
    entered, release = threading.Event(), threading.Event()
    original = service._assemble_once
    def delayed(*args, **kwargs):
        result = original(*args, **kwargs)
        entered.set()
        assert release.wait(3)
        return result
    monkeypatch.setattr(service, "_assemble_once", delayed)
    async def scenario():
        task = asyncio.create_task(service.assemble_async(run))
        try:
            deadline = time.monotonic() + 2
            while not entered.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(.005)
            assert entered.is_set()
            with repo._connect() as connection:
                if changed == "generation":
                    connection.execute("UPDATE conversations SET context_generation=context_generation+1 WHERE id=?", (run["conversation_id"],))
                else:
                    connection.execute("UPDATE global_instructions SET revision=revision+1 WHERE id=1")
            release.set()
            with pytest.raises(ContextEngineError) as error:
                await task
            assert error.value.code == "context_source_changed"
        finally:
            release.set()
            if not task.done():
                task.cancel()
    asyncio.run(scenario())


def test_source_snapshot_never_mixes_generation_with_later_tail(tmp_path, monkeypatch):
    repo, run = seed_history(tmp_path, covered=20, tail=2)
    original = repo._get_conversation_header_with_connection
    def change_after_header(connection, conversation_id):
        header = original(connection, conversation_id)
        with repo._connect() as writer:
            writer.execute("UPDATE messages SET content='new generation' WHERE id='m21'")
            writer.execute("UPDATE conversations SET context_generation=context_generation+1 WHERE id=?", (conversation_id,))
        return header
    monkeypatch.setattr(repo, "_get_conversation_header_with_connection", change_after_header)
    sources = ContextRepository(repo).load_context_sources(run["id"])
    assert sources["conversation"]["messages"][0]["content"] == "fact 21"
    assert sources["context_generation"] == sources["conversation"]["context_generation"]
    assert ContextRepository(repo).context_generation(run["conversation_id"]) == sources["context_generation"] + 1


@pytest.mark.parametrize("missing", [False, True])
def test_worker_source_connection_closes_on_success_and_failure(tmp_path, monkeypatch, missing):
    repo, run = seed_history(tmp_path, tail=2)
    context = ContextRepository(repo)
    connection = context._connect()
    monkeypatch.setattr(context, "_connect", lambda: connection)
    if missing:
        from agent.storage import CoreDataError
        with pytest.raises(CoreDataError):
            context.load_context_sources("missing")
    else:
        context.load_context_sources(run["id"])
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


def test_old_selected_reference_and_latest_manifest_survive_bounded_reads(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    repo, old_run = seed_history(tmp_path, covered=20_000, tail=100)
    repo.transition_model_run(old_run["id"], {"queued"}, "cancelled")
    body = "SELECTED excluded suffix"
    (tmp_path / "selected.txt").write_text(body, encoding="utf-8")
    with repo._connect() as connection:
        connection.executemany(
            """INSERT INTO message_attachments(id,message_id,kind,name,media_type,byte_size,sha256,storage_key,created_at)
               VALUES(?,?,'text',?,'text/plain',?,?,?,'2026')""",
            [(f"att{i}", f"m{i}", f"name{i}.txt", len(body), hashlib.sha256(body.encode()).hexdigest(),
              "selected.txt" if i == 1 else "UNREADABLE_UNSELECTED") for i in range(1, 41)],
        )
    async def scenario():
        async def sink(event):
            pass
        service = ChatRunService(repo, sink)
        result = await service.start(dict(conversation_id=old_run["conversation_id"], content="read selected",
            idempotency_key="selected", provider_profile_id="builtin-demo", model_id="demo",
            attachment_references=[dict(attachment_id="att1", source_start=0, source_end=8)]))
        run = result["run"]
        task = service.run_tasks.pop(run["id"])
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        repo._insert_message(run["conversation_id"], "user", "future")
        with repo._connect() as connection:
            future_id = connection.execute("SELECT id FROM messages WHERE conversation_id=? ORDER BY sequence_no DESC LIMIT 1", (run["conversation_id"],)).fetchone()[0]
            connection.execute("INSERT INTO message_attachments(id,message_id,kind,name,media_type,byte_size,sha256,storage_key,created_at) VALUES('future',?,'text','FUTURE','text/plain',1,'hash','MISSING','2027')", (future_id,))
        sources = service.context.context_repository.load_context_sources(run["id"])
        manifest = sources["reference_manifest"]
        assert len(manifest) == 32
        assert [item["attachment_id"] for item in manifest] == [f"att{i}" for i in range(9, 41)]
        packed = service.context.assemble(run, context_window=32768)
        prompt = json.dumps(packed.messages)
        assert "SELECTED" in prompt and "excluded suffix" not in prompt
        assert "FUTURE" not in prompt and "UNREADABLE_UNSELECTED" not in prompt
        item = next(item for item in packed.items if item.source.source_type == "attachment")
        assert (item.metadata["source_attachment_id"], item.source.source_start, item.source.source_end) == ("att1", 0, 8)
    asyncio.run(scenario())


def test_expanded_partial_boundary_is_replayed_once(tmp_path):
    repo, run = seed_history(tmp_path, covered=20, tail=2, partial=True)
    repo.transition_model_run(run["id"], {"queued"}, "cancelled")
    retry = repo.create_expanded_retry(run["id"])
    packed = ContextEngineService(repo).assemble(retry)
    prompt = json.dumps(packed.messages, ensure_ascii=False)
    assert prompt.count("prefix🙂suffix") == 1
    assert prompt.count("suffix") == 1


@pytest.mark.parametrize("compact", [False, True])
def test_runtime_executes_without_loading_ui_conversation_history(tmp_path, monkeypatch, compact):
    repo, old = seed_history(tmp_path, tail=2)
    repo.transition_model_run(old["id"], {"queued"}, "cancelled")
    async def scenario():
        async def sink(event):
            pass
        service = ChatRunService(repo, sink)
        if compact:
            from test_context_compaction_lifecycle import CheckpointProvider
            original_assemble = service.context._assemble_once
            original_compact = service.context.compact_once
            attempts = []
            def require_compaction_once(*args, **kwargs):
                if not attempts:
                    attempts.append(True)
                    raise ContextEngineError("context_compaction_required", "required")
                return original_assemble(*args, **kwargs)
            async def offline_compact(run, provider, **kwargs):
                return await original_compact(run, CheckpointProvider(), **kwargs)
            monkeypatch.setattr(service.context, "_assemble_once", require_compaction_once)
            monkeypatch.setattr(service.context, "compact_once", offline_compact)
        result = await service.start(dict(conversation_id=old["conversation_id"], content="hello",
            idempotency_key="execute", provider_profile_id="builtin-demo", model_id="demo"))
        def forbidden(*args, **kwargs):
            raise AssertionError("execution loaded UI history")
        monkeypatch.setattr(repo, "get_conversation", forbidden)
        await service.run_tasks[result["run"]["id"]]
        completed = repo.get_model_run(result["run"]["id"])
        assert completed["status"] == "completed", completed.get("error_code")
    asyncio.run(scenario())


def test_cancelled_worker_keeps_its_slot_until_cpu_job_finishes():
    from agent.context_engine.service import run_context_work
    entered = [threading.Event(), threading.Event(), threading.Event()]
    release = threading.Event()
    def work(index):
        entered[index].set()
        assert release.wait(3)
        return index
    async def scenario():
        first = asyncio.create_task(run_context_work(work, 0))
        second = asyncio.create_task(run_context_work(work, 1))
        third = None
        try:
            async with asyncio.timeout(2):
                while not all(event.is_set() for event in entered[:2]):
                    await asyncio.sleep(.005)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            third = asyncio.create_task(run_context_work(work, 2))
            await asyncio.sleep(.025)
            assert not entered[2].is_set()
        finally:
            release.set()
            await second
            if third:
                assert await third == 2
    asyncio.run(scenario())


def test_cancel_during_assembly_never_persists_or_dispatches_late(tmp_path, monkeypatch):
    repo, old = seed_history(tmp_path, tail=2)
    repo.transition_model_run(old["id"], {"queued"}, "cancelled")
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    async def scenario():
        events = []
        async def sink(event):
            events.append(event)
        service = ChatRunService(repo, sink)
        original = service.context._assemble_once
        def delayed(*args, **kwargs):
            result = original(*args, **kwargs)
            entered.set()
            try:
                assert release.wait(3)
                return result
            finally:
                finished.set()
        monkeypatch.setattr(service.context, "_assemble_once", delayed)
        result = await service.start(dict(conversation_id=old["conversation_id"], content="hello",
            idempotency_key="cancel", provider_profile_id="builtin-demo", model_id="demo"))
        run = result["run"]
        try:
            async with asyncio.timeout(2):
                while not entered.is_set():
                    await asyncio.sleep(.005)
            started = time.perf_counter()
            await service.cancel(run["id"])
            assert time.perf_counter() - started < 1
        finally:
            release.set()
        async with asyncio.timeout(2):
            while not finished.is_set():
                await asyncio.sleep(.005)
        assert repo.get_model_run(run["id"])["status"] == "cancelled"
        assert service.context.context_repository.preview(run["id"]) is None
        assert not any(event["event_type"] == "run.invocation_dispatched" for event in events)
    asyncio.run(scenario())
