import json
import sqlite3
from pathlib import Path

from agent.storage import CoreRepository
from agent.tool_contracts import ToolCall
from agent.tool_repository import ToolRepository


def test_schema20_upgrade_preserves_rows_and_revokes_legacy_claims(tmp_path: Path):
    path = tmp_path / "upgrade.db"
    repository = CoreRepository(path)
    run = repository.start_model_run(
        content="preserved", idempotency_key="migration", provider_profile_id="provider",
        model_id="model", kind="standard_chat", category="casual",
        attachments=[{"id": "attachment", "kind": "text", "name": "notes.txt",
                      "media_type": "text/plain", "byte_size": 5, "sha256": "a" * 64,
                      "storage_key": "message-attachments/a.bin"}],
    )
    project = repository.create_project("Preserved project", "coding")
    work = repository.derive_work(run["conversation_id"], project["id"])
    source_message = work["messages"][0]
    assert source_message["attachments"][0]["source_attachment_id"] == "attachment"
    proposal = repository.propose_knowledge(
        work["id"], "fact", "Preserved fact", "Synthetic knowledge", source_message["id"],
    )
    repository.publish_knowledge(proposal["id"])
    repository.save_context_snapshot(run["id"], [
        {"source_type": "message", "source_id": run["user_message_id"], "estimated_tokens": 2},
    ], token_budget=1024, estimated_tokens=2, content_hash="audit-snapshot-hash")
    run_attempt = repository.begin_run_attempt(run["id"])
    tools = ToolRepository(path)
    operation = tools.create(run["id"], run_attempt["id"], 1, ToolCall(
        "audit-call", "fs.propose_changes", {"title": "Draft directory", "mutations": [{"kind": "mkdir", "path": "drafts"}]},
    ))
    tools.record_review(operation["id"], "provider", "model", "approve", "synthetic_review", 7)
    tools.transition(operation["id"], {"requested"}, "completed", result_summary=json.dumps({
        "change_set_id": "host-change-reference", "restore_point_id": "host-restore-reference",
    }))
    with repository._connect() as connection:
        for column in ("target_json", "budget_policy_json", "claim_token"):
            if column in {row[1] for row in connection.execute("PRAGMA table_info(context_compaction_attempts)")}:
                connection.execute(f"ALTER TABLE context_compaction_attempts DROP COLUMN {column}")
        connection.execute("UPDATE schema_info SET version = 20")
        for status in ("queued", "running", "valid", "failed", "interrupted", "stale"):
            connection.execute(
                """INSERT INTO context_compaction_attempts
                   (id, conversation_id, run_id, idempotency_key, context_generation,
                    source_hash, status, created_at) VALUES (?, ?, ?, ?, 0, 'hash', ?, 'now')""",
                (status, run["conversation_id"], run["id"], status, status),
            )
        connection.execute(
            """INSERT INTO context_compaction_attempts
               (id, conversation_id, idempotency_key, context_generation, source_hash, status, created_at)
               VALUES ('orphan', ?, 'orphan', 0, 'hash', 'failed', 'now')""", (run["conversation_id"],),
        )
        connection.execute(
            """INSERT INTO context_checkpoints
               (id, conversation_id, start_sequence, through_sequence, content_json, source_hash,
                provider_profile_id, model_id, policy_version, status, input_tokens, output_tokens, created_at)
               VALUES ('checkpoint', ?, 1, 1, '{"goal":[]}', 'original-hash', 'provider', 'model',
                       'old-policy', 'valid', 13, 5, 'old-time')""", (run["conversation_id"],),
        )
        connection.execute(
            """UPDATE context_compaction_attempts SET checkpoint_id = 'checkpoint', input_tokens = 13,
               output_tokens = 5, estimated_cost = 0.001, finished_at = 'old-time' WHERE id = 'valid'"""
        )
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name != 'schema_info'")]
        before = {table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in tables}
        preserved_tables = [table for table in tables if table != "context_compaction_attempts"]
        contents = {table: sorted((tuple(row) for row in connection.execute(f'SELECT * FROM "{table}"')), key=repr)
                    for table in preserved_tables}
    # Keep an open connection so a committed new row remains in WAL. Reading
    # the immutable main file proves that the backup must include those pages.
    keeper = repository._connect()
    keeper.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    keeper.execute("PRAGMA wal_autocheckpoint=0")
    keeper.execute("UPDATE messages SET content='committed WAL marker' WHERE id=?", (run["user_message_id"],))
    keeper.commit()
    with sqlite3.connect(f"file:{path.as_posix()}?immutable=1", uri=True) as main_only:
        assert main_only.execute("SELECT content FROM messages WHERE id=?", (run["user_message_id"],)).fetchone()[0] == "preserved"
    contents["messages"] = sorted((tuple(row) for row in keeper.execute('SELECT * FROM messages')), key=repr)
    CoreRepository(path)
    CoreRepository(path)
    keeper.close()
    with repository._connect() as connection:
        assert connection.execute("SELECT version FROM schema_info").fetchone()[0] == 28
        assert {table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in tables} == before
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert {table: sorted((tuple(row) for row in connection.execute(f'SELECT * FROM "{table}"')), key=repr)
                for table in preserved_tables} == contents
        attempts = {row["id"]: dict(row) for row in connection.execute("SELECT * FROM context_compaction_attempts")}
    assert (attempts["valid"]["checkpoint_id"], attempts["valid"]["input_tokens"],
            attempts["valid"]["output_tokens"], attempts["valid"]["estimated_cost"], attempts["valid"]["finished_at"]) == (
                "checkpoint", 13, 5, 0.001, "old-time")
    for identifier in ("queued", "running"):
        assert attempts[identifier]["status"] == "interrupted"
        assert attempts[identifier]["error_code"] == "core_interrupted"
    for identifier in ("valid", "failed", "interrupted", "stale"):
        assert attempts[identifier]["status"] == identifier
    for identifier, attempt in attempts.items():
        assert attempt["claim_token"] is None
        assert attempt["budget_policy_json"] is None
        assert json.loads(attempt["target_json"]) == (
            {"provider_profile_id": "provider", "model_id": "model"} if identifier != "orphan" else {})
    backups = list(tmp_path.glob("upgrade.db.pre-schema28-*.bak"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as connection:
        assert connection.execute("SELECT version FROM schema_info").fetchone()[0] == 20
        assert connection.execute("SELECT content FROM messages WHERE id=?", (run["user_message_id"],)).fetchone()[0] == "committed WAL marker"
        assert connection.execute("SELECT checkpoint_id FROM context_compaction_attempts WHERE id='valid'").fetchone()[0] == "checkpoint"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT status FROM context_compaction_attempts WHERE id='running'").fetchone()[0] == "running"


def test_new_database_needs_no_upgrade_backup(tmp_path: Path):
    path = tmp_path / "new.db"
    repository = CoreRepository(path)
    with repository._connect() as connection:
        assert connection.execute("SELECT version FROM schema_info").fetchone()[0] == 28
    assert list(tmp_path.glob("*.bak")) == []
