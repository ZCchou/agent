from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent.storage import CoreDataError, CoreRepository


def _source(repository: CoreRepository) -> tuple[dict, dict]:
    conversation = repository.create_conversation("standard_chat", "Chat", "research")
    run = repository.start_model_run(
        content="帮我找西安事变纪念馆官网",
        idempotency_key="source-run",
        provider_profile_id="builtin-demo", model_id="demo",
        conversation_id=conversation["id"], kind="standard_chat", category="research",
    )
    message = repository.get_conversation(conversation["id"])["messages"][-1]
    return conversation, {"run": run, "message": message}


def test_pending_action_round_trips_and_is_single_use(tmp_path) -> None:
    repository = CoreRepository(tmp_path / "follow-up.db")
    conversation, source = _source(repository)

    action = repository.create_pending_follow_up_action(
        conversation_id=conversation["id"], source_run_id=source["run"]["id"],
        source_message_id=source["message"]["id"], action_type="web_search",
        query_hint="帮我找西安事变纪念馆官网", purpose="核对官方域名",
        evidence="required", ttl_seconds=600,
    )

    assert repository.get_pending_follow_up_action(conversation["id"])["id"] == action["id"]
    consumed = repository.consume_pending_follow_up_action(
        conversation["id"], action["id"],
    )
    assert consumed["status"] == "consumed"
    assert repository.get_pending_follow_up_action(conversation["id"]) is None


def test_pending_action_replaces_previous_pending_action(tmp_path) -> None:
    repository = CoreRepository(tmp_path / "follow-up-replace.db")
    conversation, source = _source(repository)
    first = repository.create_pending_follow_up_action(
        conversation_id=conversation["id"], source_run_id=source["run"]["id"],
        source_message_id=source["message"]["id"], action_type="web_search",
        query_hint="first", purpose="first", evidence="required", ttl_seconds=600,
    )
    second = repository.create_pending_follow_up_action(
        conversation_id=conversation["id"], source_run_id=source["run"]["id"],
        source_message_id=source["message"]["id"], action_type="web_search",
        query_hint="second", purpose="second", evidence="required", ttl_seconds=600,
    )

    assert repository.get_pending_follow_up_action(conversation["id"])["id"] == second["id"]
    with pytest.raises(CoreDataError) as error:
        repository.consume_pending_follow_up_action(conversation["id"], first["id"])
    assert error.value.code == "pending_follow_up_action_stale"


def test_expired_pending_action_is_not_returned(tmp_path) -> None:
    repository = CoreRepository(tmp_path / "follow-up-expired.db")
    conversation, source = _source(repository)
    repository.create_pending_follow_up_action(
        conversation_id=conversation["id"], source_run_id=source["run"]["id"],
        source_message_id=source["message"]["id"], action_type="web_search",
        query_hint="expired", purpose="expired", evidence="required", ttl_seconds=-1,
    )

    assert repository.get_pending_follow_up_action(conversation["id"]) is None
