from agent.context_engine.policy import estimate_tokens
from agent.rag.review_queries import review_evidence_window, review_queries


def test_indexed_measurements_respect_physical_units_source_scope_and_top_k():
    import sqlite3
    from agent.rag.review_queries import indexed_quantity_table,retain_quantity_table
    from agent.rag.retrieval import lexical_tokens
    db=sqlite3.connect(':memory:')
    db.row_factory=sqlite3.Row
    db.executescript("""
        CREATE VIRTUAL TABLE rag_fts USING fts5(chunk_id UNINDEXED,tokens);
        CREATE TABLE rag_chunks(id,text,locator,version_id);
        CREATE TABLE rag_versions(id,status);
        CREATE TABLE rag_index_chunks(index_id,chunk_id);
    """)
    for key,unit,version,index,status in [('good','kPa','v1','i','confirmed'),('wrong','mbar','v1','i','confirmed'),
            ('other','kPa','v2','other-index','confirmed'),('withdrawn','kPa','v3','i','withdrawn')]:
        text=f'Table 2 pressure\n{unit} (Pa)\n100ft 9.45 9450\n500ft 6.72 6720'
        db.execute("insert into rag_chunks values(?,?,'{}',?)",(key,text,version))
        db.execute('insert into rag_versions select ?,? where not exists(select 1 from rag_versions where id=?)',(version,status,version))
        db.execute('insert into rag_index_chunks values(?,?)',(index,key))
        db.execute('insert into rag_fts values(?,?)',(key,' '.join(lexical_tokens(text))))
    for v in ['9.45kPa → 6.72kPa','Topic\n9.45kPa → 6.72kPa\n2h','other\n25s\n9.45kPa → 6.72kPa']:
        assert indexed_quantity_table(db,'i',{'v1'},v)=='good'
    for v in ['9.45→6.72','9.45Pa → 6.72Pa','9.45kPa\n6.72kPa']:
        assert indexed_quantity_table(db,'i',{'v1'},v) is None
    for versions in [[],['v2'],['v3']]:
        assert indexed_quantity_table(db,'i',versions,'9.45kPa → 6.72kPa') is None
    ranked=[{'id':key} for key in ['procedure','other','good']]
    assert retain_quantity_table(ranked,2,{'good'})==[ranked[0],ranked[2]]
    assert retain_quantity_table(ranked,1,{'good'})==ranked[:1]


def cells(texts, row=1):
    return [dict(id=f't-3-r{row}-c{i}',text=text,chapter_path=['压力']) for i,text in enumerate(texts)]


def source(text, key='table'):
    return dict(id=key,title='Standard',revision='F',text=text,locator={'page':20})


def test_multiple_topics_keep_physical_row_quantities_in_query():
    fields = [*cells(['75.26kPa→11.60kPa','<15s','飞机最大飞行高度按+50,000ft计算。']),
              dict(id='p-6',text='试验准备',chapter_path=['其他'])]
    query, other = review_queries('审阅',dict(fields=fields),'',400)
    assert all(v in query for v in ['75.26kPa','11.60kPa','<15s','50,000ft'])
    assert '75.26' not in other


def test_matched_table_rows_include_original_units_location_quote_ids_within_budget():
    fields=cells(['75.26kPa→11.60kPa'])
    table=source('表 4－2 压力换算\n压力高度\n绝对压力\nkPa (mbars)\n＋8，000ft 75.26 752.6\n'
                 '＋50，000ft 11.60 116.0\n＋55，000ft 9.12 91.2\n')
    unrelated=source('Other content. '*30,'irrelevant')
    result=review_evidence_window([unrelated,table],fields,500)
    item=result[0][1]
    assert item['id']=='table' and item['locator']=={'page':20}
    options=item['quote_options']
    assert len(options)==3 and len({v['quote_id'] for v in options})==3
    assert all(v['text'] in table['text'] and v['illustration_labels']==['表4-2'] for v in options)
    assert 'kPa' in options[0]['text'] and '55，000' not in ''.join(v['text'] for v in options)
    assert estimate_tokens([i for _,i in result])<=500


def test_wrong_unit_column_or_cross_row_numbers_do_not_promote():
    from agent.rag.review_queries import _table_row_excerpts, _row_quantities
    original=source('表 4-2 压力\nkPa (mbars)\n8000ft 75.26 752.6\n50000ft 11.60 116.0\n')
    for fields in [cells(['752.6kPa→116.0kPa']), cells(['75.26','11.60']),
                   [*cells(['75.26kPa']),*cells(['11.60kPa'],row=2)]]:
        assert _table_row_excerpts(original,_row_quantities(fields))==[]
