from agent.document_review import _public_target_delivery
from agent.review_presentation import prepare_public_section


def section(raw, targets, state='present', refs=()):
    return {'body': raw, 'target_ids': targets, 'target_delivery': _public_target_delivery(raw,targets,state),
            'public_output_state': state,'verified_source_quotes':list(refs)}


def test_only_target_with_explicit_reversal_is_condensed_and_original_is_still_addressable():
    raw = ('p-55\n\n**判断**：符合\n\np-55 保留此项正文。\n\np-63\n\n**判断**：不符合\n\n'
           'p-63 说明 [2](#cite-2)。\n因此，p-63 的判断应该是 **符合**。\n\np-63 后续文字被截')
    quote = {'id':'rag-x', 'quote_id':'q-x', 'citation_number':2, 'title':'标准',
             'page':18, 'clause':'6.3.1', 'quote':'原文摘录'}
    saved = section(raw,['p-55','p-63'],'truncated',[quote])
    prepare_public_section(saved,{'p-55':'温度变化','p-63':'湿热'},0)
    rendered = saved['presented_body']
    assert saved['body'] == raw and saved['verdict_conflicts'] == ['p-63']
    assert '**判断**：符合' in rendered and '保留此项正文' in rendered
    assert '**判断**：无法判断' in rendered and '相互冲突' in rendered and '判断应该是' not in rendered
    assert '[1](#审阅出处-1)' in rendered and '> 原文摘录' in rendered


def test_conditional_quoted_comparisons_and_other_targets_do_not_change_verdict():
    raw = ('## p-1\n**判断**：不符合\n如果满足要求，判断应该是符合。\n'
           '因此，p-9 的判断应该是符合。\n判断应该是符合吗？\n'
           '> 判断：符合\n```\n判断：符合\n```\n'
           '## p-2\n**判断**：符合\n原位文字。')
    saved = section(raw,['p-1','p-2'])
    prepare_public_section(saved,{},0)
    assert saved['verdict_conflicts'] == [] and saved['presented_body'] == raw


def test_only_mapped_explicit_blocks_may_be_condensed_and_partial_unknown_output_is_preserved():
    raw = '## p-1\n**判断**：不符合\n**结论**：符合\n这一句话尚未写完'
    for ids,state in [(['p-1','p-2'],'present'),(['p-1'],'truncated')]:
        saved = section(raw,ids,state)
        prepare_public_section(saved,{},0)
        assert saved['verdict_conflicts'] == [] and saved['presented_body'] == raw
    saved = section(raw,['p-1'])
    prepare_public_section(saved,{},0)
    assert saved['verdict_conflicts'] == ['p-1']


def test_explicit_inline_revision_in_natural_final_batch_with_bare_target_headings():
    raw = ('p-55\n\n**判断**：符合\n正文完整。\n\np-63\n\n**判断**：不符合\n'
           '参数是否一致？原文记录了数值。无直接矛盾。修正判断：**判断**：符合\n\n理由：请核实适用条件。')
    saved = section(raw,['p-55','p-63'])
    prepare_public_section(saved,{},0)
    assert saved['verdict_conflicts'] == ['p-63']
    assert '正文完整。' in saved['presented_body'] and '修正判断' not in saved['presented_body']
    assert saved['body'] == raw
    for phrase in ('引文“结论。修正判断：判断：符合”。', '是否修正判断：判断：符合？',
                   '如果参数正确，修正判断：判断：符合。', '普通内容。`修正判断：判断：符合`'):
        sample = section('p-1\n**判断**：不符合\n' + phrase, ['p-1'])
        prepare_public_section(sample,{},0)
        assert sample['verdict_conflicts'] == []
