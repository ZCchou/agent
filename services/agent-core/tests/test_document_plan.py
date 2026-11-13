import asyncio
import io

import pytest
from docx import Document
from docx.oxml import OxmlElement

from agent.storage import CoreDataError
from test_document_workflow import workflow


def source_document(service, run, count=2):
    document = Document()
    for chapter in range(count):
        document.add_heading(['温度试验', '振动试验'][chapter % 2], 1)
        document.add_heading('试验目的', 2)
        document.add_paragraph('说明：填写本章试验目的，删除示例。')
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = '型号'
    table.cell(0, 1).text = '待填写'
    table.cell(0, 1).add_table(rows=1, cols=1).cell(0, 0).text = '示例：A'
    stream = io.BytesIO()
    document.save(stream)
    return service.register_template(run['id'], stream.getvalue(), '模板.docx')


def test_structure_disambiguates_repeated_headings_and_nested_table_positions(tmp_path):
    _, run, service = workflow(tmp_path)
    source = source_document(service, run)
    structure = service.inspect_structure(run['id'], source['id'])
    fields = {field['id']: field for field in structure['fields']}
    assert fields['p-2']['chapter_path'] == ['温度试验', '试验目的']
    assert fields['p-5']['chapter_path'] == ['振动试验', '试验目的']
    assert fields['p-2']['section_id'] != fields['p-5']['section_id']
    assert fields['p-5']['field_type'] == 'instruction'
    assert fields['p-5']['original_content'] == '说明：填写本章试验目的，删除示例。'
    assert fields['t-0-r0-c1-t0-r0-c0']['position']['table_path'] == [0, 0]
    assert fields['t-0-r0-c1-t0-r0-c0']['field_type'] == 'example'


def test_planned_workflow_batches_chapters_resumes_and_persists_simulation(tmp_path):
    _, run, service = workflow(tmp_path)
    source = source_document(service, run)
    events = []
    fail_second = True

    async def generate(task):
        nonlocal fail_second
        if task['stage'] == 'plan':
            return {'targets': [{'target_id': 'p-2', 'field_type': 'instruction'},
                                {'target_id': 'p-5', 'field_type': 'instruction'}],
                    'equipment_parameters': {'型号': 'SIM-A', '质量': '2 kg'},
                    'gaps': ['设备实测资料未提供']}
        if task['fields'][0]['target_id'] == 'p-5' and fail_second:
            fail_second = False
            raise RuntimeError('provider interrupted')
        return {'fields': [{'target_id': task['fields'][0]['target_id'],
                            'text': task['equipment_parameters']['型号'] + ' 验证试验目的。', 'evidence': [],
                            'missing': ['试验示意图未提供']}]}

    async def exercise():
        kwargs = dict(scope='full', revision='r1', allow_simulated=True, progress=events.append)
        with pytest.raises(RuntimeError, match='interrupted'):
            await service.fill_planned(run['id'], source['id'], '整份填写，允许模拟设备数据', generate, **kwargs)
        plan = service.read_plan(run['id'], source['id'], '整份填写，允许模拟设备数据', revision='r1')
        assert [field['status'] for field in plan['targets']] == ['completed', 'pending']
        assert plan['equipment_parameters'] == {'型号': 'SIM-A', '质量': '2 kg'}
        result = await service.fill_planned(run['id'], source['id'], '整份填写，允许模拟设备数据', generate, **kwargs)
        document = Document(service.path_for(run['id'], result['id']))
        assert document.paragraphs[0].text == '温度试验'
        assert 'SIM-A' in document.paragraphs[2].text
        assert '模拟' in document.paragraphs[2].text
        assert 'SIM-A' in document.paragraphs[5].text
        assert result['manifest']['coverage']['completed'] == 2
        assert result['manifest']['review_status'] == 'pending'
        assert '试验示意图未提供' in result['manifest']['missing']
        assert len(result['manifest']['fields']) == 2
        assert result['status'] == 'draft'
        assert events[-1]['stage'] == 'completed'

    asyncio.run(exercise())


def test_section_scope_uses_full_identity_and_rejects_outside_targets(tmp_path):
    _, run, service = workflow(tmp_path)
    source = source_document(service, run)
    async def generate(task):
        return {'targets': [{'target_id': 'p-2', 'field_type': 'instruction'}]}
    async def exercise():
        with pytest.raises(CoreDataError) as raised:
            await service.fill_planned(run['id'], source['id'], '填写振动试验目的', generate,
                                       scope='section', section_id='p-3/p-4')
        assert raised.value.code == 'document_plan_invalid'
    asyncio.run(exercise())


def test_planned_fill_rejects_unplanned_outputs_and_unauthorized_simulation(tmp_path):
    _, run, service = workflow(tmp_path)
    source = source_document(service, run)
    async def generate(task):
        if task['stage'] == 'plan':
            return {'targets': [{'target_id': 'p-2', 'field_type': 'instruction'}],
                    'equipment_parameters': {'型号': 'SIM-A'}}
        return {'fields': [{'target_id': 'p-5', 'text': 'overwrite', 'evidence': []}]}
    async def exercise():
        with pytest.raises(CoreDataError) as raised:
            await service.fill_planned(run['id'], source['id'], '填写', generate)
        assert raised.value.code == 'document_simulation_forbidden'
        with pytest.raises(CoreDataError) as raised:
            await service.fill_planned(run['id'], source['id'], '填写', generate, revision='allowed', allow_simulated=True)
        assert raised.value.code == 'document_plan_invalid'
    asyncio.run(exercise())


def test_fill_retains_drawings_and_unauthorized_text_spans(tmp_path):
    _, run, service = workflow(tmp_path)
    document = Document()
    paragraph = document.add_paragraph('前缀：待填写；保留后缀')
    drawing = OxmlElement('w:drawing')
    paragraph.add_run()._r.append(drawing)
    table = document.add_table(rows=1, cols=1)
    table.cell(0, 0).text = '待填写'
    extra = table.cell(0, 0).add_paragraph('第二段')
    extra.add_run()._r.append(OxmlElement('w:drawing'))
    stream = io.BytesIO()
    document.save(stream)
    source = service.register_template(run['id'], stream.getvalue(), '图形.docx')
    result = service.fill(run['id'], source['id'], [
        {'target_id': 'p-0', 'text': '已完成', 'evidence': [], 'span': {'start': 3, 'end': 6}},
        {'target_id': 't-0-r0-c0', 'text': '替换正文', 'evidence': []},
    ])
    result_doc = Document(service.path_for(run['id'], result['id']))
    assert result_doc.paragraphs[0].text == '前缀：已完成；保留后缀'
    assert len(result_doc._element.xpath('.//w:drawing')) == 2
    assert len(result_doc.tables[0].cell(0, 0).paragraphs) == 2


def test_plan_batch_limit_and_context_budget_are_enforced(tmp_path):
    _, run, service = workflow(tmp_path)
    document = Document()
    document.add_heading('章', 1)
    for _ in range(23):
        document.add_paragraph('待填写')
    stream = io.BytesIO()
    document.save(stream)
    source = service.register_template(run['id'], stream.getvalue(), '长.docx')
    batch_sizes = []
    async def generate(task):
        if task['stage'] == 'plan':
            return {'targets': [{'target_id': field['id'], 'field_type': 'placeholder'}
                                for field in task['fields'] if field['field_type'] == 'placeholder']}
        batch_sizes.append(len(task['fields']))
        assert task['token_budget'] <= 6000
        return {'fields': [{'target_id': field['target_id'], 'text': '已填写', 'evidence': []} for field in task['fields']]}
    result = asyncio.run(service.fill_planned(run['id'], source['id'], '整份', generate, token_allowance=6000))
    assert max(batch_sizes) <= 10
    assert sum(batch_sizes) == 23
    assert result['manifest']['coverage']['completed'] == 23
    with pytest.raises(CoreDataError) as raised:
        asyncio.run(service.fill_planned(run['id'], source['id'], '整份', generate, revision='tiny', token_allowance=10))
    assert raised.value.code == 'document_context_too_small'


def test_evidence_resolver_rechecks_completed_fields_before_resuming(tmp_path):
    _, run, service = workflow(tmp_path)
    source = source_document(service, run)
    evidence_live = True
    async def generate(task):
        if task['stage'] == 'plan':
            return {'targets': [{'target_id': 'p-2', 'field_type': 'instruction'}]}
        return {'fields': [{'target_id': 'p-2', 'text': '依据条款 8.2 第3页',
                            'evidence': [{'id': 'e1', 'quote_id': 'q1'}]}]}
    def evidence(text, citations):
        if not evidence_live:
            raise CoreDataError('rag_citation_invalid', '证据已撤回')
        return text, [{'id': 'e1', 'quote_id': 'q1', 'physical_page': 3}]
    asyncio.run(service.fill_planned(run['id'], source['id'], '整份', generate, evidence_resolver=evidence))
    evidence_live = False
    with pytest.raises(CoreDataError) as raised:
        asyncio.run(service.fill_planned(run['id'], source['id'], '整份', generate, evidence_resolver=evidence))
    assert raised.value.code == 'rag_citation_invalid'


def test_filling_preserves_page_breaks_and_numbering_properties(tmp_path):
    from docx.enum.text import WD_BREAK
    _, run, service = workflow(tmp_path)
    document = Document()
    paragraph = document.add_paragraph(style='List Number')
    paragraph.add_run().add_break(WD_BREAK.PAGE)
    paragraph.add_run('待填写')
    stream = io.BytesIO()
    document.save(stream)
    source = service.register_template(run['id'], stream.getvalue(), '分页.docx')
    original_properties = paragraph._p.pPr.xml
    artifact = service.fill(run['id'], source['id'], [{'target_id': 'p-0', 'text': '完成', 'evidence': []}])
    filled = Document(service.path_for(run['id'], artifact['id'])).paragraphs[0]
    assert filled._p.pPr.xml == original_properties
    assert len(filled._p.xpath('.//w:br[@w:type="page"]')) == 1
    assert filled.text == '完成'


def test_checkpoint_authorization_identity_and_parameter_values_are_rechecked(tmp_path):
    _, run, service = workflow(tmp_path)
    source = source_document(service, run)
    async def generate(task):
        if task['stage'] == 'plan':
            return {'targets': [{'target_id': 'p-2', 'field_type': 'instruction'}],
                    'equipment_parameters': {'型号': 'SIM-A'}}
        return {'fields': [{'target_id': 'p-2', 'text': 'SIM-B', 'evidence': [],
                            'parameter_values': {'型号': 'SIM-B'}}]}
    with pytest.raises(CoreDataError) as raised:
        asyncio.run(service.fill_planned(run['id'], source['id'], '整份，允许模拟', generate, allow_simulated=True))
    assert raised.value.code == 'document_plan_invalid'
    with pytest.raises(CoreDataError) as raised:
        asyncio.run(service.fill_planned(run['id'], source['id'], '整份，允许模拟', generate))
    assert raised.value.code == 'document_plan_invalid'
    path = service.path_for(run['id'], source['id'])
    path.write_bytes(b'changed')
    with pytest.raises(CoreDataError) as raised:
        asyncio.run(service.fill_planned(run['id'], source['id'], '整份，允许模拟', generate, allow_simulated=True))
    assert raised.value.code == 'document_artifact_changed'


def test_concurrent_plan_calls_cannot_overwrite_completed_batches(tmp_path):
    _, run, service = workflow(tmp_path)
    source = source_document(service, run)
    entered = asyncio.Event()
    release = asyncio.Event()
    async def generate(task):
        if task['stage'] == 'plan':
            entered.set()
            await release.wait()
            return {'targets': [{'target_id': 'p-2', 'field_type': 'instruction'}]}
        return {'fields': [{'target_id': 'p-2', 'text': '完成', 'evidence': []}]}
    async def exercise():
        first = asyncio.create_task(service.fill_planned(run['id'], source['id'], '整份', generate))
        await entered.wait()
        try:
            with pytest.raises(CoreDataError) as raised:
                await service.fill_planned(run['id'], source['id'], '整份', generate)
            assert raised.value.code == 'document_plan_busy'
        finally:
            release.set()
            artifact = await first
        assert artifact['manifest']['coverage']['completed'] == 1
    asyncio.run(exercise())


def test_source_locators_need_current_citations_before_writing(tmp_path):
    _, run, service = workflow(tmp_path)
    source = source_document(service, run)
    async def generate(task):
        if task['stage'] == 'plan':
            return {'targets': [{'target_id': 'p-2', 'field_type': 'instruction'}]}
        return {'fields': [{'target_id': 'p-2', 'text': '依据条款8.2第3页', 'evidence': []}]}
    with pytest.raises(CoreDataError) as raised:
        asyncio.run(service.fill_planned(run['id'], source['id'], '整份', generate))
    assert raised.value.code == 'rag_citation_invalid'


def test_existing_inspection_exposes_chapter_identity_and_section_fill_avoids_legacy_resolver(tmp_path, monkeypatch):
    _, run, service = workflow(tmp_path)
    source = source_document(service, run)
    inspected = service.inspect(run['id'], source['id'])
    purpose = next(field for field in inspected['fields'] if field['id'] == 'p-5')
    assert purpose['chapter_path'] == ['振动试验', '试验目的']
    def legacy(*args):
        raise AssertionError('legacy ambiguity resolver used')
    monkeypatch.setattr(service, 'select_section', legacy)
    async def generate(task):
        if task['stage'] == 'plan':
            return {'targets': [{'target_id': 'p-5', 'field_type': 'instruction'}]}
        return {'fields': [{'target_id': 'p-5', 'text': '振动目标', 'evidence': []}]}
    artifact = asyncio.run(service.fill_planned(run['id'], source['id'], '填写振动目的', generate,
                                               scope='section', section_id='p-3/p-4'))
    document = Document(service.path_for(run['id'], artifact['id']))
    assert document.paragraphs[2].text == '说明：填写本章试验目的，删除示例。'
    assert document.paragraphs[5].text == '振动目标'
