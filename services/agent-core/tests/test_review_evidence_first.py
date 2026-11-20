import pytest

from agent.document_review import _public_review_layout, _public_target_delivery, review_request
from agent.review_presentation import prepare_public_section


def project(raw, style='default', state='present'):
    ids = ['p-3']
    saved = {'body':raw,'target_ids':ids,'public_output_state':state,'review_layout':style,
             'target_delivery':_public_target_delivery(raw,ids,state)}
    prepare_public_section(saved,{'p-3':'采购合同'},0)
    assert saved['body'] == raw
    return saved


@pytest.mark.parametrize(('prompt_text','style'),[
    ('结合资料库审阅附件，保存报告。','default'), ('Review the supplier agreement.','default'),
    ('按「审查项｜位置｜判断」输出','table'), ('Use a Markdown table.','table'),
    ('请先列问题，再列建议。','custom'), ('把判断放在最后。','custom'),
    ('Use bullet points, in this order.','custom')])
def test_one_generation_writer_contract_respects_requested_layout(prompt_text,style):
    assert _public_review_layout(prompt_text) == style
    prompt = review_request({'request':prompt_text,'freeform_review':True,'fields':[]},{'evidence':[]})[0][0]['content']
    assert ('End this item with exactly one final verdict' in prompt) == (style == 'default')
    assert ('For nonconformity name the concrete contradiction' in prompt) == (style == 'default')


@pytest.mark.parametrize(('body','verdict'),[
    ('合同约定付款期限为 45 天，采购规则的上限为 30 天。\n建议确认适用的付款条款。','**判断**：不符合'),
    ('保险条款有免赔额，本页未列明限额。','**判断**：无法判断'),
    ('The retention period is seven days while the policy requires ten.','**Verdict**: Does not conform'),
    ('The measurement matches the specified tolerance.','**Verdict**: Conforms'),
    ('The drawing identifies a rating but gives no value.','**Verdict**: Needs information'),
    ('The published excerpt does not address this warranty.','**Verdict**: Undetermined')])
def test_moves_only_a_single_supported_final_line_in_complete_default_prose(body,verdict):
    raw = '## p-3\n\n'+body+'\n\n'+verdict
    result = project(raw)
    assert result['presented_body'].index(verdict) < result['presented_body'].index(body)
    assert result['verdict_conflicts'] == []
    for style,state in [('custom','present'), ('table','present'), ('default','truncated')]:
        text = project(raw,style,state)['presented_body']
        assert text.index(body) < text.index(verdict)


def test_missing_unknown_inline_and_quoted_verdicts_do_not_trigger_reordering_or_guessing():
    for text in ('原文有两个数值。','原文有两个数值。\n**判断**：部分满足',
                 '原文有两个数值。\n> **判断**：符合', '原文有两个数值。\n```\n**判断**：符合',
                 '原文有两个数值。\n**判断**：符合\n**判断**：符合'):
        raw = '## p-3\n\n'+text
        shown=project(raw)['presented_body']
        assert shown.endswith(text)
    english=project('## p-3\n**Verdict**: Does not conform\nRevised verdict: Conforms.')
    assert english['verdict_conflicts']==['p-3'] and '**Verdict**: Undetermined' in english['presented_body']
