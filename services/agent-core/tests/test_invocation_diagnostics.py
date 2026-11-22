import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest
import httpx
import httpcore

from agent.execution.compiler import InvocationCompiler
from agent.execution.invocation_diagnostics import record
from agent.model_invocation import ModelInvocationService
from agent.providers import ProviderError, ProviderEvent, ProviderProfile
from test_unified_invocation_v3 import model_spec


def test_network_failure_diagnostics_allow_only_transport_types_and_native_codes(tmp_path):
    database=tmp_path/'network.db'
    repository=SimpleNamespace(_connect=lambda: sqlite3.connect(database))
    spec=model_spec()
    with repository._connect() as db:
        db.execute('CREATE TABLE invocation_diagnostics(spec_id TEXT PRIMARY KEY,payload_json TEXT)')
        db.execute('INSERT INTO invocation_diagnostics VALUES(?,?)',(spec.spec_id,'{"text_characters":18}'))
    native=ConnectionResetError(10054,'private socket path')
    native.winerror=10054
    core=httpcore.ReadError('secret body')
    core.__cause__=native
    error=ProviderError('connection_reset','private URL',details={'winerror':10061,'network_error':'private'})
    error.__cause__=httpx.ReadError('secret URL')
    error.__cause__.__cause__=core
    record(repository,spec,error=error)
    record(repository,spec)
    with repository._connect() as db:
        result=db.execute('SELECT payload_json FROM invocation_diagnostics').fetchone()[0]
    value=json.loads(result)
    assert value['network_error']=={'types':['httpx.ReadError','httpcore.ReadError'],'errno':10054,'winerror':10054}
    assert value['text_characters']==18
    assert 'private' not in result and 'secret' not in result and '10061' not in result

    unknown=type('PrivateProviderName',(httpx.ReadError,),{})('private URL')
    unknown.__cause__=OSError(123456,'private message')
    unknown.__cause__.winerror=True
    unknown.__cause__.__context__=unknown
    error.__cause__=unknown
    with repository._connect() as db:
        db.execute('DELETE FROM invocation_diagnostics')
    record(repository,spec,error=error)
    with repository._connect() as db:
        value=json.loads(db.execute('SELECT payload_json FROM invocation_diagnostics').fetchone()[0])
    assert 'network_error' not in value


def test_outcome_records_safe_counts_and_preserves_partial_flags(tmp_path):
    database = tmp_path / 'diagnostics.db'
    repository = SimpleNamespace(_connect=lambda: sqlite3.connect(database))
    with repository._connect() as connection:
        connection.execute('CREATE TABLE invocation_diagnostics(spec_id TEXT PRIMARY KEY,payload_json TEXT)')
    spec = model_spec(max_output_tokens=1586)
    error = ProviderError('provider_response_incomplete', 'private document body',
                          details={'incomplete_reason': 'length', 'authorization': 'secret'})
    record(repository, spec, finish_reason='length', text_characters=10, tool_characters=5,
           usage={'input_tokens': 123, 'output_tokens': 1586, 'prompt': 'secret'}, error=error)
    record(repository, spec)
    with repository._connect() as connection:
        row = connection.execute('SELECT payload_json FROM invocation_diagnostics WHERE spec_id=?', (spec.spec_id,)).fetchone()
    value = json.loads(row[0])
    assert value['partial_text'] is True and value['partial_tool_arguments'] is True
    assert value['finish_reason'] == 'length' and value['max_output_tokens'] == 1586
    assert value['usage'] == {'input_tokens': 123, 'output_tokens': 1586}
    assert 'secret' not in row[0] and 'private document' not in row[0]


def test_hidden_invocation_error_retains_raw_finish_and_partial_counts():
    async def scenario():
        async def stream(*args, **kwargs):
            yield ProviderEvent('text_delta', text='partial private text')
            yield ProviderEvent('finish', finish_reason='length')
            yield ProviderEvent('usage', usage={'input_tokens': 40, 'output_tokens': 80})

        spec = model_spec()
        provider = SimpleNamespace(profile=ProviderProfile('provider', 'name', 'openai_compatible',
                                                           'https://example.invalid', primary_protocol='openai_responses'), stream_events=stream)
        prepared = InvocationCompiler().compile_model(spec, [{'role': 'user', 'content': 'request'}])
        with pytest.raises(ProviderError) as caught:
            await ModelInvocationService().complete_prepared(provider, spec, prepared)
        assert caught.value.details['finish_reason'] == 'length'
        assert caught.value.details['text_character_count'] == 20
        assert caught.value.details['partial_text'] is True
        assert 'partial private text' not in json.dumps(caught.value.details)

    asyncio.run(scenario())
