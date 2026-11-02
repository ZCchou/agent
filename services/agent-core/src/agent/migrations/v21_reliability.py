from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone


def migrate(connection: sqlite3.Connection) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(context_compaction_attempts)")}
    for column in ("target_json", "budget_policy_json", "claim_token"):
        if column not in columns:
            connection.execute(f"ALTER TABLE context_compaction_attempts ADD COLUMN {column} TEXT NULL")
    # Historical runs prove identity, but not the capability limits or protocol
    # in effect when compaction ran. Never manufacture those unknown values.
    rows = connection.execute(
        """SELECT a.id, r.provider_profile_id, r.model_id FROM context_compaction_attempts a
           LEFT JOIN model_runs r ON r.id = a.run_id WHERE a.target_json IS NULL"""
    ).fetchall()
    for row in rows:
        target = {key: row[key] for key in ("provider_profile_id", "model_id") if row[key] is not None}
        connection.execute("UPDATE context_compaction_attempts SET target_json = ? WHERE id = ?",
                           (json.dumps(target, sort_keys=True), row["id"]))
    connection.execute(
        """UPDATE context_compaction_attempts SET status = 'interrupted',
           error_code = 'core_interrupted', claim_token = NULL, finished_at = ?
           WHERE status IN ('queued', 'running')""",
        (datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),),
    )
