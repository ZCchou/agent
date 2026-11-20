import pytest

from agent.rag.retrieval import lexical_tokens, reciprocal_rank_fusion


def test_chinese_bigrams_and_exact_clause_tokens():
    tokens = lexical_tokens('25.1309 系统安全性')
    assert '系统' in tokens and '安全' in tokens and '全性' in tokens
    assert '25.1309' in tokens


@pytest.mark.parametrize('body', [
    '8.1 Scope\nThe equipment shall meet the specified performance requirements.',
    'The equipment shall meet specified performance requirements.',
    'This section applies to equipment in the specified environment.',
    '设备应满足规定的性能要求。',
    '本章适用于机载设备的环境鉴定。',
])
def test_publisher_header_does_not_hide_actual_scope_requirements(body):
    from agent.rag.retrieval import reference_material
    text = ('RTCA, Inc.\nWashington, DC\nRTCA/DO-160F\nSection 8\n'
            'Vibration\nIssue date: December 6, 2007\n' + body)
    assert reference_material(text, 'What performance must equipment meet?') is False


def test_rrf_rewards_both_channels_with_k60():
    scores = reciprocal_rank_fusion([['a', 'b'], ['b', 'c']])
    assert scores['b'] == 1 / 62 + 1 / 61
    assert scores['b'] > scores['a']


def test_vector_candidates_search_full_authorized_set_without_keyword_prefilter(tmp_path):
    from agent.storage import CoreRepository
    from agent.rag.repository import RagRepository
    from agent.rag.documents import parse_text
    from agent.rag.retrieval import candidates, vector_blob
    from contextlib import closing
    import json

    repository = RagRepository(CoreRepository(tmp_path / 'agent.db'))
    library = repository.create_library({'name': 'Library', 'embedding_model_id': 'embed', 'reranker_model_id': 'rank'})
    version = repository.save_version(library['id'], 'Regulation', parse_text('机械故障\n\n查询词\n\n保密内容'), '0' * 64, 'text.txt', 'text/plain')['version']['id']
    repository.confirm({'version_id': version, 'revision': '1', 'source': 'Authority', 'document_number': '25', 'title': 'Regulation'})
    with closing(repository.connect()) as connection, connection:
        chunks = connection.execute('SELECT id FROM rag_chunks WHERE version_id=? ORDER BY ordinal', (version,)).fetchall()
        connection.execute('''INSERT INTO rag_indexes(id,library_id,embedding_model_id,embedding_digest,reranker_model_id,reranker_digest,dimension,version_ids,chunk_count,created_at)
            VALUES ('index',?,'embed','digest','rank','digest',2,?,3,'now')''', (library['id'], json.dumps([version])))
        connection.executemany('INSERT INTO rag_index_chunks(index_id,chunk_id,embedding) VALUES (?,?,?)',
                               [('index', chunks[0]['id'], vector_blob([1, 0])), ('index', chunks[1]['id'], vector_blob([0, 1])), ('index', chunks[2]['id'], vector_blob([0, 1]))])
        ids, _ = candidates(connection, 'index', vector_blob([1, 0]), '完全不同的同义表达', count=1)
        assert ids == [chunks[0]['id']]
        ids, _ = candidates(connection, 'index', vector_blob([1, 0]), '完全不同的同义表达', version_ids=[version] * 40000, count=1)
        assert ids == [chunks[0]['id']]
        connection.execute("UPDATE rag_chunks SET clause_number='8.2.1.3' WHERE id=?", (chunks[2]['id'],))
        ids, _ = candidates(connection, 'index', vector_blob([1, 0]),
                            '请确认第8.2.1.3条的试验类型并给出物理页和原文', count=1)
        assert ids == [chunks[2]['id']]
        ids, _ = candidates(connection, 'index', vector_blob([1, 0]),
                            '请确认第8.2.1.4条的试验类型', count=1)
        assert ids == []
        connection.execute("UPDATE rag_versions SET title='DO-160F' WHERE id=?", (version,))
        assert candidates(connection, 'index', None, 'DO-160F clause 8.2.1.3')[0] == [chunks[2]['id']]
        assert candidates(connection, 'index', None, 'DO-160E clause 8.2.1.3')[0] == []
        assert candidates(connection, 'index', None, '请查DO-160E第8.2.1.3条')[0] == []
        connection.execute("UPDATE rag_versions SET title='RTCA DO-160', revision='F' WHERE id=?", (version,))
        assert candidates(connection, 'index', None, 'DO-160F clause 8.2.1.3.')[0] == [chunks[2]['id']]
        assert candidates(connection, 'index', None, 'DO-160F 8.2.1.3原文')[0] == [chunks[2]['id']]
        assert candidates(connection, 'index', None, 'DO-160F clause 8.2.1.3', version_ids=[])[0] == []


def test_100000_chunk_vector_scan_and_enforced_library_capacity(tmp_path):
    from agent.storage import CoreRepository, CoreDataError
    from agent.rag.repository import RagRepository
    from agent.rag.documents import parse_text
    from agent.rag.retrieval import candidates, vector_blob
    from contextlib import closing
    import json
    import time
    import pytest

    repository = RagRepository(CoreRepository(tmp_path / 'agent.db'))
    library = repository.create_library({'name': 'Capacity', 'embedding_model_id': 'embed', 'reranker_model_id': 'rank'})
    version = repository.save_version(library['id'], 'Capacity', parse_text('25.1309 initial'), '0' * 64, 'text.txt', 'text/plain')['version']['id']
    repository.confirm({'version_id': version, 'revision': '1', 'source': 'Authority', 'document_number': '25', 'title': 'Capacity'})
    near = vector_blob([1] + [0] * 63)
    far = vector_blob([0, 1] + [0] * 62)
    with closing(repository.connect()) as connection, connection:
        connection.execute('''WITH RECURSIVE numbers(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM numbers WHERE n<99999)
            INSERT INTO rag_chunks(id,version_id,ordinal,text,text_hash,clause_number,locator)
            SELECT 'capacity-' || n,?,n,'mechanical malfunction','hash',NULL,'{}' FROM numbers''', (version,))
        connection.execute('UPDATE rag_versions SET chunk_count=100000 WHERE id=?', (version,))
        connection.execute('''INSERT INTO rag_indexes(id,library_id,embedding_model_id,embedding_digest,reranker_model_id,reranker_digest,dimension,version_ids,chunk_count,created_at)
            VALUES ('capacity-index',?,'embed','digest','rank','digest',64,?,100000,'now')''', (library['id'], json.dumps([version])))
        connection.execute('''INSERT INTO rag_index_chunks(index_id,chunk_id,embedding)
            SELECT 'capacity-index',id,CASE WHEN id='capacity-99999' THEN ? ELSE ? END FROM rag_chunks WHERE version_id=?''', (near, far, version))
        started = time.perf_counter()
        ids, _ = candidates(connection, 'capacity-index', near, 'unrelated semantic paraphrase')
        elapsed = time.perf_counter() - started
        assert ids[0] == 'capacity-99999'
        assert len(ids) == 40
        print(f'100000 chunks, 64-dimensional sqlite-vec full cosine scan: {elapsed:.3f}s')
    with pytest.raises(CoreDataError) as error:
        repository.save_version(library['id'], 'Overflow', parse_text('extra'), '1' * 64, 'overflow.txt', 'text/plain')
    assert error.value.code == 'rag_capacity_exceeded'


@pytest.mark.parametrize('publisher,city', [('RTCA，INC.', 'Washington，DC 20036'), ('RTCA INC.', 'Washington, DC 20036')])
def test_pdf_publisher_cover_unicode_punctuation_is_not_technical_evidence(publisher, city):
    from agent.rag.retrieval import reference_material

    text = publisher + '\n1828 L Street\n' + city + '\nRTCA/DO－160F\n第 5 章\n温度变化\n发行日期：2007'
    assert reference_material(text, '通用试验条件和试验场地要求')
    assert not reference_material(text, '封面上的发行日期')


def test_spaced_preface_heading_is_reference_material():
    from agent.rag.retrieval import reference_material

    assert reference_material('前 言\n本文件由特别委员会制定。', '试验安装条件')
    assert not reference_material('前 言\n本文件由特别委员会制定。', '前言介绍了什么')


def test_appendix_cover_crossreferences_are_not_the_referenced_requirements():
    from agent.rag.retrieval import reference_material

    text = ('RTCA INC.\n1828 L Street\nWashington,DC 20036\nRTCA/DO－160F\n'
            '附录 A\n环境试验标识\n第 1、2、3 章中包含相关信息。\n发行日期：2007')
    assert reference_material(text, '通用试验条件')
    assert not reference_material(text + '\nA.1 设备应保存环境鉴定记录。', '设备记录要求')
