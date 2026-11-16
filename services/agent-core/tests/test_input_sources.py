import hashlib
import json

import pytest

from agent.storage import CoreDataError, CoreRepository


def source_run(tmp_path, content):
    core = CoreRepository(tmp_path / 'core.db')
    conversation = core.create_conversation('standard_chat', 'Input', category='casual')
    run = core.start_model_run(content=content, idempotency_key='input', provider_profile_id='p',
                               model_id='m', conversation_id=conversation['id'])
    return core, run


def test_long_pasted_input_can_enter_the_managed_reading_route(tmp_path):
    from agent.execution.input_sources import UserInputSources
    content = ('保持全部要求\n' * 10000).strip()
    core, run = source_run(tmp_path, content)
    store = UserInputSources(core.database_path)
    assert store.save(run['id'])['character_count'] == len(content)
    assert store.read(run['id'], length=2000)['content'] == content[:2000]


def test_projection_keeps_ordinary_prompt_and_bounds_oversized_unicode_source():
    from agent.execution.input_sources import project_input, input_tokens
    assert project_input('Summarize this text.', 2000) == 'Summarize this text.'
    content = 'START requirement\n' + '原始🌟要求\n' * 5000 + 'END requirement'
    projected = project_input(content, 700)
    assert input_tokens(projected) <= 700
    value = json.loads(projected)
    assert value['result_id'] == 'user-input'
    assert value['sha256'] == hashlib.sha256(content.encode()).hexdigest()
    assert value['character_count'] == len(content)
    assert 'START requirement' in value['preview'] and 'END requirement' in value['preview']
    assert value['coverage_required'] == 'entire_source_before_full_task_completion'
    assert 'tools.read_result' in value['instruction']


def test_saved_source_reloads_complete_text_and_binds_cursors_to_run(tmp_path):
    from agent.execution.input_sources import UserInputSources
    content = '开始\n' + '用户要求🌟\n' * 2200 + '最后一项'
    core, run = source_run(tmp_path, content)
    sources = UserInputSources(core.database_path)
    saved = sources.save(run['id'])
    assert saved['result_id'] == 'user-input'
    assert saved['byte_size'] == len(content.encode())
    with core._connect() as connection:
        connection.execute('UPDATE messages SET content=? WHERE id=?', ('later edit', run['user_message_id']))
    restored = UserInputSources(core.database_path)
    pieces, cursor = [], None
    while True:
        page = restored.read(run['id'], cursor=cursor, length=512)
        pieces.append(page['content'])
        cursor = page['next_cursor']
        if cursor is None:
            break
    assert ''.join(pieces) == content
    with pytest.raises(CoreDataError):
        restored.read('unrelated', length=10)
    with pytest.raises(CoreDataError):
        restored.read(run['id'], cursor='forged')
    with pytest.raises(CoreDataError):
        restored.save(run['id'], 'replacement')


def test_input_search_returns_original_offsets_and_read_cursor(tmp_path):
    from agent.execution.input_sources import UserInputSources
    core, run = source_run(tmp_path, 'prefix 原始要求 middle 原始要求 end')
    sources = UserInputSources(core.database_path)
    sources.save(run['id'])
    found = sources.search(run['id'], '原始要求', limit=1)
    assert found['truncated']
    match = found['matches'][0]
    assert match['offset'] == 7
    assert '原始要求' in sources.read(run['id'], cursor=match['cursor'])['content']


def test_routing_long_supplement_keeps_source_identity_and_defers_interpretation():
    from agent.rag.routing import intent_payload
    content = 'Please process all requirements.\n\n补充信息\n' + '原始要求🌟\n' * 5000 + 'FINAL MUST RETAIN'
    payload = intent_payload(content, [], [], 'casual', budget_bytes=1800)
    assert len(payload.encode()) <= 1800
    value = json.loads(payload)
    assert value['request_truncated']
    assert value['input_source']['result_id'] == 'user-input'
    assert value['input_source']['sha256'] == hashlib.sha256(content.encode()).hexdigest()
    assert value['input_source']['defer_interpretation'] is True
    assert value['input_source']['routing_hint'] == {'files': 'available', 'tools': 'available', 'reason': 'raw_input_read_required'}
    assert 'FINAL MUST RETAIN' in value['request']


def test_ordinary_routing_input_is_preserved_without_source_projection():
    from agent.rag.routing import intent_payload
    content = 'Read the attached document and list the dates.'
    value = json.loads(intent_payload(content, [], [], 'casual', budget_tokens=2000))
    assert value['request'] == content
    assert not value['request_truncated']
    assert 'input_source' not in value


def test_source_integrity_and_run_identity_are_verified(tmp_path):
    from agent.execution.input_sources import UserInputSources
    content = 'A full input that requires two pages'
    core, run = source_run(tmp_path, content)
    sources = UserInputSources(core.database_path)
    metadata = sources.save(run['id'])
    page = sources.read(run['id'], length=5)
    chat = core.create_conversation('standard_chat', 'Other', category='casual')
    other = core.start_model_run(content=content, idempotency_key='other', provider_profile_id='p',
                                 model_id='m', conversation_id=chat['id'])
    sources.save(other['id'])
    with pytest.raises(CoreDataError):
        sources.read(other['id'], cursor=page['next_cursor'])
    (sources.root / 'objects' / (metadata['sha256'] + '.txt')).write_text('corrupt', encoding='utf-8')
    with pytest.raises(CoreDataError):
        sources.read(run['id'])
