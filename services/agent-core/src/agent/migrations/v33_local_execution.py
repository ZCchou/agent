"""Local tool verification results and automatic execution defaults."""
import json


def migrate(connection):
    connection.execute('''CREATE TABLE IF NOT EXISTS tool_verifications (
        identity TEXT PRIMARY KEY, status TEXT NOT NULL,
        mode TEXT, error_json TEXT, updated_at TEXT NOT NULL)''')
    columns = {row[1] for row in connection.execute('PRAGMA table_info(execution_wait_tasks)')}
    if 'resume_pending' not in columns:
        connection.execute('ALTER TABLE execution_wait_tasks ADD COLUMN resume_pending INTEGER NOT NULL DEFAULT 0')
    # Preserve stored settings while activating continuous background execution.
    for row in connection.execute('SELECT id,policy_json FROM execution_wait_tasks').fetchall():
        policy = json.loads(row['policy_json'])
        policy.update(mode='adaptive', max_total_seconds=None, max_auto_recoveries=2)
        connection.execute('UPDATE execution_wait_tasks SET policy_json=? WHERE id=?',
                           (json.dumps(policy), row['id']))
