import base64
import hashlib
import json

import pytest

from agent.storage import CoreDataError
from agent.tool_contracts import ToolCall, ToolContractRegistry, TOOL_CONTRACT_VERSION, validate_tool_call
from test_result_store import saved
from test_tool_repository import started_run


def test_explicit_result_hash_and_original_source_range(tmp_path):
    from agent.execution.results import ResultStore
    core, tools, run, attempt = started_run(tmp_path)
    op = tools.create(run['id'], attempt['id'], 1, ToolCall('read', 'document.read_attachment', {
        'attachment_id': 'attachment-A', 'offset': 42, 'limit': 100,
    }))
    body = json.dumps({'source_sha256': 'a' * 64, 'offset': 42, 'next_offset': 55, 'content': 'original'})
    store = ResultStore(core.database_path)
    result = store.save(op['id'], body, success=True)
    digest = hashlib.sha256(body.encode()).hexdigest()
    metadata = store.metadata(run['id'], op['id'])
    for value in (result, metadata, store.read(run['id'], op['id']), store.search(run['id'], op['id'], 'original'), store.read(run['id'])['results'][0]):
        assert value['result_sha256'] == value['sha256'] == digest
        assert value['source_ref']['source_sha256'] == 'a' * 64
        assert value['source_ref']['arguments']['attachment_id'] == 'attachment-A'
        assert value['source_ref']['range']['offset'] == 42
    assert metadata['source']['arguments']['attachment_id'] == 'attachment-A'


@pytest.mark.parametrize('limit', [512, 600, 1000])
def test_envelope_retains_identity_status_and_read_entry_with_escaped_preview(tmp_path, limit):
    store, run, _, result = saved(tmp_path, '\\"\n' * 1000)
    rendered = store.envelope(result, max_characters=limit)
    value = json.loads(rendered)
    assert len(rendered) <= limit
    assert value['result_sha256'] == result['sha256']
    assert value['success'] and value['complete']
    assert value['read_entry'] == {'result_id': result['result_id']}
    assert list(value).index('read_entry') < list(value).index('preview')


def test_impossible_envelope_is_bounded_json_error(tmp_path):
    store, _, _, result = saved(tmp_path)
    rendered = store.envelope(result, max_characters=128)
    assert len(rendered) <= 128
    assert json.loads(rendered)['error_code'] == 'tool_result_budget_too_small'


@pytest.mark.parametrize('cursor', ['0', 'forged', '', base64.urlsafe_b64encode(b'["other","hash",0]').decode()])
def test_invalid_cursor_has_safe_first_read_after_scope_validation(tmp_path, cursor):
    store, run, _, result = saved(tmp_path)
    with pytest.raises(CoreDataError) as caught:
        store.read(run['id'], result['result_id'], cursor=cursor, length=25)
    assert caught.value.code == 'tool_result_cursor_invalid'
    assert caught.value.details['first_read'] == {'result_id': result['result_id'], 'cursor': None, 'length': 25}
    assert store.read(run['id'], **caught.value.details['first_read'])['offset'] == 0
    with pytest.raises(CoreDataError) as forbidden:
        store.read('other-run', result['result_id'], cursor=cursor)
    assert forbidden.value.code == 'tool_result_forbidden'
    assert forbidden.value.details is None


def test_null_cursor_contract_and_existing_cursor_roundtrip(tmp_path):
    call = ToolCall('read', 'tools.read_result', {'result_id': 'r', 'cursor': None})
    assert validate_tool_call(call) == call
    definition = ToolContractRegistry.definitions({'tools.read_result'})[0]['function']
    assert definition['parameters']['properties']['cursor']['type'] == ['string', 'null']
    assert 'next_cursor' in definition['description'] and 'null' in definition['description']
    assert TOOL_CONTRACT_VERSION == 'tool-contract-v9'
    store, run, _, result = saved(tmp_path)
    first = store.read(run['id'], result['result_id'], cursor=None, length=5)
    assert store.read(run['id'], result['result_id'], cursor=first['next_cursor'], length=5)['offset'] == 5


def test_provenance_is_bounded_and_keeps_distinct_attachment_identity(tmp_path):
    from agent.execution.results import ResultStore
    core, tools, run, attempt = started_run(tmp_path)
    store = ResultStore(core.database_path)
    identities = []
    for sequence, attachment_id in enumerate(('attachment-A', 'attachment-B'), 1):
        op = tools.create(run['id'], attempt['id'], sequence, ToolCall(str(sequence), 'document.read_attachment', {
            'attachment_id': attachment_id, 'section': 'section' * 100,
        }))
        body = json.dumps({'name': 'same.pdf', 'source_sha256': str(sequence) * 64,
                           'offset': 10 ** 300, 'content': 'same text'})
        result = store.save(op['id'], body, success=True)
        reference = result['source_ref']
        identities.append(reference['arguments']['attachment_id'])
        assert reference['truncated']
        assert 'offset' not in reference['range']
        assert len(json.dumps(reference)) < 2048
        assert 'same.pdf' not in json.dumps(reference)
    assert identities == ['attachment-A', 'attachment-B']


def test_catalog_cursor_correction_and_explicit_result_ranges(tmp_path):
    store, run, _, result = saved(tmp_path, 'abc original xyz')
    with pytest.raises(CoreDataError) as caught:
        store.read(run['id'], cursor='0')
    assert caught.value.details['first_read'] == {'cursor': None}
    catalog = store.read(run['id'], **caught.value.details['first_read'])
    assert catalog['results'][0]['result_range'] == {'start': 0, 'end': 16, 'unit': 'character'}
    page = store.read(run['id'], result['result_id'], length=5)
    assert page['result_range'] == {'start': 0, 'end': 5, 'unit': 'character'}
    match = store.search(run['id'], result['result_id'], 'original')['matches'][0]
    assert match['result_range'] == {'start': 0, 'end': 16, 'unit': 'character'}

def test_envelope_accepts_legacy_saved_metadata(tmp_path):
    store, _, _, result = saved(tmp_path)
    legacy = {key: value for key, value in result.items() if key not in {'source_ref', 'result_sha256'}}
    envelope = json.loads(store.envelope(legacy, max_characters=512))
    assert envelope['result_sha256'] == legacy['sha256']
    assert envelope['read_entry'] == {'result_id': legacy['result_id']}
