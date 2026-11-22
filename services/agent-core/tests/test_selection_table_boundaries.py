from agent.execution.selection_roles import explicit_example_disposition, needs_role_review, source_role_observations


def field(key, text, chapter):
    return dict(id=key, text=text, chapter_path=[chapter],
                kind='table_cell' if key.startswith('t-') else 'paragraph')


def test_referenced_example_table_through_caption():
    items = [field('p-4', '示例：本节用到的仪器如表X所示。', '设备'),
             field('p-5', '表XX 仪器清单', '设备'),
             field('t-0-r1-c2', '设备型号A20', '设备')]
    facts = source_role_observations(items)
    sources = {item['id']:item for item in items}
    item = items[-1]
    assert explicit_example_disposition(item, sources, '跳过示例', facts[item['id']]) == 'E:example'
    assert explicit_example_disposition(item, sources, '不要跳过示例', facts[item['id']]) is None


def test_new_chapter_measurement_after_example_is_rechecked():
    items = [field('p-4', '示例：参见表X。', '要求'), field('p-5', '参数', '参数'),
             field('t-2-r2-c1', '减压', '参数'), field('t-2-r2-c2', '73kPa→12kPa', '参数')]
    fact = source_role_observations(items)[items[-1]['id']]
    assert 'explicit_example_table_reference_id' not in fact
    assert 'whole_example_anchor_ids' not in fact
    assert 't-2-r2-c1' in fact['nearby_original_anchor_ids']
    for code in ('C:label', 'C:example'):
        assert needs_role_review(items[-1], {'decision':'context', 'basis_code':code}, fact)


def test_explicit_number_mismatch_keeps_following_table():
    items = [field('p-4', '示例：见表2。', '设备'), field('p-5', '表3 正式仪器清单', '设备'),
             field('t-0-r1-c2', '设备型号A20', '设备')]
    fact=source_role_observations(items).get(items[-1]['id'], {})
    assert explicit_example_disposition(items[-1], {}, '跳过示例', fact) is None


def test_only_strict_format_guidance_is_context_in_formal_content_review():
    from agent.execution.selection_roles import editorial_disposition
    text='本节建议用表格的形式具体阐述试验大纲包含所有试验件的组成、构型状态、机上安装位置、设备研制保证等级、构型偏离情况说明等。'
    request='审阅已经填写的正式内容，跳过示例'
    assert editorial_disposition({'text':text}, request)=='C:guidance'
    for mixed in [text+'受试设备应持续工作。', text.replace('构型状态','实际构型状态'),
                  text.replace('机上安装位置','机上安装位置为A2'), text.replace('等级','等级必须确认')]:
        assert editorial_disposition({'text':mixed}, request) is None
    assert editorial_disposition({'text':text}, request+'，也审阅编写建议') is None
    assert editorial_disposition({'text':text}, '总结附件') is None


def test_concrete_applicability_is_distinct_from_drafting_and_caption():
    from agent.execution.selection_roles import editorial_disposition, filled_applicability
    actual='本节描述试验目的。过压试验考核增压舱设备。本节适用于安装在加压区域内的设备，以及其内部与外部加压区域相连通的设备。'
    assert filled_applicability(actual)
    for guidance in ['本节描述试验目的。建议填写：本节适用于安装在舱内的设备。',
                     '本节描述试验目的。示例：本节适用于安装在舱内的设备。',
                     '本节描述试验目的。本节适用于安装在XX区域的设备。']:
        assert not filled_applicability(guidance)
    request='审阅已经填写的正式内容，跳过空白、占位和示例。'
    assert editorial_disposition({'text':'图 X '},request)=='E:placeholder'
    assert editorial_disposition({'text':'图 X 试验件实际安装图'},request) is None
    assert editorial_disposition({'text':'本节描述试验要求。'},request)=='C:guidance'
    assert editorial_disposition({'text':actual},request) is None


def test_public_report_carries_chinese_language_and_edition_evidence_policy():
    from agent.document_review import review_request
    messages,_=review_request({'freeform_review':True,'report_language':'zh','fields':[]},{'evidence':[]})
    assert '均用中文' in messages[0]['content']
    assert 'cannot establish compliance with the requested edition' in messages[0]['content']
    assert all(rule in messages[0]['content'] for rule in ['表记形式','同一试验类型、阶段及动作','环境条件保持时长'])


def test_completed_test_objective_after_generic_drafting_sentence():
    from agent.execution.selection_roles import filled_test_purpose
    intro='本节描述试验目的。'
    altitude='高度试验是考核机载设备在民用飞机飞行过程中所经历的最高的高度环境，即最低的压力条件下，是否能够正常工作。'
    vibration='振动试验用于验证机载设备在预期的振动环境条件下能否执行预期的功能（包括耐久性要求），并且在振动条件下保持其结构完整性。'
    assert filled_test_purpose(intro+altitude)
    assert filled_test_purpose(intro+vibration)
    for example in [intro,intro+'示例：'+altitude,intro+'建议填写：'+vibration,
                    intro+'请说明试验验证设备在什么条件下是否能够正常工作。',
                    intro+altitude.replace('最低的压力','XX')]:
        assert not filled_test_purpose(example)
