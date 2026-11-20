import json

from agent.document_review import _fits, review_request
from agent.review_table_context import paragraph_table_references


def paragraph(index, text, section='chapter'):
    return {'id': f'p-{index}', 'section_id': section, 'text': text, 'chapter_path': [section]}


def table(index, rows, section='chapter'):
    return [{'id': f't-{index}-r{row}-c{col}', 'text': text, 'section_id': section}
            for row, values in enumerate(rows) for col, text in enumerate(values)]


def test_directional_original_context_is_visible_and_budgeted_but_never_an_external_citation_or_review_target():
    first = paragraph(2, '各阶段按下表控制。')
    last = paragraph(3, '上述a至h完成后累计循环数。')
    fields = [paragraph(1, '目的：所选A类。'), first, *table(2, [['步骤','时长'],['a','2 h±10 min']]),
              last, *table(3, [['另一张表']])]
    refs = paragraph_table_references(fields, ['p-2', 'p-3'])
    assert refs['p-2']['tables'][0]['cells']['t-2-r1-c1'] == '2 h±10 min'
    assert refs['p-3']['tables'][0]['id'] == 't-2'
    assert refs['p-3']['section_context'] == {'p-1': '目的：所选A类。'}
    batch = {'fields': [{**first, 'referenced_table_context':refs['p-2']}], 'target_ids':['p-2'], 'freeform_review':True}
    messages, _ = review_request(batch, {'evidence':[]})
    payload = json.loads(messages[-1]['content'])
    assert payload['batch']['target_ids'] == ['p-2'] and payload['sources']['evidence'] == []
    assert 't-2-r1-c1' in messages[-1]['content'] and 'does not establish' in messages[0]['content']
    from agent.context_engine.policy import estimate_tokens
    bare = {**batch, 'fields':[first]}
    cost = estimate_tokens({'messages': review_request(bare, {'evidence': []})[0], 'response_schema': None, 'tools': []}) + 256
    assert _fits(bare, {'evidence':[]}, cost)
    assert not _fits(batch, {'evidence':[]}, cost)


def test_references_are_local_bounded_and_require_a_directional_phrase():
    fields = [paragraph(1,'见下表'), *table(1,[['A'*310]*4]*20), paragraph(2,'普通描述'),
              paragraph(3,'按如下方法处理。'), paragraph(4,'见下表'), *table(2,[['其他章节']], 'other')]
    result = paragraph_table_references(fields, [f'p-{i}' for i in range(1, 5)])
    assert set(result) == {'p-1','p-4'}
    assert result['p-4']['tables'] == []
    excerpt = result['p-1']['tables'][0]
    assert excerpt['excerpt_only'] and len(excerpt['cells']) <= 32
    assert sum(map(len,excerpt['cells'].values())) <= 1500
