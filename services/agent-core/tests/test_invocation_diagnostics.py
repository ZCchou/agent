import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest

from agent.execution.compiler import InvocationCompiler
from agent.execution.invocation_diagnostics import record
from agent.model_invocation import ModelInvocationService
from agent.providers import ProviderError, ProviderEvent, ProviderProfile
from test_unified_invocation_v3 import model_spec


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
