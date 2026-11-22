import pytest

from agent.document_review import _public_target_delivery
from agent.review_presentation import prepare_public_section


def assess(original, clause, *, compared='', verdict='不符合', scope='', reference=True,
           requirement=None, contract=True, finding=None):
    if finding is None:
        finding = f'本项规定“{original}”，与所引要求“{clause}”冲突。'
    raw = ('### p-1\n**原文摘录**：' + original + '\n**依据摘录**：'
           + (clause if requirement is None else requirement)
           + (' [1](#cite-1)' if reference else '') + '\n**问题**：' + finding + '\n')
    if compared:
        raw += '**对比量值**：' + compared + '\n'
    if scope:
        raw += '**已查范围**：' + scope + '\n'
    raw += '**判断**：' + verdict
    section = dict(body=raw, target_ids=['p-1'], public_output_state='present', review_layout='default',
                   verified_source_quotes=[dict(id='ref', quote_id='q-1', citation_number=1,
                       title='采购规范', clause='1.1', page=1, quote=clause)],
                   checked_scope_ids={'p-1':['p-1','p-2']},
                   target_delivery=_public_target_delivery(raw, ['p-1'], 'present'))
    if contract:
        section['review_contract_version'] = 1
    prepare_public_section(section, {'p-1':'目标位置'}, 0, source_fields=[dict(id='p-1', text=original)])
    return section['review_items'][0], section['presented_body']


@pytest.mark.parametrize('source,requirement', [
    ('60 min内', '1 h内'), ('5～6 kPa', '至少5000 Pa'), ('大于5 V', '至少5 V'),
    ('5～6 ℃/min', '最小5 ℃/min'), ('1.0 kHz', '1000 Hz'),
])
def test_equal_or_contained_quantity_cannot_substantiate_adverse_judgment(source, requirement):
    item, body = assess('复测窗口为' + source, '复测窗口为' + requirement,
                        compared=source + ' | ' + requirement)
    assert item['verdict'] == '无法判断' and item['technical_status'] == 'needs_evidence'
    assert '**判断**：不符合' not in body and item['finding'] in body


@pytest.mark.parametrize('source,requirement', [
    ('90 min内', '1 h内'), ('4～6 kPa', '至少5000 Pa'), ('至少5 V', '大于5 V'),
])
def test_real_difference_is_kept(source, requirement):
    item, _ = assess('原件：' + source, '条款：' + requirement, compared=source + ' | ' + requirement)
    assert item['verdict'] == '不符合' and item['technical_status'] == 'evidence_linked'


def test_adverse_verdict_needs_item_evidence_and_observed_scope():
    for options in [dict(reference=False), dict(requirement='另一份条款'), dict(verdict='待补充'),
                    dict(verdict='待补充', scope='p-1、p-99')]:
        item, _ = assess('本项列出了入口', '文件必须说明入口和出口', **options)
        assert item['verdict'] == '无法判断'
    item, _ = assess('本项列出了入口', '文件必须说明入口和出口', verdict='待补充', scope='p-1、p-2')
    assert item['verdict'] == '待补充'
    assert item['checked_scope_ids'] == ['p-1','p-2']


def test_legacy_review_still_displays_historical_verdict():
    item, _ = assess('文本', '依据', contract=False, reference=False)
    assert item['verdict'] == '不符合' and item['technical_status'] == 'unverified'


def test_equivalent_normative_wording_is_not_a_conflict_but_opposite_polarity_is():
    item, _ = assess('禁止接通主电源。', '不得接通主电源。')
    assert item['verdict'] == '无法判断'
    item, _ = assess('必须接通主电源。', '不得接通主电源。')
    assert item['verdict'] == '不符合'


def test_an_omission_claim_also_requires_scope_when_marked_nonconforming():
    item, _ = assess('测试前检查外观。', '每套程序须记录批号。', finding='整份文件未记录批号。')
    assert item['verdict'] == '无法判断'


def test_equivalent_windows_with_different_anchors_can_have_a_real_stage_defect():
    item, _ = assess('关机后60 min内提交', '必须在开机后1 h内提交', finding='起算事件不同，应以开机作为起算事件。')
    assert item['verdict'] == '不符合'


def test_citation_cannot_drop_a_leading_digit_or_minus_sign():
    for requirement in ('5 kPa', '15 kPa'):
        item, _ = assess('压力20 kPa', '压力必须为-15 kPa', requirement=requirement)
        assert item['verdict'] == '无法判断'


def test_missing_operands_can_be_recovered_for_unambiguous_numeric_mismatch():
    item, _ = assess('结束后60 min内供电', '结束后1 h内供电', finding='两者的时间限值不一致。')
    assert item['verdict'] == '无法判断'
    item, _ = assess('结束后90 min内供电', '结束后1 h内供电', finding='两者的时间限值不一致。')
    assert item['verdict'] == '不符合'
    item, _ = assess('峰值20 V，平均10 V', '峰值15 V，平均8 V', finding='电压数值与要求不符。')
    assert item['verdict'] == '无法判断'


def test_tentative_conflict_is_not_a_confirmed_violation_but_risk_consequence_is_allowed():
    item, _ = assess('传感器在测试后接入', '测试前接入传感器', finding='连接顺序存在潜在不符。')
    assert item['verdict'] == '无法判断'
    item, _ = assess('传感器在测试后接入', '测试前接入传感器', finding='连接顺序与规定相反，可能造成数据丢失。')
    assert item['verdict'] == '不符合'


def test_bare_conclusion_with_a_citation_does_not_establish_any_specific_conflict():
    item, _ = assess('附录记载测试人员姓名', '设备应在通电前完成检查', finding='要求未得到满足。')
    assert item['verdict'] == '无法判断'


def test_preventive_action_cannot_be_attributed_as_an_affirmative_requirement():
    item, _ = assess('机柜应防误触开关，保持清洁。', '机柜应采取措施避免意外操作。', finding='原文要求误触开关，而依据要求避免意外操作。')
    assert item['verdict'] == '无法判断' and '相反' in item['finding']
    item, _ = assess('机柜应允许误触开关。', '机柜应采取措施避免意外操作。', finding='原文允许误触开关，与预防意外操作的条款相反。')
    assert item['verdict'] == '不符合'


def test_default_positive_verdict_requires_at_least_one_item_scoped_citation():
    item, body = assess('送检前检查外观。', '送检前检查外观。',verdict='符合',reference=False)
    assert item['verdict'] == '无法判断' and item['finding'] in body
    item, _ = assess('送检前检查外观。', '送检前检查外观。',verdict='符合',reference=True)
    assert item['verdict'] == '符合' and item['technical_status'] == 'unverified'


def test_explicit_numeric_statement_comparing_time_origins_does_not_imply_a_value_only_finding():
    item, _ = assess('开机后60 min内供电', '关机后1 h内供电', finding='时间限值不一致，原因是两者的起算点不同。')
    assert item['verdict'] == '不符合'


def test_uncertain_boundary_in_prose_does_not_get_parsed_as_exact_number():
    item, _ = assess('压力约20 kPa', '压力要求至少25 kPa', finding='压力数值不符。')
    assert item['verdict'] == '无法判断'
    item, _ = assess('压力不得高于20 kPa', '压力要求高于25 kPa', finding='压力数值不符。')
    assert item['verdict'] == '无法判断'


def test_percent_limits_and_unconverted_identical_count_units_preserve_real_defects():
    item, _ = assess('湿度75%', '湿度至少85%', finding='低于下限。')
    assert item['verdict'] == '不符合'


@pytest.mark.parametrize('first,expected_statuses', [
    ('120 min', ['outside','outside']), ('60 min', ['within','outside']),
    ('120 min (1 h)', ['unverified','outside']),
])
def test_multiline_labels_and_individual_pairs_survive_real_markdown_variants(first, expected_statuses):
    original = '结束后' + first.split(' (')[0] + '内启动；热机最多30 min。'
    clause = '结束后1h\r\n内启动；热机最多允许15min。'
    code = 'p-3'
    raw = (f'### {code}\n**原文摘录**\n{original}\n\n**依据摘录**\n结束后1h内启动；热机最多允许15min。 [1](#cite-1)\n\n'
           f'**对比量值**\n启动时限：{first} | 1 h (60 min)\n热机时长：30 min | 15 min\n\n'
           '**问题**：启动和热机均超出要求。\n**建议**：必须修改两项。\n**判断**：不符合')
    section = dict(body=raw, target_ids=[code], review_layout='default', review_contract_version=1,
                   public_output_state='present', checked_scope_ids={code:[code]},
                   verified_source_quotes=[dict(id='r',quote_id='q-1',citation_number=1,title='依据',clause='',page=1,quote=clause)],
                   target_delivery=_public_target_delivery(raw,[code],'present'))
    prepare_public_section(section,{code:'步骤'},0,source_fields=[dict(id=code,text=original)])
    row = section['review_items'][0]
    assert row['verdict'] == '不符合'
    assert [entry['relation'] for entry in row['quantity_comparisons']] == expected_statuses
    if expected_statuses[0] != 'outside':
        assert '均超出' not in row['finding'] and '均超出' not in section['presented_body']
        assert '必须修改两项' not in section['presented_body']
    item, _ = assess('每次取1份', '每次取至少2份', compared='1份 | 至少2份')
    assert item['verdict'] == '不符合'
