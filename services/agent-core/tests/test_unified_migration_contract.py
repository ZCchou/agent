from agent.storage import CoreRepository, SCHEMA_VERSION
from agent.protocol import BUILD_ID, PROTOCOL_VERSION


def test_unified_schema_upgrades_v29_and_preserves_history(tmp_path):
    repo = CoreRepository(tmp_path / 'agent.db')
    chat = repo.create_conversation('standard_chat', 'Existing', category='casual')
    repo._insert_message(chat['id'], 'user', 'Saved history')
    with repo._connect() as connection:
        connection.execute('DROP TABLE IF EXISTS profile_migration_sources')
        connection.execute('DROP TABLE IF EXISTS profile_migration_jobs')
        connection.execute('UPDATE schema_info SET version=29')
    upgraded = CoreRepository(repo.database_path)
    assert SCHEMA_VERSION == 31
    assert PROTOCOL_VERSION == 24
    assert BUILD_ID == 'schema32-agent-unified-v1'
    assert upgraded.get_conversation(chat['id'])['messages'][0]['content'] == 'Saved history'
    with upgraded._connect() as connection:
        assert connection.execute('SELECT count(*) FROM profile_migration_sources').fetchone()[0] == 0
        assert connection.execute('SELECT count(*) FROM profile_migration_jobs').fetchone()[0] == 0
        assert not connection.execute('PRAGMA foreign_key_check').fetchall()
