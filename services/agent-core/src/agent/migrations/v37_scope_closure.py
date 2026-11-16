"""Separate document inventory from task obligations and persist reading proofs."""


def migrate(connection):
    import hashlib
    from datetime import datetime, timezone
    from agent.global_instructions import (DEFAULT_GLOBAL_INSTRUCTIONS, DEFAULT_GLOBAL_INSTRUCTIONS_VERSION,
                                           LEGACY_DEFAULT_GLOBAL_INSTRUCTIONS)
    legacy_hash = hashlib.sha256(LEGACY_DEFAULT_GLOBAL_INSTRUCTIONS.encode('utf-8')).hexdigest()
    # Only an untouched bundled default is ours to replace. Edited instructions
    # and the user's enabled/disabled choice remain intact.
    connection.execute('''UPDATE global_instructions SET content=?,content_hash=?,default_version=?,
        revision=revision+1,updated_at=? WHERE id=1 AND revision=1 AND default_version='agent-default-v1'
        AND content=? AND content_hash=?''', (DEFAULT_GLOBAL_INSTRUCTIONS,
        hashlib.sha256(DEFAULT_GLOBAL_INSTRUCTIONS.encode('utf-8')).hexdigest(),
        DEFAULT_GLOBAL_INSTRUCTIONS_VERSION,datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
        LEGACY_DEFAULT_GLOBAL_INSTRUCTIONS,legacy_hash))
    connection.execute('''CREATE TABLE IF NOT EXISTS task_review_proofs (
        task_id TEXT NOT NULL REFERENCES execution_wait_tasks(id) ON DELETE CASCADE,
        source_id TEXT NOT NULL, source_hash TEXT NOT NULL, target_id TEXT NOT NULL,
        outcome TEXT NOT NULL, evidence_generation INTEGER,
        PRIMARY KEY(task_id,source_id,target_id))''')
    if 'evidence_generation' not in {row[1] for row in connection.execute('PRAGMA table_info(task_review_proofs)')}:
        connection.execute('ALTER TABLE task_review_proofs ADD COLUMN evidence_generation INTEGER')
    # Legacy read bookkeeping used the same outcome vocabulary. Only a real
    # review payload and one authorized review source establish provenance.
    connection.execute('''INSERT OR IGNORE INTO task_review_proofs
        (task_id,source_id,source_hash,target_id,outcome)
        SELECT s.task_id,s.source_id,w.source_hash,w.target_id,w.outcome
        FROM task_sources s JOIN task_work_items w
          ON w.task_id=s.task_id AND w.source_id=s.artifact_id AND w.source_hash=s.source_hash
        WHERE s.operation='review' AND w.outcome IN ('done','gap')
          AND json_valid(w.result_json) AND json_type(w.result_json,'$.summary')='text'
          AND (SELECT count(*) FROM task_sources other WHERE other.task_id=s.task_id
               AND other.artifact_id=s.artifact_id AND other.operation='review')=1''')
    connection.execute('''CREATE TABLE IF NOT EXISTS task_inventory_sources (
        task_id TEXT NOT NULL REFERENCES execution_wait_tasks(id) ON DELETE CASCADE,
        source_id TEXT NOT NULL, artifact_id TEXT NOT NULL, source_hash TEXT NOT NULL,
        complete INTEGER NOT NULL, parser_view TEXT, PRIMARY KEY(task_id,source_id))''')
    if 'parser_view' not in {row[1] for row in connection.execute('PRAGMA table_info(task_inventory_sources)')}:
        connection.execute('ALTER TABLE task_inventory_sources ADD COLUMN parser_view TEXT')
    connection.execute('''CREATE TABLE IF NOT EXISTS task_source_aliases (
        task_id TEXT NOT NULL REFERENCES execution_wait_tasks(id) ON DELETE CASCADE,
        alias TEXT NOT NULL, source_id TEXT NOT NULL, PRIMARY KEY(task_id,alias))''')
    connection.execute('''CREATE TABLE IF NOT EXISTS task_inventory_positions (
        task_id TEXT NOT NULL REFERENCES execution_wait_tasks(id) ON DELETE CASCADE,
        source_id TEXT NOT NULL, target_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
        source_hash TEXT NOT NULL, content_json TEXT NOT NULL,
        PRIMARY KEY(task_id,source_id,target_id))''')
    connection.execute('''CREATE TABLE IF NOT EXISTS task_read_proofs (
        task_id TEXT NOT NULL REFERENCES execution_wait_tasks(id) ON DELETE CASCADE,
        source_id TEXT NOT NULL, source_hash TEXT NOT NULL, target_id TEXT NOT NULL,
        text TEXT NOT NULL, complete INTEGER NOT NULL,
        PRIMARY KEY(task_id,source_id,target_id))''')
    connection.execute('''CREATE TABLE IF NOT EXISTS invocation_diagnostics (
        spec_id TEXT PRIMARY KEY REFERENCES invocation_specs(id) ON DELETE CASCADE,
        payload_json TEXT NOT NULL)''')
    connection.execute('''CREATE TABLE IF NOT EXISTS tool_verification_details (
        identity TEXT PRIMARY KEY, payload_json TEXT NOT NULL)''')
    connection.execute('''CREATE TABLE IF NOT EXISTS task_goal_execution (
        task_id TEXT PRIMARY KEY REFERENCES execution_wait_tasks(id) ON DELETE CASCADE,
        goal_revision INTEGER NOT NULL, request_digest TEXT NOT NULL,
        payload_json TEXT NOT NULL)''')
    # Historical outcomes remain unchanged. Legacy implicit work rows are not
    # imported as inventory or declared goals: resumed tasks revalidate sources.
