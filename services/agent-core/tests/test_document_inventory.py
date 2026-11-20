import io
import zipfile

from docx import Document
from docx.oxml import OxmlElement


def test_inventory_includes_header_footer_nested_cells_and_textbox():
    from agent.document_inventory import inventory
    document = Document()
    document.add_paragraph('正文')
    document.sections[0].header.paragraphs[0].text = '页眉中的设备型号'
    document.sections[0].footer.paragraphs[0].text = '页脚中的受控版本'
    cell = document.add_table(rows=1, cols=1).cell(0, 0)
    cell.text = '表格'
    cell.add_table(rows=1, cols=1).cell(0, 0).text = '嵌套表格'
    box = OxmlElement('w:txbxContent')
    paragraph = OxmlElement('w:p')
    run = OxmlElement('w:r')
    text = OxmlElement('w:t')
    text.text = '文本框里的试验条件'
    run.append(text)
    paragraph.append(run)
    box.append(paragraph)
    document.paragraphs[0]._p.append(box)
    payload = io.BytesIO()
    document.save(payload)
    result = inventory(payload.getvalue())
    texts = [item['text'].strip() for item in result['fields']]
    for text in ['正文', '页眉中的设备型号', '页脚中的受控版本', '表格', '嵌套表格', '文本框里的试验条件']:
        assert texts.count(text) == 1
    assert len({item['id'] for item in result['fields']}) == len(result['fields'])
    assert result == inventory(payload.getvalue())


def test_inventory_records_unreadable_drawing_as_a_coverage_gap():
    from agent.document_inventory import inventory
    document = Document()
    drawing = OxmlElement('w:drawing')
    blip = OxmlElement('a:blip')
    drawing.append(blip)
    document.add_paragraph('带图段落')._p.append(drawing)
    payload = io.BytesIO()
    document.save(payload)
    result = inventory(payload.getvalue())
    unreadable = [item for item in result['fields'] if item['field_type'] == 'unreadable']
    assert len(unreadable) == 1
    assert unreadable[0]['extraction_status'] == 'requires_ocr_or_visual_review'
    assert unreadable[0]['writable'] is False


def test_inventory_includes_word_content_control_paragraphs():
    from agent.document_inventory import inventory
    document = Document()
    control = OxmlElement('w:sdt')
    content = OxmlElement('w:sdtContent')
    paragraph = OxmlElement('w:p')
    run = OxmlElement('w:r')
    text = OxmlElement('w:t')
    text.text = '内容控件内的设备要求'
    run.append(text)
    paragraph.append(run)
    content.append(paragraph)
    control.append(content)
    document.element.body.insert(0, control)
    document.add_paragraph('后续普通正文')
    payload = io.BytesIO()
    document.save(payload)
    result = inventory(payload.getvalue())
    assert [item['text'] for item in result['fields']] == ['内容控件内的设备要求', '后续普通正文']


def test_explicit_review_reads_all_selected_originals_despite_short_preview(tmp_path, monkeypatch):
    from hashlib import sha256
    from types import SimpleNamespace
    from agent.runtime import ChatRunService
    from agent.tool_contracts import ToolCall
    from agent.execution import source_inventory

    document = Document()
    for text in ('first actual text', 'unrequested text', 'second actual text'):
        document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    payload = buffer.getvalue()
    original = tmp_path / 'original.docx'
    original.write_bytes(payload)
    recorded = []
    class Inventory:
        def __init__(self, repository):
            pass
        def register(self, *args, **kwargs):
            pass
        def record_read(self, task, source, digest, fields, **kwargs):
            recorded.extend(fields)
    monkeypatch.setattr(source_inventory, 'SourceInventory', Inventory)
    artifact = {'sha256': sha256(payload).hexdigest()}
    service = SimpleNamespace(repository=object(),
        rag_grounding=SimpleNamespace(validate=lambda run: None),
        documents=SimpleNamespace(
            inspect=lambda *args, **kwargs: {'fields': [{'id': 'p-0', 'text': 'first'}]},
            get=lambda *args: artifact, path_for=lambda *args: original),
        execution_monitor=SimpleNamespace(start_task=lambda **kwargs: {'id': 'task', 'revision': 1, 'attempt': 1}))
    call = ToolCall('inspect', 'document.inspect_template', {'template_id': 'source', 'limit': 90})
    ChatRunService._document_tool(service, 'run', call, max_characters=100,
                                 selected_ids=frozenset({'p-0', 'p-2'}))
    assert [(field['id'], field['text']) for field in recorded] == [
        ('p-0', 'first actual text'), ('p-2', 'second actual text')]
