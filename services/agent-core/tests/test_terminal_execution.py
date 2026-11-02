import asyncio
import io
import json
import logging
import sqlite3
import sys

import pytest

from agent.storage import CoreDataError, CoreRepository
from agent.tool_contracts import ToolCall, ToolContractRegistry, validate_tool_call
from agent.tool_loop import ToolLoopService
from test_tool_repository import started_run


def test_terminal_audit_is_dispatched_through_actual_stdio_entrypoint(tmp_path, monkeypatch, capsys):
    from agent.main import _run

    repository, tools, run, attempt = started_run(tmp_path)
    operation = tools.create(run['id'], attempt['id'], 1, ToolCall('audit', 'terminal.run', {'command': 'exit 7'}))
    result = json.dumps(dict(stdout='persisted stdout', stderr='persisted stderr', exit_code=7,
                            duration_ms=12, timed_out=False, cancelled=False,
                            stdout_truncated=False, stderr_truncated=False))
    tools.transition(operation['id'], {'requested'}, 'completed', result_summary=result)
    request = {'v': 16, 'id': 'stdio-audit', 'type': 'tool.operation.list_conversation',
               'params': {'conversation_id': run['conversation_id']}}
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(request) + '\n'))
    asyncio.run(_run(repository, logging.getLogger('terminal-stdio-test')))
    response = next(json.loads(line) for line in capsys.readouterr().out.splitlines()
                    if json.loads(line).get('id') == 'stdio-audit')
    assert 'error' not in response, response
    [persisted] = response['result']['operations']
    assert persisted['id'] == operation['id']
    assert persisted['result_summary'] == result


def test_terminal_contract_advertises_actual_shell_and_bounds():
    definition = ToolContractRegistry.definitions({'terminal.run'})[0]['function']
    assert 'PowerShell' in definition['description']
    assert definition['parameters']['properties']['timeout_ms']['maximum'] == 600000
    assert validate_tool_call(ToolCall('terminal', 'terminal.run', {'command': 'Write-Output ok'}))


@pytest.mark.parametrize('patch', [
    {'command': ''}, {'command': ' '}, {'command': 'x' * 32769}, {'command': None},
    {'cwd': ''}, {'cwd': 'x' * 1025}, {'timeout_ms': True}, {'timeout_ms': 999},
    {'timeout_ms': 600001}, {'max_output_bytes': 1023}, {'max_output_bytes': 1048577},
    {'max_output_bytes': 3.5}, {'unexpected': 'ignored'},
])
def test_terminal_rejects_invalid_arguments(patch):
    with pytest.raises(CoreDataError) as error:
        validate_tool_call(ToolCall('terminal', 'terminal.run', {'command': 'exit 0', **patch}))
    assert error.value.code == 'invalid_tool_call'


def test_terminal_result_persists_nonzero_exit_and_cannot_replay(tmp_path):
    async def scenario():
        repository, tools, run, attempt = started_run(tmp_path)
        events = []
        async def emit(event):
            events.append(event)
        loop = ToolLoopService(tools, emit)
        call = ToolCall('terminal', 'terminal.run', {'command': 'exit 7'})
        task = asyncio.create_task(loop.request(run_id=run['id'], attempt_id=attempt['id'], sequence=1, call=call))
        await asyncio.sleep(0)
        operation = events[0]['data']['operation']
        result = dict(stdout='out' * 2000, stderr='problem', exit_code=7, duration_ms=123,
                      timed_out=False, cancelled=False, stdout_truncated=False, stderr_truncated=False)
        loop.resolve(operation['id'], success=True, output=json.dumps(result))
        completed, resolution = await task
        assert resolution.success
        assert json.loads(completed['result_summary']) == result
        with pytest.raises(CoreDataError):
            await loop.request(run_id=run['id'], attempt_id=attempt['id'], sequence=2, call=call)
        assert len(events) == 1
    asyncio.run(scenario())


def test_schema21_migration_preserves_tool_rows_reviews_indexes_and_wal_backup(tmp_path):
    repository, tools, run, attempt = started_run(tmp_path)
    operation = tools.create(run['id'], attempt['id'], 1, ToolCall('old', 'fs.read', {'path': 'a'}))
    tools.record_review(operation['id'], 'provider', 'model', 'approve', 'review', 12)
    with repository._connect() as connection:
        connection.execute('PRAGMA foreign_keys = OFF')
        sql = connection.execute("SELECT sql FROM sqlite_master WHERE name='tool_operations'").fetchone()[0]
        import re
        legacy = re.sub(r'CREATE TABLE ["`\[]?tool_operations["`\]]?', 'CREATE TABLE tool_operations_legacy', sql, count=1).replace(",'terminal.run'", "")
        indexes = [row[0] for row in connection.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='tool_operations' AND sql IS NOT NULL")]
        connection.execute(legacy)
        connection.execute('INSERT INTO tool_operations_legacy SELECT * FROM tool_operations')
        connection.execute('DROP TABLE tool_operations')
        connection.execute('ALTER TABLE tool_operations_legacy RENAME TO tool_operations')
        for index in indexes:
            connection.execute(index)
        connection.execute('UPDATE schema_info SET version = 21')
        before = [tuple(row) for row in connection.execute('SELECT * FROM tool_operations')]
        reviews = [tuple(row) for row in connection.execute('SELECT * FROM tool_reviews')]
    keeper = repository._connect()
    keeper.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    keeper.execute('PRAGMA wal_autocheckpoint=0')
    keeper.execute("UPDATE messages SET content='terminal WAL marker' WHERE id=?", (run['user_message_id'],))
    keeper.commit()
    CoreRepository(repository.database_path)
    with repository._connect() as connection:
        assert connection.execute('SELECT version FROM schema_info').fetchone()[0] == 23
        assert [tuple(row) for row in connection.execute('SELECT * FROM tool_operations')] == before
        assert [tuple(row) for row in connection.execute('SELECT * FROM tool_reviews')] == reviews
        assert not connection.execute('PRAGMA foreign_key_check').fetchall()
        indexes = {row[1] for row in connection.execute('PRAGMA index_list(tool_operations)')}
        assert {'idx_tool_operations_run', 'idx_tool_operations_status'} <= indexes
    tools.create(run['id'], attempt['id'], 2, ToolCall('new', 'terminal.run', {'command': 'exit 0'}))
    backups = list(tmp_path.glob('*.pre-schema23-*.bak'))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as connection:
        assert connection.execute('SELECT version FROM schema_info').fetchone()[0] == 21
        assert connection.execute('SELECT content FROM messages WHERE id=?', (run['user_message_id'],)).fetchone()[0] == 'terminal WAL marker'
    keeper.close()
