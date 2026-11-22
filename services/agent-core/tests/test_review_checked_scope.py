from agent import review_table_context as context


def test_sequenced_table_exposes_next_operation_within_existing_budget():
    rows = [('Step', 'Operation'), ('1', 'Disconnect'), ('2', 'Reconnect'), ('3', 'Verify')]
    fields = [dict(id=f't-1-r{r}-c{c}', kind='table_cell', text=value)
              for r, row in enumerate(rows) for c, value in enumerate(row)]
    target = fields[3]
    scope = context.table_contexts(fields, [target['id']])[target['id']]
    ids = context.checked_scope_ids({**target, 'table_context': scope})
    assert 't-1-r2-c1' in ids and 't-1-r3-c1' not in ids
    assert target['id'] == ids[0]
    assert sum(len(cell['text']) for cell in scope['cells']) <= 1024
    fields[0]['text'] = 'Metric'
    plain = context.table_contexts(fields, [target['id']])[target['id']]
    assert 't-1-r2-c1' not in context.checked_scope_ids({**target, 'table_context': plain})


def test_numbered_paragraphs_only_share_adjacent_source_in_same_section():
    fields = [dict(id=f'p-{i}', section_id=section, text=text) for i, (section, text) in enumerate([
        ('a','1) Disconnect.'), ('a','2) Wait.'), ('a','3) Reconnect.'), ('b','4) Inspect.')])]
    refs = context.paragraph_table_references(fields, ['p-1', 'p-2'])
    assert refs['p-1']['section_context'] == {'p-0':'1) Disconnect.', 'p-2':'3) Reconnect.'}
    assert context.checked_scope_ids({**fields[2], 'referenced_table_context':refs['p-2']}) == ['p-2','p-1']


def test_scope_does_not_count_labels_empty_text_or_unseen_references():
    target = dict(id='p-3', text='See table below.', referenced_table_context={
        'tables': [{'id':'t-0','cells':{'t-0-r0-c0':' ', 't-0-r1-c0':'Checked'}}],
        'section_context':{'p-2':'Applicability'}},
        table_context={'nearest_nonempty_anchor_ids':{'above':'unseen'},'cells':[]})
    assert set(context.checked_scope_ids(target)) == {'p-3','p-2','t-0-r1-c0'}
