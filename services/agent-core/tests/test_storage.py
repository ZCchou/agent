from pathlib import Path
import sqlite3

import pytest

from agent.storage import CoreDataError, CoreRepository


def repo(tmp_path: Path) -> CoreRepository:
    return CoreRepository(tmp_path / "agent.db")


def standard(repository: CoreRepository, **overrides):
    values = {"kind": "standard_chat", "title": "New chat", "category": "casual"}
    values.update(overrides)
    return repository.create_conversation(**values)


def work(repository: CoreRepository, project_id: str):
    return repository.create_conversation("project_work", "New work", project_id=project_id)


def exchange(repository: CoreRepository, conversation_id: str, content: str):
    """Seed persisted history without invoking a model from the data repository."""
    normalized = repository._message_content(content)
    user = repository._insert_message(conversation_id, "user", normalized)
    category = repository.behavior_policy(conversation_id)["category"]
    assistant = repository._insert_message(
        conversation_id, "assistant", f"Demo [{category}]: {normalized}",
    )
    return {"messages": [user, assistant], "category": category}


def test_schema_initialization_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "agent.db"
    CoreRepository(path)
    CoreRepository(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version FROM schema_info").fetchone()[0] == 23


def test_schema_v2_migrates_without_losing_data(tmp_path: Path) -> None:
    path = tmp_path / "v2.db"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE schema_info(version INTEGER NOT NULL);
            INSERT INTO schema_info VALUES (2);
            CREATE TABLE chat_groups(id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE conversations(
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL, category TEXT,
                chat_group_id TEXT REFERENCES chat_groups(id), project_id TEXT REFERENCES projects(id),
                derived_from_conversation_id TEXT, derived_through_message_id TEXT, derived_at TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE messages(
                id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
                role TEXT NOT NULL, content TEXT NOT NULL, event_type TEXT, source_message_id TEXT,
                created_at TEXT NOT NULL
            );
            INSERT INTO chat_groups VALUES ('g1', 'Group', 'now', 'now');
            INSERT INTO conversations VALUES ('c1', 'standard_chat', 'Chat', 'casual', 'g1', NULL, NULL, NULL, NULL, 'now', 'now');
        """)
    repository = CoreRepository(path)
    assert repository.get_conversation("c1")["chat_group_id"] == "g1"
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version FROM schema_info").fetchone()[0] == 23
        assert connection.execute("SELECT sort_order FROM conversations WHERE id = 'c1'").fetchone()[0] == 0


def test_v8_to_v9_preserves_records_and_adds_context_contract(tmp_path: Path) -> None:
    path = tmp_path / "v8.db"
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        CoreRepository._create_schema_v4(connection)
        connection.execute("CREATE TABLE schema_info(version INTEGER NOT NULL)")
        connection.execute("INSERT INTO schema_info VALUES (4)")
        CoreRepository._migrate_v4_to_v5(connection)
        CoreRepository._migrate_v5_to_v6(connection)
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("PRAGMA legacy_alter_table = ON")
        CoreRepository._migrate_v6_to_v7(connection)
        connection.execute("PRAGMA legacy_alter_table = OFF")
        CoreRepository._migrate_v7_to_v8(connection)
        connection.execute("UPDATE schema_info SET version = 8")
        connection.execute(
            """INSERT INTO projects(id,name,sort_order,category,created_at,updated_at,deleted_at,
                                     model_access_policy,model_scope_policy)
               VALUES ('p','Project',0,'coding','now','now',NULL,'local_only','inherit_global')"""
        )
        connection.execute(
            """INSERT INTO conversations(id,kind,title,category,chat_group_id,project_id,sort_order,
                   created_at,updated_at,deleted_at) VALUES
                   ('c','project_work','Work',NULL,NULL,'p',0,'now','now',NULL)"""
        )
        connection.execute(
            """INSERT INTO messages(id,conversation_id,role,content,event_type,source_message_id,created_at)
               VALUES ('m','c','user','hello',NULL,NULL,'now')"""
        )
        before = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in ("projects", "conversations", "messages", "model_runs")}
    CoreRepository(path)
    with sqlite3.connect(path) as connection:
        after = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                 for table in ("projects", "conversations", "messages", "model_runs")}
        message_columns = {row[1] for row in connection.execute("PRAGMA table_info(messages)")}
        run_columns = {row[1] for row in connection.execute("PRAGMA table_info(model_runs)")}
        version = connection.execute("SELECT version FROM schema_info").fetchone()[0]
    assert before == after
    assert version == 23
    assert "sequence_no" in message_columns
    assert {"context_strategy", "context_generation", "automatic_compaction_used"} <= run_columns


def test_message_attachments_persist_and_copy_with_work_snapshot(tmp_path: Path) -> None:
    repository = CoreRepository(tmp_path / "attachments.db")
    project = repository.create_project("Project", "coding")
    run = repository.start_model_run(
        content="review this", idempotency_key="attachment-run", provider_profile_id="builtin-demo",
        model_id="demo", kind="standard_chat", category="casual",
        attachments=[{
            "id": "attachment-1", "kind": "text", "name": "notes.md",
            "media_type": "text/markdown", "byte_size": 12, "sha256": "a" * 64,
            "storage_key": "message-attachments/attachment-1.bin", "width": None, "height": None,
        }],
    )
    conversation = repository.get_conversation(run["conversation_id"])
    assert conversation["messages"][0]["attachments"][0]["name"] == "notes.md"
    work = repository.derive_work(conversation["id"], project["id"])
    copied = work["messages"][0]["attachments"][0]
    assert copied["storage_key"] == "message-attachments/attachment-1.bin"
    assert copied["source_attachment_id"] == "attachment-1"


def test_v14_to_v15_creates_artifacts_without_changing_business_rows(tmp_path: Path) -> None:
    path = tmp_path / "schema14-attachments.db"
    repository = CoreRepository(path)
    run = repository.start_model_run(
        content="read the attachment", idempotency_key="schema14-attachment",
        provider_profile_id="builtin-demo", model_id="demo",
        kind="standard_chat", category="casual",
        attachments=[{
            "id": "legacy-text", "kind": "text", "name": "notes.txt",
            "media_type": "text/plain", "byte_size": 5, "sha256": "b" * 64,
            "storage_key": "message-attachments/legacy-text.bin",
            "width": None, "height": None,
        }],
    )
    with sqlite3.connect(path) as connection:
        before = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("conversations", "messages", "model_runs", "message_attachments")
        }
        connection.execute("DROP TABLE invocation_specs")
        connection.execute("DROP TABLE message_attachment_rejections")
        connection.execute("DROP TABLE attachment_artifacts")
        connection.execute("UPDATE schema_info SET version = 14")

    CoreRepository(path)

    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        after = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("conversations", "messages", "model_runs", "message_attachments")
        }
        artifact = connection.execute(
            "SELECT * FROM attachment_artifacts WHERE attachment_id = 'legacy-text'"
        ).fetchone()
        version = connection.execute("SELECT version FROM schema_info").fetchone()[0]
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        attachment_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(message_attachments)")
        }

    assert run["id"]
    assert before == after
    assert version == 23
    assert artifact["kind"] == "text.utf8"
    assert artifact["processor_id"] == "builtin.utf8_text"
    assert artifact["storage_key"] == "message-attachments/legacy-text.bin"
    assert not {
        "model_storage_key", "model_media_type", "model_byte_size", "model_sha256",
        "model_width", "model_height", "normalization_version",
    } & attachment_columns
    assert foreign_key_errors == []


def test_rejected_attachment_can_be_sent_without_aborting_the_message(tmp_path: Path) -> None:
    repository = CoreRepository(tmp_path / "rejected-attachment.db")
    run = repository.start_model_run(
        content="Please continue with anything you could read.",
        idempotency_key="rejected-attachment-run",
        provider_profile_id="builtin-demo", model_id="demo",
        kind="standard_chat", category="casual",
        attachment_rejections=[{
            "receipt_id": "receipt-pdf",
            "display_name": "paper.pdf",
            "detected_media_type": "application/pdf",
            "error_code": "attachment_type_unsupported",
            "processor_hint": "document_processor_unavailable",
        }],
    )

    conversation = repository.get_conversation(run["conversation_id"])
    user_message = conversation["messages"][0]
    assert user_message["content"] == "Please continue with anything you could read."
    assert user_message["attachments"] == []
    assert len(user_message["attachment_rejections"]) == 1
    rejection = user_message["attachment_rejections"][0]
    assert {key: rejection[key] for key in (
        "id", "message_id", "display_name", "detected_media_type",
        "error_code", "processor_hint",
    )} == {
        "id": "receipt-pdf",
        "message_id": user_message["id"],
        "display_name": "paper.pdf",
        "detected_media_type": "application/pdf",
        "error_code": "attachment_type_unsupported",
        "processor_hint": "document_processor_unavailable",
    }


def test_committed_attachment_and_rejection_can_be_reused_by_a_later_message(
    tmp_path: Path,
) -> None:
    repository = CoreRepository(tmp_path / "reused-attachment.db")
    attachment = {
        "id": "staged-text", "kind": "text", "name": "notes.txt",
        "media_type": "text/plain", "byte_size": 5, "sha256": "c" * 64,
        "storage_key": "message-attachments/staged-text.bin",
        "width": None, "height": None,
        "artifacts": [{
            "artifact_id": "staged-text-artifact", "kind": "text.utf8",
            "processor_id": "builtin.utf8_text", "processor_version": "utf8-text-v1",
            "media_type": "text/plain", "byte_size": 5, "sha256": "c" * 64,
            "storage_key": "message-attachments/staged-text.bin",
            "width": None, "height": None,
        }],
    }
    rejection = {
        "receipt_id": "staged-pdf-rejection", "display_name": "paper.pdf",
        "detected_media_type": "application/pdf",
        "error_code": "attachment_type_unsupported",
        "processor_hint": "document_processor_unavailable",
    }

    first = repository.start_model_run(
        content="first", idempotency_key="reuse-first",
        provider_profile_id="builtin-demo", model_id="demo",
        kind="standard_chat", category="casual",
        attachments=[attachment], attachment_rejections=[rejection],
    )
    second = repository.start_model_run(
        content="second", idempotency_key="reuse-second",
        provider_profile_id="builtin-demo", model_id="demo",
        kind="standard_chat", category="casual",
        attachments=[attachment], attachment_rejections=[rejection],
    )

    first_message = repository.get_conversation(first["conversation_id"])["messages"][0]
    second_message = repository.get_conversation(second["conversation_id"])["messages"][0]
    original = first_message["attachments"][0]
    reused = second_message["attachments"][0]
    assert original["id"] == "staged-text"
    assert reused["id"] != original["id"]
    assert reused["source_attachment_id"] == original["id"]
    assert reused["storage_key"] == original["storage_key"]
    assert reused["artifacts"][0]["artifact_id"] != original["artifacts"][0]["artifact_id"]
    assert reused["artifacts"][0]["storage_key"] == original["artifacts"][0]["storage_key"]
    assert second_message["attachment_rejections"][0]["id"] != rejection["receipt_id"]


def test_web_disabled_retry_reuses_message_and_does_not_repeat_native_route(tmp_path: Path) -> None:
    repository = CoreRepository(tmp_path / "web-disabled-retry.db")
    parent = repository.start_model_run(
        content="analyze this image", idempotency_key="native-image",
        provider_profile_id="deepseek", model_id="vision-model",
        kind="standard_chat", category="casual",
        web_search_request_mode="auto", web_search_route="native",
        web_search_reason_code="native_search_available",
    )
    repository.transition_model_run(
        parent["id"], {"queued"}, "failed", "multimodal_route_unsupported",
        "The native route cannot combine image input and search.",
    )
    retry = repository.create_web_disabled_retry(parent["id"])
    assert retry["id"] != parent["id"]
    assert retry["parent_run_id"] == parent["id"]
    assert retry["user_message_id"] == parent["user_message_id"]
    assert retry["web_search_request_mode"] == "off"
    assert retry["web_search_route"] == "none"
    assert len(repository.get_conversation(parent["conversation_id"])["messages"]) == 1


def test_v3_repair_fixes_migrated_chat_group_column_order(tmp_path: Path) -> None:
    path = tmp_path / "v3.db"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE schema_info(version INTEGER NOT NULL);
            INSERT INTO schema_info VALUES (3);
            CREATE TABLE chat_groups(
                id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, sort_order INTEGER NOT NULL
            );
            CREATE TABLE projects(
                id TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, sort_order INTEGER NOT NULL
            );
            CREATE TABLE conversations(
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL, category TEXT,
                chat_group_id TEXT REFERENCES chat_groups(id), project_id TEXT REFERENCES projects(id),
                derived_from_conversation_id TEXT, derived_through_message_id TEXT, derived_at TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, sort_order INTEGER NOT NULL,
                copied_from_conversation_id TEXT, copied_through_message_id TEXT, copied_at TEXT
            );
            CREATE TABLE messages(
                id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
                role TEXT NOT NULL, content TEXT NOT NULL, event_type TEXT, source_message_id TEXT,
                created_at TEXT NOT NULL
            );
            INSERT INTO chat_groups VALUES ('g1', 'Good', 'now', 'now', 0);
            INSERT INTO chat_groups VALUES ('g2', 'Broken', 0, '2026-09-14T00:00:00Z', '2026-09-14T00:00:00Z');
            INSERT INTO conversations VALUES(
                'c1','standard_chat','Chat','casual','g2',NULL,NULL,NULL,NULL,
                'now','now',0,NULL,NULL,NULL
            );
        """)
    repository = CoreRepository(path)
    groups = repository.list_chat_groups()
    assert [group["sort_order"] for group in groups] == [0, 1]
    assert all(isinstance(group["sort_order"], int) for group in groups)
    assert repository.get_conversation("c1")["chat_group_id"] == "g2"
    created = repository.create_chat_group("After migration")
    assert created["sort_order"] == 0
    assert isinstance(created["created_at"], str)


def test_chat_history_and_trash_are_recoverable(tmp_path: Path) -> None:
    repository = repo(tmp_path)
    group = repository.create_chat_group("Group")
    conversation = standard(repository, chat_group_id=group["id"])
    messages = exchange(repository, conversation["id"], "hello")["messages"]
    conversation_id = conversation["id"]
    assert [message["role"] for message in messages] == ["user", "assistant"]
    item = repository.delete_conversation(conversation_id)
    assert item["entity_type"] == "conversation"
    assert repository.list_conversations() == []
    repository.restore_trash("conversation", conversation_id)
    assert repository.get_conversation(conversation_id)["messages"]


def test_invalid_unicode_does_not_create_chat(tmp_path: Path) -> None:
    repository = repo(tmp_path)
    with pytest.raises(CoreDataError, match="Unicode"):
        repository._message_content("bad\udc80")
    assert repository.list_conversations() == []


def test_unknown_schema_is_rejected_without_rebuilding(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE schema_info (version INTEGER NOT NULL)")
        connection.execute("INSERT INTO schema_info(version) VALUES (99)")
    with pytest.raises(CoreDataError, match="schema"):
        CoreRepository(path)
    with sqlite3.connect(path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"schema_info"}


def test_chat_groups_only_organize_standard_chats(tmp_path: Path) -> None:
    repository = repo(tmp_path)
    group = repository.create_chat_group("Ideas")
    chat = standard(repository, chat_group_id=group["id"])
    project = repository.create_project("Code", "coding")
    with pytest.raises(CoreDataError, match="Project work"):
        repository.create_conversation(
            "project_work", "Invalid", category=None,
            chat_group_id=group["id"], project_id=project["id"],
        )
    repository.delete_chat_group(group["id"])
    assert repository.get_conversation(chat["id"])["chat_group_id"] is None


def test_chats_in_same_group_do_not_share_history(tmp_path: Path) -> None:
    repository = repo(tmp_path)
    group = repository.create_chat_group("Ideas")
    first = standard(repository, title="First", chat_group_id=group["id"])
    second = standard(repository, title="Second", chat_group_id=group["id"])
    exchange(repository, first["id"], "only first")
    assert len(repository.get_conversation(first["id"])["messages"]) == 2
    assert repository.get_conversation(second["id"])["messages"] == []


def test_group_rename_and_missing_delete(tmp_path: Path) -> None:
    repository = repo(tmp_path)
    group = repository.create_chat_group("One")
    assert repository.rename_chat_group(group["id"], "Two")["name"] == "Two"
    repository.delete_chat_group(group["id"])
    with pytest.raises(CoreDataError, match="does not exist"):
        repository.delete_chat_group(group["id"])


def test_manual_order_survives_activity_and_container_changes(tmp_path: Path) -> None:
    repository = repo(tmp_path)
    first_group = repository.create_chat_group("First")
    second_group = repository.create_chat_group("Second")
    repository.reorder_chat_groups([first_group["id"], second_group["id"]])
    assert [item["name"] for item in repository.list_chat_groups()] == ["First", "Second"]
    first = standard(repository, title="First")
    second = standard(repository, title="Second")
    assert [item["title"] for item in repository.list_conversations("standard_chat")] == ["Second", "First"]
    repository.reorder_conversations("standard_chat", None, [first["id"], second["id"]])
    exchange(repository, second["id"], "activity does not reorder")
    assert [item["id"] for item in repository.list_conversations("standard_chat")] == [first["id"], second["id"]]
    repository.move_chat(first["id"], first_group["id"], 0)
    assert repository.get_conversation(first["id"])["chat_group_id"] == first_group["id"]


def test_delete_group_moves_chats_to_ungrouped_top_in_relative_order(tmp_path: Path) -> None:
    repository = repo(tmp_path)
    outside = standard(repository, title="Outside")
    group = repository.create_chat_group("Group")
    older = standard(repository, title="Older", chat_group_id=group["id"])
    newer = standard(repository, title="Newer", chat_group_id=group["id"])
    repository.reorder_conversations("standard_chat", group["id"], [older["id"], newer["id"]])
    repository.delete_chat_group(group["id"])
    ids = [item["id"] for item in repository.list_conversations("standard_chat")]
    assert ids == [older["id"], newer["id"], outside["id"]]


def test_invalid_reorder_is_atomic(tmp_path: Path) -> None:
    repository = repo(tmp_path)
    first = standard(repository, title="First")
    second = standard(repository, title="Second")
    before = [item["id"] for item in repository.list_conversations("standard_chat")]
    with pytest.raises(CoreDataError, match="exactly match"):
        repository.reorder_conversations("standard_chat", None, [first["id"]])
    with pytest.raises(CoreDataError, match="duplicates"):
        repository.reorder_conversations("standard_chat", None, [first["id"], first["id"]])
    assert [item["id"] for item in repository.list_conversations("standard_chat")] == before


def test_standard_chat_cannot_belong_to_project(tmp_path: Path) -> None:
    repository = repo(tmp_path)
    project = repository.create_project("Code", "coding")
    with pytest.raises(CoreDataError, match="cannot belong"):
        standard(repository, project_id=project["id"])


def test_work_requires_project_and_inherits_category(tmp_path: Path) -> None:
    repository = repo(tmp_path)
    with pytest.raises(CoreDataError, match="must belong"):
        repository.create_conversation("project_work", "Work")
    project = repository.create_project("Research", "research")
    conversation = work(repository, project["id"])
    assert conversation["category"] is None
    assert conversation["resolved_category"] == "research"
    repository.update_project(project["id"], "Research", "writing")
    assert repository.get_conversation(conversation["id"])["resolved_category"] == "writing"


def test_listing_filters_two_tracks(tmp_path: Path) -> None:
    repository = repo(tmp_path)
    group = repository.create_chat_group("Group")
    project = repository.create_project("Project", "work")
    standard(repository, chat_group_id=group["id"])
    work(repository, project["id"])
    assert len(repository.list_conversations(kind="standard_chat", chat_group_id=group["id"])) == 1
    assert len(repository.list_conversations(kind="project_work", project_id=project["id"])) == 1


def test_derivation_copies_complete_snapshot_then_diverges(tmp_path: Path) -> None:
    repository = repo(tmp_path)
    source = standard(repository)
    exchange(repository, source["id"], "original")
    source_at_derivation = repository.get_conversation(source["id"])
    project = repository.create_project("Build", "coding")
    derived = repository.derive_work(source["id"], project["id"])
    assert derived["kind"] == "project_work"
    assert derived["derived_from_conversation_id"] == source["id"]
    assert derived["derived_through_message_id"] == source_at_derivation["messages"][-1]["id"]
    assert [message["content"] for message in derived["messages"]] == [
        "original", "Demo [casual]: original",
    ]
    assert all(message["source_message_id"] for message in derived["messages"])
    exchange(repository, source["id"], "source only")
    exchange(repository, derived["id"], "work only")
    restored = repository.get_conversation(derived["id"])
    assert "source only" not in [message["content"] for message in restored["messages"]]
    assert restored["messages"][-1]["content"] == "Demo [coding]: work only"


def test_work_cannot_be_derived_again(tmp_path: Path) -> None:
    repository = repo(tmp_path)
    project = repository.create_project("Build", "coding")
    with pytest.raises(CoreDataError, match="Only a standard"):
        repository.derive_work(work(repository, project["id"])["id"], project["id"])


def test_work_moves_and_copies_into_target_project_policy(tmp_path: Path) -> None:
    repository = repo(tmp_path)
    source_project = repository.create_project("Source", "research")
    target_project = repository.create_project("Target", "coding")
    source = work(repository, source_project["id"])
    exchange(repository, source["id"], "before move")
    moved = repository.move_work(source["id"], target_project["id"], 0)
    assert moved["id"] == source["id"]
    assert moved["project_id"] == target_project["id"]
    assert moved["resolved_category"] == "coding"
    assert exchange(repository, moved["id"], "after move")["category"] == "coding"

    copied = repository.copy_work(moved["id"], source_project["id"])
    assert copied["id"] != moved["id"]
    assert copied["copied_from_conversation_id"] == moved["id"]
    assert copied["copied_through_message_id"] is not None
    assert copied["resolved_category"] == "research"
    assert all(message["source_message_id"] for message in copied["messages"])
    exchange(repository, copied["id"], "copy only")
    assert "copy only" not in [message["content"] for message in repository.get_conversation(moved["id"])["messages"]]


def test_category_policy_is_resolved_per_track(tmp_path: Path) -> None:
    repository = repo(tmp_path)
    chat = standard(repository, category="research")
    project = repository.create_project("Writing", "writing")
    project_work = work(repository, project["id"])
    assert repository.behavior_policy(chat["id"])["planning_style"]
    assert repository.behavior_policy(chat["id"])["suggested_abilities"] == []
    assert repository.behavior_policy(project_work["id"])["category"] == "writing"


def test_project_work_can_override_and_restore_project_purpose(tmp_path: Path) -> None:
    repository = repo(tmp_path)
    project = repository.create_project("Mixed work", "coding")
    project_work = work(repository, project["id"])

    overridden = repository.update_work_purpose(project_work["id"], "writing")
    assert overridden["purpose_override"] == "writing"
    assert overridden["resolved_category"] == "writing"

    repository.update_project(project["id"], "Mixed work", "research")
    assert repository.get_conversation(project_work["id"])["resolved_category"] == "writing"

    inherited = repository.update_work_purpose(project_work["id"], None)
    assert inherited["purpose_override"] is None
    assert inherited["resolved_category"] == "research"

    chat = standard(repository)
    with pytest.raises(CoreDataError, match="Only project work"):
        repository.update_work_purpose(chat["id"], "coding")


def test_category_changes_only_affect_future_replies(tmp_path: Path) -> None:
    repository = repo(tmp_path)
    chat = standard(repository, title="Topic", category="research")
    exchange(repository, chat["id"], "first")
    repository.update_conversation(chat["id"], "Topic", "writing")
    exchange(repository, chat["id"], "second")
    replies = [
        message["content"]
        for message in repository.get_conversation(chat["id"])["messages"]
        if message["role"] == "assistant"
    ]
    assert replies == ["Demo [research]: first", "Demo [writing]: second"]


def test_legacy_slash_commands_are_plain_text_not_a_second_tool_system(tmp_path: Path) -> None:
    repository = repo(tmp_path)
    chat = standard(repository)
    exchange(repository, chat["id"], "/files")
    assert repository.get_conversation(chat["id"])["messages"][0]["content"] == "/files"


@pytest.mark.parametrize("name", ["", " ", "x" * 81])
def test_names_are_validated(tmp_path: Path, name: str) -> None:
    repository = repo(tmp_path)
    with pytest.raises(CoreDataError):
        repository.create_project(name, "casual")
    with pytest.raises(CoreDataError):
        repository.create_chat_group(name)


@pytest.mark.parametrize("content", ["", "   ", "x" * 20_001])
def test_message_length_is_validated(tmp_path: Path, content: str) -> None:
    repository = repo(tmp_path)
    conversation = standard(repository)
    with pytest.raises(CoreDataError, match="Message"):
        exchange(repository, conversation["id"], content)
