from agent.rag.retrieval import lexical_tokens, reciprocal_rank_fusion


def test_chinese_bigrams_and_exact_clause_tokens():
    tokens = lexical_tokens('25.1309 系统安全性')
    assert '系统' in tokens and '安全' in tokens and '全性' in tokens
    assert '25.1309' in tokens


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
