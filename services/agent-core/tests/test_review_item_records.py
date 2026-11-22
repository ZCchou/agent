from agent.document_review import _public_target_delivery
from agent.review_presentation import prepare_public_section


def item_batch(raw, ids, *, style='default', state='present'):
    refs=[dict(id=f'rag-{n}',quote_id=f'q-{n}',quote=f'摘录{n}',title='标准',clause=None,page=n,citation_number=n) for n in (1,3)]
    section=dict(body=raw,target_ids=ids,review_layout=style,public_output_state=state,verified_source_quotes=refs,
                 target_delivery=_public_target_delivery(raw,ids,state))
    prepare_public_section(section,dict.fromkeys(ids,'原件位置'),5,
                           source_fields=[dict(id=k,text=f'已授权原文{k}') for k in ids])
    return section


def test_repeated_commit_coalesces_and_opening_only_red_remains_uncommitted_per_target():
    first='### p-55\n**判断**：符合\n问题：期限有对应条款。[1](#cite-1)\n建议：保存实测时刻。\n**判断**：符合\n\n'
    second='### p-63\n**判断**：不符合\n原文参数与标准数值一致。[3](#cite-3) [2](#cite-2)\n'
    s=item_batch(first+second,['p-55','p-63'])
    a,b=s['review_items']
    assert s['body']==first+second and s['presented_body'].count('**判断**') == 2
    assert (a['verdict'],a['verdict_status'],a['source_numbers'])==('符合','submitted',[6])
    assert a['recommendation']=='保存实测时刻。' and a['finding']=='期限有对应条款。'
    assert (b['verdict'],b['verdict_status'],b['source_numbers'])==('无法判断','missing_final',[7])
    assert b['source_excerpt']=='已授权原文p-63' and b['finding']=='附件原文：已授权原文p-63'
    assert set(x['technical_status'] for x in (a,b)) == {'unverified'}
    assert '**判断**：不符合' not in s['presented_body']


def test_declared_red_is_a_model_judgement_and_not_a_verified_technical_violation():
    s=item_batch('## p-1\n原文列明 6 小时，标准写 8 小时。[3](#cite-3)\n**判断**：不符合',['p-1'])
    item=s['review_items'][0]
    assert item['verdict']=='不符合' and item['technical_status']=='unverified'
    assert '请复核适用依据和具体冲突' in s['presented_body'] and item['source_numbers']==[7]


def test_unknown_or_shared_table_row_is_not_promoted_or_attributed_to_one_target():
    raw='|位置|判断|\n|---|---|\n|p-3 与 p-4|符合 [1](#cite-1)|'
    s=item_batch(raw,['p-3','p-4'],style='table')
    for item in s['review_items']:
        assert item['verdict_status']=='custom' and item['verdict'] is None and not item['source_numbers']
    assert '**判断**' not in s['presented_body']
