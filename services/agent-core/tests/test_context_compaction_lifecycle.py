import asyncio
import json
import sqlite3

import pytest

from agent.context_engine.domain import ContextEngineError
from agent.context_engine.service import ContextEngineService
from agent.providers import ProviderError, ProviderEvent
from agent.runtime import ChatRunService
from agent.storage import CoreDataError, CoreRepository


def seed(repository):
    conversation = repository.create_conversation("standard_chat", "History", "casual")
    repository._insert_message(conversation["id"], "user", "keep this original fact")
    repository._insert_message(conversation["id"], "assistant", "acknowledged")
    return repository.start_model_run(
        conversation_id=conversation["id"], content="continue", idempotency_key="current",
        provider_profile_id="builtin-demo", model_id="demo",
    )


class CheckpointProvider:
    def __init__(self, errors=()):
        self.errors = list(errors)
        self.calls = []

    async def stream_events(self, model, messages, *, tools, options):
        self.calls.append((model, options.response_format))
        if self.errors:
            error = self.errors.pop(0)
            if isinstance(error, Exception):
                raise error
            yield ProviderEvent("text_delta", text=error)
            return
        source = json.loads(messages[1]["content"])["sources"][0]
        payload = {key: [] for key in (
            "goal", "success_criteria", "constraints", "decisions", "completed",
            "relative_files", "failed_approaches", "open_tasks", "next_step")}
        payload["goal"] = [{"text": "preserved fact", "source_ids": [source["source_id"]], "trust": source["trust"]}]
        yield ProviderEvent("text_delta", text=json.dumps(payload))
        if getattr(self, "usage", None):
            yield ProviderEvent("usage", usage=self.usage)


@pytest.mark.parametrize("previous", [None, "valid", "stale"])
def test_preclaim_budget_failure_reloads_and_explicit_target_recovers_same_request(tmp_path, monkeypatch, previous):
    async def scenario():
        from dataclasses import replace
        from agent.providers import create_provider
        repository = CoreRepository(tmp_path / "preclaim.db")
        old_run = seed(repository)
        context = ContextEngineService(repository)
        checkpoint = await context.compact_once(old_run, CheckpointProvider(), context_window=32768, max_output_tokens=512)
        payload = checkpoint["content"]
        payload["goal"][0]["text"] = "😀" * 6000
        with context.context_repository._connect() as connection:
            connection.execute("UPDATE context_checkpoints SET content_json=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), checkpoint["id"]))
            if previous is None:
                connection.execute("DELETE FROM context_compaction_attempts")
            else:
                connection.execute("UPDATE context_compaction_attempts SET status=?", (previous,))
        repository.transition_model_run(old_run["id"], {"queued"}, "cancelled")
        repository._insert_message(old_run["conversation_id"], "assistant", "later history " * 2000)
        events = []
        async def sink(event):
            events.append(event)
        service = ChatRunService(repository, sink)
        started = await service.start({"conversation_id": old_run["conversation_id"], "content": "continue this request",
                                       "idempotency_key": "later", "provider_profile_id": "builtin-demo", "model_id": "demo"})
        run = started["run"]
        queued = service.run_tasks.pop(run["id"])
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        service.profiles["builtin-demo"] = replace(service.profiles["builtin-demo"], model_capabilities={"demo": {"context_length": 8192, "max_output_tokens": 512}})
        run = repository.transition_model_run(run["id"], {"queued"}, "building_context")
        provider = CheckpointProvider()
        assert await service._prepare_context(run, service.profiles["builtin-demo"], provider, round_no=0) is None
        assert provider.calls == []
        assert repository.get_model_run(run["id"])["status"] == "waiting_context"
        status = service.compaction_status(run["conversation_id"])
        assert status is not None and status["status"] == "failed"
        assert status["run_id"] == run["id"]
        assert status["error_code"] == "context_compaction_budget_exhausted"
        assert status["source_hash"] == "" and status["claim_token"] is None
        assert json.loads(status["budget_policy_json"])["phase"] == "preparation"
        assert context.context_repository.recoverable_compactions("builtin-demo") == []
        restarted = ChatRunService(CoreRepository(tmp_path / "preclaim.db"), sink)
        assert restarted.compaction_status(run["conversation_id"])["id"] == status["id"]
        assert restarted.compaction_tasks == {}
        restarted.profiles["replacement"] = replace(restarted.profiles["builtin-demo"], id="replacement", allowed_model_ids=("reliable",),
            model_capabilities={"reliable": {"context_length": 131072, "max_output_tokens": 512}})
        with pytest.raises(CoreDataError):
            await restarted.resume_compaction(run["conversation_id"], {"provider_profile_id": "replacement", "model_id": "forbidden"})
        demo = create_provider(restarted.profiles["builtin-demo"])
        monkeypatch.setattr("agent.runtime.create_provider", lambda profile: provider if profile.id == "replacement" else demo)
        before = repository.get_conversation(run["conversation_id"])["messages"]
        await restarted.resume_compaction(run["conversation_id"], {"provider_profile_id": "replacement", "model_id": "reliable", "context_window": 999999})
        await restarted.compaction_tasks[run["conversation_id"]]
        if run["id"] in restarted.run_tasks:
            await restarted.run_tasks[run["id"]]
        after = repository.get_conversation(run["conversation_id"])["messages"]
        assert repository.get_model_run(run["id"])["status"] == "completed", (repository.get_model_run(run["id"])["error_code"], repository.get_model_run(run["id"])["error_message"])
        assert len(after) == len(before) + 1
        assert [m["id"] for m in after if m["role"] == "user"] == [m["id"] for m in before if m["role"] == "user"]
        assert len(provider.calls) == 1
        assert json.loads(restarted.compaction_status(run["conversation_id"])["target_json"])["context_window"] == 131072
        await restarted.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("code", ["request_timeout", "stream_stalled", "rate_limited"])
def test_provider_error_keeps_first_code_and_finishes_attempt(tmp_path, code):
    async def scenario():
        repository = CoreRepository(tmp_path / "error.db")
        run = seed(repository)
        service = ContextEngineService(repository)
        with pytest.raises(ProviderError) as caught:
            await service.compact_once(run, CheckpointProvider([ProviderError(code, "safe")]),
                                       context_window=32768, max_output_tokens=512)
        assert caught.value.code == code
        status = service.context_repository.compaction_status(run["conversation_id"])
        assert (status["status"], status["error_code"]) == ("failed", code)
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["running", "failed", "valid", "cancelled", "generation", "same_claim_finished"])
def test_late_preparation_failure_cannot_replace_newer_authority(tmp_path, monkeypatch, change):
    async def scenario():
        import threading
        from agent.context_engine.domain import CompactionTarget
        repository = CoreRepository(tmp_path / "late-preparation.db")
        run = seed(repository)
        context = ContextEngineService(repository)
        store = context.context_repository
        generation = store.context_generation(run["conversation_id"])
        target = CompactionTarget("builtin-demo", "demo", "unknown", 32768, 512)
        args = dict(conversation_id=run["conversation_id"], run_id=run["id"], source_hash="synthetic-source",
                    context_generation=generation, target=target, policy_version="test")
        previous = store.claim_compaction(**args) if change == "same_claim_finished" else None
        entered, release = threading.Event(), threading.Event()
        prepare = context._prepare_compaction
        def delayed(*args):
            entered.set()
            assert release.wait(5)
            return prepare(*args)
        monkeypatch.setattr(context, "_prepare_compaction", delayed)
        provider = CheckpointProvider()
        task = asyncio.create_task(context.compact_once(run, provider, context_window=128, max_output_tokens=512))
        assert await asyncio.to_thread(entered.wait, 5)
        try:
            if change in {"running", "failed"}:
                claim = store.claim_compaction(**args)
                if change == "failed":
                    store.fail_compaction(claim.attempt_id, "newer-failure", claim_token=claim.claim_token)
            elif change == "same_claim_finished":
                store.fail_compaction(previous.attempt_id, "newer-failure", claim_token=previous.claim_token)
            elif change == "valid":
                await ContextEngineService(repository).compact_once(run, CheckpointProvider(), context_window=32768, max_output_tokens=512)
            elif change == "cancelled":
                repository.transition_model_run(run["id"], {"queued"}, "cancelled")
            else:
                with store._connect() as connection:
                    connection.execute("UPDATE conversations SET context_generation=context_generation+1 WHERE id=?", (run["conversation_id"],))
            expected = store.compaction_status(run["conversation_id"])
        finally:
            release.set()
        with pytest.raises(ContextEngineError):
            await task
        assert provider.calls == []
        latest = store.compaction_status(run["conversation_id"])
        if change == "generation":
            assert latest["error_code"] == "context_source_changed"
            assert latest["context_generation"] == generation + 1
            assert latest["source_hash"] == "" and latest["claim_token"] is None
        else:
            assert latest == expected
        if change == "cancelled":
            assert repository.get_model_run(run["id"])["status"] == "cancelled"
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["unexpected", "generation", "global_revision"])
def test_complete_preclaim_boundary_exposes_current_recovery_evidence(tmp_path, monkeypatch, failure):
    async def scenario():
        repository = CoreRepository(tmp_path / "preclaim-boundary.db")
        run = seed(repository)
        context = ContextEngineService(repository)
        prepare = context._prepare_compaction
        def changed_prepare(*args):
            if failure == "unexpected":
                raise ValueError("synthetic content must never enter status")
            result = prepare(*args)
            with context.context_repository._connect() as connection:
                if failure == "generation":
                    connection.execute("UPDATE conversations SET context_generation=context_generation+1 WHERE id=?", (run["conversation_id"],))
                else:
                    connection.execute("UPDATE global_instructions SET revision=revision+1 WHERE id=1")
            return result
        monkeypatch.setattr(context, "_prepare_compaction", changed_prepare)
        provider = CheckpointProvider()
        with pytest.raises((ValueError, ContextEngineError)):
            await context.compact_once(run, provider, context_window=32768, max_output_tokens=512)
        status = context.context_repository.compaction_status(run["conversation_id"])
        assert status is not None and status["status"] == "failed"
        assert status["error_code"] == ("context_compaction_internal_error" if failure == "unexpected" else "context_source_changed")
        assert status["context_generation"] == context.context_repository.context_generation(run["conversation_id"])
        assert status["source_hash"] == "" and status["claim_token"] is None
        assert "synthetic content" not in json.dumps(status)
        assert provider.calls == []
    asyncio.run(scenario())


def test_unsupported_json_mode_falls_back_once_without_output(tmp_path):
    async def scenario():
        repository = CoreRepository(tmp_path / "fallback.db")
        run = seed(repository)
        provider = CheckpointProvider([ProviderError("response_format_unsupported", "unsupported")])
        result = await ContextEngineService(repository).compact_once(
            run, provider, context_window=32768, max_output_tokens=512)
        assert result["id"]
        assert provider.calls == [("demo", "json_object"), ("demo", None)]
    asyncio.run(scenario())


def test_two_bad_json_responses_are_terminal_unreliable(tmp_path):
    async def scenario():
        repository = CoreRepository(tmp_path / "json.db")
        run = seed(repository)
        context = ContextEngineService(repository)
        with pytest.raises(ContextEngineError) as caught:
            await context.compact_once(run, CheckpointProvider(["bad json", "bad again"]),
                                       context_window=32768, max_output_tokens=512)
        assert caught.value.code == "context_compaction_unreliable"
        assert context.context_repository.compaction_status(run["conversation_id"])["status"] == "failed"
    asyncio.run(scenario())


def test_cancelled_claim_cannot_commit_or_fail_replacement(tmp_path):
    # Import within test so the missing behavior is reported as a test failure.
    from agent.context_engine import domain
    assert hasattr(domain, "CompactionTarget"), "Claims must bind authoritative targets"
    repository = CoreRepository(tmp_path / "claims.db")
    run = seed(repository)
    context = ContextEngineService(repository).context_repository
    target = domain.CompactionTarget("builtin-demo", "demo", "openai_chat", 32768, 512)
    args = dict(conversation_id=run["conversation_id"], run_id=run["id"], source_hash="hash",
                context_generation=context.context_generation(run["conversation_id"]), target=target, policy_version="test-v1")
    first = context.claim_compaction(**args)
    context.pause_compaction(run["conversation_id"])
    second = context.claim_compaction(**args)
    assert first.claim_token != second.claim_token
    assert context.fail_compaction(first.attempt_id, "late", claim_token=first.claim_token) is False
    assert context.commit_checkpoint(
        attempt_id=first.attempt_id, claim_token=first.claim_token,
        conversation_id=run["conversation_id"], run_id=run["id"], expected_generation=context.context_generation(run["conversation_id"]),
        parent_checkpoint_id=None, start_sequence=1, start_offset=0, through_sequence=2,
        through_offset=None, content={}, source_hash="hash", provider_profile_id="builtin-demo",
        model_id="demo", input_tokens=None, output_tokens=None, estimated_cost=None,
    ) is None
    assert context.compaction_status(run["conversation_id"])["id"] == second.attempt_id
    assert context.active_checkpoint(run["conversation_id"]) is None


def test_database_cleanup_failure_preserves_provider_first_cause(tmp_path, monkeypatch):
    async def scenario():
        repository = CoreRepository(tmp_path / "db-failure.db")
        run = seed(repository)
        context = ContextEngineService(repository)
        def broken(*args, **kwargs):
            raise sqlite3.OperationalError("synthetic unavailable database")
        monkeypatch.setattr(context.context_repository, "fail_compaction", broken)
        with pytest.raises(ProviderError) as caught:
            await context.compact_once(run, CheckpointProvider([ProviderError("rate_limited", "first")]),
                                       context_window=32768, max_output_tokens=512)
        assert caught.value.code == "rate_limited"
        # A restart reconciles the claim if terminal persistence was unavailable.
        restarted = ContextEngineService(repository).context_repository
        restarted.interrupt_compactions()
        assert restarted.compaction_status(run["conversation_id"])["error_code"] == "core_interrupted"
    asyncio.run(scenario())


@pytest.mark.parametrize("initial_status", ["waiting_context", "building_context"])
def test_restart_preserves_context_wait_without_automatic_replay(tmp_path, initial_status):
    async def scenario():
        repository = CoreRepository(tmp_path / "restart.db")
        run = seed(repository)
        repository.transition_model_run(run["id"], {"queued"}, initial_status, "request_timeout")
        context = ContextEngineService(repository).context_repository
        context.begin_compaction(conversation_id=run["conversation_id"], run_id=run["id"],
                                 context_generation=context.context_generation(run["conversation_id"]), source_hash="hash", idempotency_key="old")
        async def sink(event):
            pass
        restarted = ChatRunService(repository, sink)
        assert repository.get_model_run(run["id"])["status"] == "waiting_context"
        assert restarted.compaction_status(run["conversation_id"])["error_code"] == "core_interrupted"
        assert restarted.compaction_tasks == {}
    asyncio.run(scenario())


def test_pause_before_worker_starts_cleans_registration(tmp_path):
    async def scenario():
        repository = CoreRepository(tmp_path / "early-cancel.db")
        async def sink(event):
            pass
        service = ChatRunService(repository, sink)
        run = seed(repository)
        service._launch_compaction(run, service.profiles["builtin-demo"], trigger="post_run")
        await service.pause_compaction(run["conversation_id"])
        assert service.compaction_tasks == {}
        assert service.context.context_repository.compaction_status(run["conversation_id"]) is None
    asyncio.run(scenario())


def test_explicit_target_recovery_uses_authority_and_same_user_request(tmp_path, monkeypatch):
    async def scenario():
        from dataclasses import replace
        repository = CoreRepository(tmp_path / "recover-target.db")
        events = []
        async def sink(event):
            events.append(event)
        service = ChatRunService(repository, sink)
        conversation = repository.create_conversation("standard_chat", "History", "casual")
        repository._insert_message(conversation["id"], "user", "keep this original fact")
        repository._insert_message(conversation["id"], "assistant", "acknowledged")
        started = await service.start({
            "conversation_id": conversation["id"], "content": "continue", "idempotency_key": "recovery",
            "provider_profile_id": "builtin-demo", "model_id": "demo",
        })
        run = started["run"]
        queued_task = service.run_tasks.pop(run["id"])
        queued_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued_task
        real_assemble = service.context._assemble_once
        def require_compaction(*args, **kwargs):
            raise ContextEngineError("context_compaction_required", "required")
        monkeypatch.setattr(service.context, "_assemble_once", require_compaction)
        monkeypatch.setattr("agent.runtime.create_provider", lambda profile: CheckpointProvider(["bad", "bad"]))
        await service._execute(run["id"])
        assert repository.get_model_run(run["id"])["status"] == "waiting_context"
        monkeypatch.setattr(service.context, "_assemble_once", real_assemble)
        service.profiles["replacement"] = replace(
            service.profiles["builtin-demo"], id="replacement", allowed_model_ids=("reliable",),
            model_capabilities={"reliable": {"context_length": 16384, "max_output_tokens": 1024,
                                            "input_price_per_million": 1, "output_price_per_million": 2}},
        )
        provider = CheckpointProvider()
        provider.profile = service.profiles["replacement"]
        provider.usage = {"input_tokens": 1000, "output_tokens": 1000}
        from agent.providers import create_provider
        demo = create_provider(service.profiles["builtin-demo"])
        monkeypatch.setattr("agent.runtime.create_provider", lambda profile: provider if profile.id == "replacement" else demo)
        # Continue through the real run loop using the demo adapter after compaction.
        before = repository.get_conversation(run["conversation_id"])["messages"]
        await service.resume_compaction(run["conversation_id"], {
            "provider_profile_id": "replacement", "model_id": "reliable",
            "context_window": 999999, "max_output_tokens": 999999,
        })
        await service.compaction_tasks[run["conversation_id"]]
        if run["id"] in service.run_tasks:
            await service.run_tasks[run["id"]]
        status = service.context.context_repository.compaction_status(run["conversation_id"])
        target = json.loads(status["target_json"])
        assert target["model_id"] == "reliable"
        assert (target["context_window"], target["max_output_tokens"]) == (16384, 1024)
        assert status["status"] == "valid"
        assert status["estimated_cost"] == 0.003
        after = repository.get_conversation(run["conversation_id"])["messages"]
        assert repository.get_model_run(run["id"])["status"] == "completed"
        assert len(after) == len(before) + 1
        assert [m["id"] for m in after if m["role"] == "user"] == [m["id"] for m in before if m["role"] == "user"]
        assert len([e for e in events if e["event_type"] == "run.context_compaction_completed"]) == 1
    asyncio.run(scenario())


def test_cancel_rejects_provider_that_swallows_task_cancellation(tmp_path, monkeypatch):
    async def scenario():
        repository = CoreRepository(tmp_path / "swallowed-cancel.db")
        events = []
        async def sink(event):
            events.append(event)
        service = ChatRunService(repository, sink)
        conversation = repository.create_conversation("standard_chat", "History", "casual")
        repository._insert_message(conversation["id"], "user", "keep this original fact")
        repository._insert_message(conversation["id"], "assistant", "acknowledged")
        started = await service.start({
            "conversation_id": conversation["id"], "content": "continue", "idempotency_key": "recovery",
            "provider_profile_id": "builtin-demo", "model_id": "demo",
        })
        run = started["run"]
        # Keep the persisted execution plan, but park before the first dispatch.
        queued_task = service.run_tasks.pop(run["id"])
        queued_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued_task
        started = asyncio.Event()
        class SlowProvider(CheckpointProvider):
            async def stream_events(self, *args, **kwargs):
                started.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    pass
                async for event in super().stream_events(*args, **kwargs):
                    yield event
        def require_compaction(*args, **kwargs):
            raise ContextEngineError("context_compaction_required", "required")
        monkeypatch.setattr(service.context, "_assemble_once", require_compaction)
        monkeypatch.setattr("agent.runtime.create_provider", lambda profile: SlowProvider())
        service.run_tasks[run["id"]] = asyncio.create_task(service._execute(run["id"]))
        await started.wait()
        await service.cancel(run["id"])
        assert repository.get_model_run(run["id"])["status"] == "cancelled"
        assert service.context.context_repository.active_checkpoint(run["conversation_id"]) is None
        status = service.context.context_repository.compaction_status(run["conversation_id"])
        assert (status["status"], status["error_code"]) == ("interrupted", "cancelled_by_user")
        assert len([e for e in events if e["event_type"] == "run.context_compaction_failed"]) == 1
    asyncio.run(scenario())


def test_unexpected_run_failure_is_bounded_and_safe(tmp_path, monkeypatch):
    async def scenario():
        repository = CoreRepository(tmp_path / "outer-failure.db")
        events = []
        async def sink(event):
            events.append(event)
        service = ChatRunService(repository, sink)
        run = seed(repository)
        def broken(*args, **kwargs):
            raise RuntimeError("private source content")
        monkeypatch.setattr(service.context, "_assemble_once", broken)
        service.run_tasks[run["id"]] = asyncio.create_task(service._execute(run["id"]))
        await service.run_tasks[run["id"]]
        current = repository.get_model_run(run["id"])
        assert (current["status"], current["error_code"]) == ("failed", "internal_error")
        assert service.run_tasks == {}
        assert len([e for e in events if e["event_type"] == "run.failed"]) == 1
        assert "private source content" not in json.dumps(events)
    asyncio.run(scenario())


def test_event_sink_failure_does_not_escape_outer_task(tmp_path):
    async def scenario():
        repository = CoreRepository(tmp_path / "sink-failure.db")
        async def broken_sink(event):
            raise RuntimeError("synthetic event transport failure")
        service = ChatRunService(repository, broken_sink)
        run = seed(repository)
        service.run_tasks[run["id"]] = asyncio.create_task(service._execute(run["id"]))
        await service.run_tasks[run["id"]]
        assert repository.get_model_run(run["id"])["status"] == "failed"
        assert service.run_tasks == {}
    asyncio.run(scenario())


def test_cancel_before_foreground_task_starts_is_terminal(tmp_path):
    async def scenario():
        repository = CoreRepository(tmp_path / "foreground-early-cancel.db")
        events = []
        async def sink(event):
            events.append(event)
        service = ChatRunService(repository, sink)
        run = seed(repository)
        service.run_tasks[run["id"]] = asyncio.create_task(service._execute(run["id"]))
        await service.cancel(run["id"])
        assert repository.get_model_run(run["id"])["status"] == "cancelled"
        assert service.run_tasks == {}
        assert len([e for e in events if e["event_type"] == "run.cancelled"]) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("saw_output, expected", [(False, "request_timeout"), (True, "stream_stalled")])
def test_real_timeout_paths_converge(tmp_path, monkeypatch, saw_output, expected):
    async def scenario():
        repository = CoreRepository(tmp_path / "timeouts.db")
        run = seed(repository)
        context = ContextEngineService(repository)
        class StalledProvider:
            async def stream_events(self, *args, **kwargs):
                if saw_output:
                    yield ProviderEvent("text_delta", text="{")
                await asyncio.sleep(1)
        real_timeout = asyncio.timeout
        monkeypatch.setattr("agent.context_engine.service.asyncio.timeout", lambda seconds: real_timeout(0.01))
        with pytest.raises(ProviderError) as caught:
            await context.compact_once(run, StalledProvider(), context_window=32768, max_output_tokens=512)
        assert caught.value.code == expected
        assert context.context_repository.compaction_status(run["conversation_id"])["status"] == "failed"
    asyncio.run(scenario())


def test_json_mode_error_after_output_never_replays(tmp_path):
    async def scenario():
        repository = CoreRepository(tmp_path / "partial-json.db")
        run = seed(repository)
        class PartialProvider:
            calls = 0
            async def stream_events(self, *args, **kwargs):
                self.calls += 1
                yield ProviderEvent("text_delta", text="{")
                raise ProviderError("response_format_unsupported", "uncertain output")
        provider = PartialProvider()
        with pytest.raises(ProviderError):
            await ContextEngineService(repository).compact_once(run, provider, context_window=32768, max_output_tokens=512)
        assert provider.calls == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("code", ["cancelled_by_user", "connection_reset", "request_timeout", "context_compaction_unreliable", "provider_unavailable", "network_unavailable"])
def test_cancelled_compaction_is_not_restarted_by_next_send(tmp_path, monkeypatch, code):
    async def scenario():
        repository = CoreRepository(tmp_path / "cancelled-gate.db")
        async def sink(event):
            pass
        service = ChatRunService(repository, sink)
        run = seed(repository)
        context = service.context.context_repository
        context.begin_compaction(conversation_id=run["conversation_id"], run_id=run["id"],
                                 context_generation=context.context_generation(run["conversation_id"]),
                                 source_hash="hash", idempotency_key="cancelled")
        context.pause_compaction(run["conversation_id"], code=code)
        monkeypatch.setattr(service, "_compaction_is_required", lambda status: True)
        before = repository.get_conversation(run["conversation_id"])["messages"]
        with pytest.raises(CoreDataError) as caught:
            await service.start({"conversation_id": run["conversation_id"], "content": "do not persist",
                                 "idempotency_key": "next", "provider_profile_id": "builtin-demo", "model_id": "demo"})
        assert caught.value.code == code
        assert service.compaction_tasks == {}
        assert repository.get_conversation(run["conversation_id"])["messages"] == before
    asyncio.run(scenario())


def test_cancel_run_before_recovery_worker_dispatches(tmp_path, monkeypatch):
    async def scenario():
        repository = CoreRepository(tmp_path / "queued-recovery.db")
        async def sink(event):
            pass
        service = ChatRunService(repository, sink)
        run = seed(repository)
        repository.transition_model_run(run["id"], {"queued"}, "waiting_context")
        provider = CheckpointProvider()
        monkeypatch.setattr("agent.runtime.create_provider", lambda profile: provider)
        service._launch_compaction(run, service.profiles["builtin-demo"], trigger="explicit_resume")
        task = service.compaction_tasks[run["conversation_id"]]
        await service.cancel(run["id"])
        await asyncio.gather(task, return_exceptions=True)
        assert provider.calls == []
        assert service.compaction_tasks == {}
    asyncio.run(scenario())


def test_post_commit_resume_failure_does_not_emit_second_compaction_terminal(tmp_path, monkeypatch):
    async def scenario():
        repository = CoreRepository(tmp_path / "post-commit.db")
        events = []
        async def sink(event):
            events.append(event)
        service = ChatRunService(repository, sink)
        run = seed(repository)
        repository.transition_model_run(run["id"], {"queued"}, "waiting_context")
        monkeypatch.setattr("agent.runtime.create_provider", lambda profile: CheckpointProvider())
        async def broken_continue(run_id):
            raise sqlite3.OperationalError("synthetic continuation failure")
        monkeypatch.setattr(service, "continue_context", broken_continue)
        await service._run_compaction(run["id"], service.profiles["builtin-demo"], trigger="explicit_resume")
        terminal = [e["event_type"] for e in events if e["event_type"] in {
            "run.context_compaction_completed", "run.context_compaction_failed"}]
        assert terminal == ["run.context_compaction_completed"]
        assert service.context.context_repository.compaction_status(run["conversation_id"])["status"] == "valid"
    asyncio.run(scenario())


def test_restart_does_not_reopen_obsolete_interrupted_claim(tmp_path):
    async def scenario():
        repository = CoreRepository(tmp_path / "obsolete-claim.db")
        run = seed(repository)
        context = ContextEngineService(repository)
        store = context.context_repository
        store.begin_compaction(conversation_id=run["conversation_id"], run_id=run["id"],
                               context_generation=store.context_generation(run["conversation_id"]),
                               source_hash="old", idempotency_key="old")
        store.interrupt_compactions()
        await context.compact_once(run, CheckpointProvider(), context_window=32768, max_output_tokens=512)
        repository.transition_model_run(run["id"], {"queued"}, "generating")
        async def sink(event):
            pass
        ChatRunService(repository, sink)
        assert repository.get_model_run(run["id"])["status"] == "interrupted"
        assert store.compaction_status(run["conversation_id"])["status"] == "valid"
    asyncio.run(scenario())


def test_foreground_compaction_failure_waits_for_context(tmp_path, monkeypatch):
    async def scenario():
        repository = CoreRepository(tmp_path / "foreground.db")
        events = []
        async def sink(event):
            events.append(event)
        service = ChatRunService(repository, sink)
        run = seed(repository)
        def require_compaction(*args, **kwargs):
            raise ContextEngineError("context_compaction_required", "required")
        monkeypatch.setattr(service.context, "_assemble_once", require_compaction)
        monkeypatch.setattr("agent.runtime.create_provider", lambda profile: CheckpointProvider([RuntimeError("private payload")]))
        await service._execute(run["id"])
        current = repository.get_model_run(run["id"])
        assert (current["status"], current["error_code"]) == ("waiting_context", "context_compaction_internal_error")
        assert len([e for e in events if e["event_type"] == "run.context_compaction_failed"]) == 1
        assert len([e for e in events if e["event_type"] == "run.waiting_context"]) == 1
        assert "private payload" not in json.dumps(events)
    asyncio.run(scenario())


def test_background_unexpected_error_preserves_completed_answer_and_cleans_task(tmp_path, monkeypatch):
    async def scenario():
        repository = CoreRepository(tmp_path / "background.db")
        events = []
        async def sink(event):
            events.append(event)
        service = ChatRunService(repository, sink)
        run = seed(repository)
        repository.transition_model_run(run["id"], {"queued"}, "completed")
        monkeypatch.setattr("agent.runtime.create_provider", lambda profile: CheckpointProvider([RuntimeError("private payload")]))
        service._launch_compaction(run, service.profiles["builtin-demo"], trigger="post_run")
        await service.compaction_tasks[run["conversation_id"]]
        assert repository.get_model_run(run["id"])["status"] == "completed"
        assert run["conversation_id"] not in service.compaction_tasks
        failed = [e for e in events if e["event_type"] == "run.context_compaction_failed"]
        assert len(failed) == 1
        assert failed[0]["data"]["error_code"] == "context_compaction_internal_error"
    asyncio.run(scenario())


@pytest.mark.parametrize("response", ["malformed_json", "unsupported_json"])
@pytest.mark.parametrize("interruption", ["cancelled", "replaced_claim", "changed_generation"])
def test_each_compaction_dispatch_rechecks_cancellation_and_source(tmp_path, response, interruption):
    async def scenario():
        from agent.context_engine.domain import CompactionTarget
        repository = CoreRepository(tmp_path / "dispatch-guard.db")
        run = seed(repository)
        context = ContextEngineService(repository)
        started, release = asyncio.Event(), asyncio.Event()
        class InterruptedProvider(CheckpointProvider):
            def __init__(self):
                super().__init__()
                self.dispatches = []
            async def stream_events(self, *args, **kwargs):
                self.dispatches.append(asyncio.current_task().cancelling() > 0)
                if len(self.dispatches) == 1:
                    started.set()
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        pass
                    if response == "unsupported_json":
                        raise ProviderError("response_format_unsupported", "synthetic unsupported JSON")
                    yield ProviderEvent("text_delta", text="bad json")
                    return
                async for event in super().stream_events(*args, **kwargs):
                    yield event
        provider = InterruptedProvider()
        task = asyncio.create_task(context.compact_once(run, provider, context_window=32768, max_output_tokens=512))
        await started.wait()
        store = context.context_repository
        replacement = None
        if interruption == "changed_generation":
            repository._insert_message(run["conversation_id"], "user", "newer source")
        else:
            old = store.compaction_status(run["conversation_id"])
            store.pause_compaction(run["conversation_id"])
            if interruption == "replaced_claim":
                replacement = store.claim_compaction(
                    conversation_id=run["conversation_id"], run_id=run["id"],
                    source_hash=old["source_hash"], context_generation=old["context_generation"],
                    target=CompactionTarget(**json.loads(old["target_json"])), policy_version="replacement",
                )
            else:
                task.cancel()
        release.set()
        with pytest.raises((asyncio.CancelledError, CoreDataError, ContextEngineError)):
            await task
        assert len(provider.dispatches) == 1
        assert sum(provider.dispatches) == 0
        assert store.active_checkpoint(run["conversation_id"]) is None
        if replacement is not None:
            latest = store.compaction_status(run["conversation_id"])
            assert (latest["id"], latest["status"], latest["claim_token"]) == (
                replacement.attempt_id, "running", replacement.claim_token)
    asyncio.run(scenario())


@pytest.mark.parametrize("foreground", [False, True])
@pytest.mark.parametrize("revoke_at", ["started_event", "correction", "fallback"])
@pytest.mark.parametrize("revocation", ["disabled", "disallowed"])
def test_every_runtime_compaction_dispatch_rechecks_current_authority(tmp_path, monkeypatch, foreground, revoke_at, revocation):
    async def scenario():
        from dataclasses import replace
        repository = CoreRepository(tmp_path / "authority-guard.db")
        events = []
        def revoke():
            changes = {"enabled": False} if revocation == "disabled" else {"allowed_model_ids": ()}
            service.profiles["builtin-demo"] = replace(service.profiles["builtin-demo"], **changes)
        async def sink(event):
            events.append(event)
            if event["event_type"] == "run.context_compaction_started" and revoke_at == "started_event":
                revoke()
        service = ChatRunService(repository, sink)
        run = seed(repository)
        class RevokedProvider(CheckpointProvider):
            dispatches = 0
            async def stream_events(self, *args, **kwargs):
                self.dispatches += 1
                if self.dispatches == 1 and revoke_at != "started_event":
                    revoke()
                    if revoke_at == "fallback":
                        raise ProviderError("response_format_unsupported", "synthetic unsupported JSON")
                    yield ProviderEvent("text_delta", text="bad json")
                    return
                async for event in super().stream_events(*args, **kwargs):
                    yield event
        provider = RevokedProvider()
        monkeypatch.setattr("agent.runtime.create_provider", lambda profile: provider)
        profile = service.profiles["builtin-demo"]
        if foreground:
            repository.transition_model_run(run["id"], {"queued"}, "building_context")
            def require_compaction(*args, **kwargs):
                raise ContextEngineError("context_compaction_required", "required")
            monkeypatch.setattr(service.context, "_assemble_once", require_compaction)
            await service._prepare_context(run, profile, provider, round_no=0)
        else:
            await service._run_compaction(run["id"], profile, trigger="explicit_resume")
        assert provider.dispatches == (0 if revoke_at == "started_event" else 1)
        assert service.context.context_repository.active_checkpoint(run["conversation_id"]) is None
        failures = [e for e in events if e["event_type"] == "run.context_compaction_failed"]
        assert len(failures) == 1
        assert failures[0]["data"]["error_code"] == "execution_plan_stale"
        if foreground:
            assert repository.get_model_run(run["id"])["status"] == "waiting_context"
    asyncio.run(scenario())
