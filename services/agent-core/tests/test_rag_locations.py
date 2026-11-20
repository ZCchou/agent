import asyncio
import copy
import json

import pytest


def source(text='图4-2 低温工作\n图4－3 地面高温耐受', **locator):
    return {'id': 'evidence', 'version_id': 'version', 'text': text, 'text_hash': 'immutable',
            'original_hash': 'original', 'clause_number': '4.6.2',
            'locator': {'kind': 'pdf', 'page': 21, 'block_id': 'block-67',
                        'clause_parent_id': 'block-65', **locator}}


def test_inherited_pdf_caption_uses_own_block_and_preserves_identity():
    from agent.rag.locations import normalize_source_location
    original = source()
    before = copy.deepcopy(original)
    result = normalize_source_location(original)
    assert original == before
    assert result['clause_number'] is None
    assert result['locator']['clause_parent_id'] == 'block-67'
    assert result['locator']['illustration_labels'] == ['图4-2', '图4-3']
    assert result['locator']['clause_verified'] is False
    assert result['location_diagnostic']['inherited_clause_number'] == '4.6.2'
    for key in ('id', 'version_id', 'text', 'text_hash', 'original_hash'):
        assert result[key] == original[key]
    assert normalize_source_location(result) == result


@pytest.mark.parametrize('record', [
    source(text='设备应满足要求，参见图4-2。'),
    source(heading_path=['4.6.2 减压']),
    source(block_id='block-65'),
    source(kind='docx'),
])
def test_verified_headings_and_ordinary_continuations_are_preserved(record):
    from agent.rag.locations import normalize_source_location
    assert normalize_source_location(record) == record


def test_fragment_without_caption_uses_complete_same_block():
    from agent.rag.locations import normalize_source_location
    fragment = source(text='设备运行区间')
    result = normalize_source_location(fragment, block_text='图4—2 低温工作\n设备运行区间')
    assert result['clause_number'] is None
    assert result['text'] == '设备运行区间'
    assert result['locator']['illustration_labels'] == ['图4-2']


def test_caption_labels_do_not_expose_inherited_clause_to_model():
    from agent.rag.locations import normalize_source_location
    from agent.rag.grounding import source_label, evidence_for_model
    record = normalize_source_location({**source(), 'title': 'DO-160F', 'revision': 'F'})
    label = source_label(record)
    assert '图4-2' in label and '物理页 21' in label
    assert '4.6.2' not in label
    assert '4.6.2' not in json.dumps(evidence_for_model(record))


def test_legacy_caption_isolation_at_search_preview_and_both_context_directions(tmp_path):
    from agent.rag.documents import _chunks, MAX_TEXT_CHARS
    from agent.rag.service import RagService
    from agent.storage import CoreRepository
    from agent.rag.retrieval import exact_clause_candidates
    from test_rag_service import Engines, wait_job

    async def exercise():
        core = CoreRepository(tmp_path / 'agent.db')
        project = core.create_project('Location proof', 'work')['id']
        service = RagService(core, Engines())
        try:
            library = service.repository.create_library({'name': 'DO-160F', 'embedding_model_id': 'embed', 'reranker_model_id': 'rank'})
            caption = '图4-2 低温工作\n' + '图示设备工作区间。' * 200
            parsed = _chunks([
                ('4.6.2 减压\n设备应符合减压要求。', {'kind': 'pdf', 'page': 18}),
                ('应保持性能。', {'kind': 'pdf', 'page': 19}),
                (caption, {'kind': 'pdf', 'page': 21}),
            ], [], MAX_TEXT_CHARS)
            version = service.repository.save_version(library['id'], 'DO-160F', parsed, 'a' * 64, 'source.pdf', 'application/pdf')['version']['id']
            service.repository.confirm({'version_id': version, 'revision': 'F', 'accept_warnings': True, 'source': 'RTCA', 'document_number': 'DO-160F', 'title': 'DO-160F'})
            index_job = await service.handle('index.build', {'library_id': library['id']})
            assert (await wait_job(service, index_job))['status'] == 'completed'
            await service.handle('library.bind', {'library_id': library['id'], 'project_id': project, 'enabled': True})
            with service.repository.connect() as connection:
                raw = [dict(row) for row in connection.execute('SELECT * FROM rag_chunks WHERE version_id=? ORDER BY ordinal', (version,))]
                index_id = connection.execute('SELECT active_index_id FROM rag_libraries WHERE id=?', (library['id'],)).fetchone()[0]
                exact, _ = exact_clause_candidates(connection, index_id, 'DO-160F clause 4.6.2')
            figure_raw = [row for row in raw if json.loads(row['locator'])['page'] == 21]
            assert len(figure_raw) > 1
            assert all(row['clause_number'] == '4.6.2' for row in figure_raw)
            assert not set(exact).intersection(row['id'] for row in figure_raw)
            preview = service.repository.preview(version, page=21)['chunks']
            assert all(row['clause_number'] is None for row in preview)
            assert all(row['locator']['illustration_labels'] == ['图4-2'] for row in preview)
            found = (await service.search(project, '设备', run_id='locations'))['evidence']
            figure = next(row for row in found if row['locator']['page'] == 21)
            clause = next(row for row in found if row['locator']['page'] == 18)
            assert figure['clause_number'] is None
            for record in (figure, clause):
                actual = service.read_evidence(project, record['id'])
                assert actual['id'] == record['id'] and actual['text_hash'] == record['text_hash']
            figure_context = service.read_evidence(project, figure['id'])['parent_context']
            assert figure_context['scope'] == 'block' and figure_context['text'] == caption
            clause_context = service.read_evidence(project, clause['id'])['parent_context']['text']
            assert '应保持性能' in clause_context and '图4-2' not in clause_context
            # Historical persisted payloads are also corrected after authorization.
            old = next(row for row in figure_raw if row['text'] == figure['text'])
            legacy = {**figure, 'clause_number': old['clause_number'], 'locator': json.loads(old['locator'])}
            with service.repository.connect() as connection:
                connection.execute('UPDATE rag_evidence SET payload=? WHERE id=?', (json.dumps(legacy), figure['id']))
            assert service.repository.checked_evidence(project, figure['id'])['clause_number'] is None
            with pytest.raises(Exception) as error:
                service.repository.checked_evidence(project, figure['id'], run_id='another-run')
            assert error.value.code == 'rag_evidence_invalid'
        finally:
            await service.close()
    asyncio.run(exercise())



def test_complete_pdf_blocks_do_not_query_storage():
    from agent.rag.locations import located_source
    class NoReads:
        def execute(self, *args):
            raise AssertionError('A complete block must use its own text')
    assert located_source(NoReads(), source(fragment_count=1))['clause_number'] is None


def test_fragment_cache_reads_requested_blocks_in_one_version_scan():
    import sqlite3
    from agent.rag.locations import located_source, prime_location_cache
    connection = sqlite3.connect(':memory:')
    connection.row_factory = sqlite3.Row
    connection.execute('CREATE TABLE rag_chunks(version_id TEXT,text TEXT,locator TEXT,ordinal INTEGER)')
    sources = []
    for index in range(20):
        record = source(text='后续内容', block_id=f'block-{index}', clause_parent_id='parent', fragment_count=2)
        sources.append(record)
        connection.execute('INSERT INTO rag_chunks VALUES (?,?,?,?)', ('version', f'图4-{index+1} 图示\n', json.dumps(record['locator']), index*2))
        connection.execute('INSERT INTO rag_chunks VALUES (?,?,?,?)', ('version', '后续内容', json.dumps(record['locator']), index*2+1))
    queries = []
    connection.set_trace_callback(lambda sql: queries.append(sql) if sql.lstrip().upper().startswith('SELECT') else None)
    cache = prime_location_cache(connection, sources)
    results = [located_source(connection, record, cache) for record in sources]
    assert len(queries) == 1
    assert all(record['clause_number'] is None for record in results)
