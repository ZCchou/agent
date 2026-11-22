from agent.execution.selection_roles import explicit_example_disposition


def test_explicit_exclusion_and_literal_source_boundary():
    request = '审阅已填写正式内容，跳过空白、占位和示例。'
    source = {'id':'a','text':'示例：型号AB12的XXX设备。','chapter_path':['概述']}
    assert explicit_example_disposition(source,{},request)=='E:example'
    assert explicit_example_disposition(source,{},'不要跳过示例。') is None
    assert explicit_example_disposition(source,{},'审阅全部示例。') is None
    for text in ['环境温度应记录，例如湿热时也应记录相对湿度。','示例：试件甲。正式要求应记录温度。','示例：试件甲，但本项目要求记录温度。']:
        assert explicit_example_disposition({'text':text},{},request) is None
    cont = {'text':'型号AB12的XXX设备清单如下。','chapter_path':['概述'],
            'selection_context':{'example_context':{'relation':'candidate_italic_continuation','anchor_id':'a'}}}
    assert explicit_example_disposition(cont,{'a':source},request)=='E:example'
    cont['text']='适用性尚待确认'
    assert explicit_example_disposition(cont,{'a':source},request)=='U:ambiguous'
