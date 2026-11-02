from __future__ import annotations

import sqlite3


def migrate(connection: sqlite3.Connection) -> None:
    """Upgrade web-search conversation defaults without losing business data.

    SQLite cannot widen a CHECK constraint in place, so the conversation table is
    rebuilt while foreign-key rewriting is disabled by the migration caller.
    """
    protected = ("projects", "conversations", "messages", "model_runs", "web_operations")
    before = {
        table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in protected
    }
    connection.execute("ALTER TABLE conversations RENAME TO conversations_v10")
    connection.executescript(
        """
        CREATE TABLE conversations (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL CHECK(kind IN ('standard_chat','project_work')),
            title TEXT NOT NULL,
            category TEXT NULL CHECK(category IS NULL OR category IN
                ('casual','research','work','writing','coding','gaming','other')),
            chat_group_id TEXT NULL REFERENCES chat_groups(id) ON DELETE SET NULL,
            project_id TEXT NULL REFERENCES projects(id) ON DELETE CASCADE,
            sort_order INTEGER NOT NULL,
            derived_from_conversation_id TEXT NULL REFERENCES conversations(id) ON DELETE SET NULL,
            derived_through_message_id TEXT NULL,
            derived_at TEXT NULL,
            copied_from_conversation_id TEXT NULL REFERENCES conversations(id) ON DELETE SET NULL,
            copied_through_message_id TEXT NULL,
            copied_at TEXT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            deleted_at TEXT NULL,
            provider_profile_id TEXT NULL,
            model_id TEXT NULL,
            purpose_override TEXT NULL CHECK(purpose_override IS NULL OR purpose_override IN
                ('casual','research','work','writing','coding','gaming','other')),
            context_generation INTEGER NOT NULL DEFAULT 0,
            web_search_mode TEXT NOT NULL DEFAULT 'inherit'
                CHECK(web_search_mode IN ('inherit','off','auto')),
            CHECK(
                (kind = 'standard_chat' AND category IS NOT NULL AND project_id IS NULL)
                OR (kind = 'project_work' AND category IS NULL AND project_id IS NOT NULL
                    AND chat_group_id IS NULL)
            )
        );
        INSERT INTO conversations(
            id, kind, title, category, chat_group_id, project_id, sort_order,
            derived_from_conversation_id, derived_through_message_id, derived_at,
            copied_from_conversation_id, copied_through_message_id, copied_at,
            created_at, updated_at, deleted_at, provider_profile_id, model_id,
            purpose_override, context_generation, web_search_mode
        ) SELECT
            id, kind, title, category, chat_group_id, project_id, sort_order,
            derived_from_conversation_id, derived_through_message_id, derived_at,
            copied_from_conversation_id, copied_through_message_id, copied_at,
            created_at, updated_at, deleted_at, provider_profile_id, model_id,
            purpose_override, context_generation,
            CASE WHEN web_search_mode = 'auto' THEN 'auto' ELSE 'inherit' END
        FROM conversations_v10;
        DROP TABLE conversations_v10;
        """
    )
    after = {
        table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in protected
    }
    if before != after:
        raise RuntimeError("Web-search usability migration changed protected record counts")
    invalid = connection.execute(
        "SELECT COUNT(*) FROM conversations WHERE web_search_mode NOT IN ('inherit','off','auto')"
    ).fetchone()[0]
    if invalid:
        raise RuntimeError("Web-search usability migration produced invalid conversation modes")
