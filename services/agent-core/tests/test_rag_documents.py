from pathlib import Path
import zipfile

import pytest

from agent.rag.documents import parse_document, parse_text
from agent.storage import CoreDataError


def test_docx_preserves_paragraph_table_order_and_locators(tmp_path):
    path = tmp_path / 'regulation.docx'
    document = '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
    <w:p><w:r><w:t>25.1309 设备安全性</w:t></w:r></w:p>
    <w:tbl><w:tr><w:tc><w:p><w:r><w:t>严重失效</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>概率要求</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
    <w:p><w:r><w:t>下一段</w:t></w:r></w:p></w:body></w:document>'''
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('word/document.xml', document)
    parsed = parse_document(path)
    assert [part['text'] for part in parsed['chunks']] == ['25.1309 设备安全性', '严重失效 | 概率要求', '下一段']
    assert parsed['chunks'][0]['clause_number'] == '25.1309'
    assert parsed['chunks'][1]['locator']['table'] == 1
    assert parsed['chunks'][1]['locator']['row'] == 1


def test_docx_notes_and_nested_tables_warn(tmp_path):
    path = tmp_path / 'notes.docx'
    namespace = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('word/document.xml', f'<w:document xmlns:w="{namespace}"><w:body><w:p><w:r><w:t>正文</w:t></w:r></w:p></w:body></w:document>')
        archive.writestr('word/footnotes.xml', f'<w:footnotes xmlns:w="{namespace}"><w:footnote w:id="1"><w:p><w:r><w:t>脚注说明</w:t></w:r></w:p></w:footnote></w:footnotes>')
    parsed = parse_document(path)
    assert parsed['chunks'][-1]['text'] == '脚注说明'
    assert parsed['chunks'][-1]['locator']['part'] == 'footnote'
    assert parsed['warnings']


def test_text_chunk_boundaries_hashes_and_limits():
    parsed = parse_text('25.1309 安全性\n\n' + '要求' * 1200)
    assert all(len(chunk['text']) <= 1200 for chunk in parsed['chunks'])
    assert all(len(chunk['text_hash']) == 64 for chunk in parsed['chunks'])
    with pytest.raises(CoreDataError):
        parse_text('x' * 100, max_chars=50)
    with pytest.raises(CoreDataError):
        parse_text('  ')


def test_rejects_unknown_file_and_zip_expansion(tmp_path):
    with pytest.raises(CoreDataError):
        parse_document(tmp_path / 'bad.exe')
    path = tmp_path / 'large.docx'
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('word/document.xml', 'x' * 10000)
    with pytest.raises(CoreDataError):
        parse_document(path, max_expanded_bytes=100)


def make_pdf(path, text=None):
    stream = f'BT /F1 12 Tf 72 700 Td ({text}) Tj ET'.encode() if text else b''
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
               b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream']
    output = bytearray(b'%PDF-1.4\n')
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f'{index} 0 obj\n'.encode() + obj + b'\nendobj\n')
    xref = len(output)
    output.extend(f'xref\n0 {len(objects) + 1}\n0000000000 65535 f \n'.encode())
    for offset in offsets[1:]:
        output.extend(f'{offset:010d} 00000 n \n'.encode())
    output.extend(f'trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF'.encode())
    path.write_bytes(output)


def test_pdf_extracts_pages_with_uncertainty_warning_and_rejects_scans(tmp_path):
    path = tmp_path / 'text.pdf'
    make_pdf(path, '25.1309 System safety')
    parsed = parse_document(path)
    assert parsed['chunks'][0]['text'] == '25.1309 System safety'
    assert parsed['chunks'][0]['locator']['page'] == 1
    assert parsed['warnings']
    make_pdf(path)
    with pytest.raises(CoreDataError) as error:
        parse_document(path)
    assert error.value.code == 'rag_document_no_text'


def test_docx_content_controls_and_subclause_numbers_are_preserved(tmp_path):
    path = tmp_path / 'control.docx'
    namespace = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('word/document.xml', f'''<w:document xmlns:w="{namespace}"><w:body>
            <w:sdt><w:sdtContent><w:p><w:r><w:t>25.1309(a)(1)设备安全性</w:t></w:r></w:p></w:sdtContent></w:sdt>
            <w:p><w:r><w:t>后续要求</w:t></w:r></w:p></w:body></w:document>''')
    parsed = parse_document(path)
    assert [chunk['text'] for chunk in parsed['chunks']] == ['25.1309(a)(1)设备安全性', '后续要求']
    assert parsed['chunks'][0]['clause_number'] == '25.1309(a)(1)'


@pytest.mark.parametrize('suffix', ['.txt', '.md', '.markdown'])
def test_utf8_text_files_preserve_line_locators_and_reject_invalid_encoding(tmp_path, suffix):
    path = tmp_path / ('standard' + suffix)
    path.write_text('\ufeff25.1309 安全性\n第一段\n\n第二段', encoding='utf-8')
    parsed = parse_document(path)
    assert [chunk['text'] for chunk in parsed['chunks']] == ['25.1309 安全性\n第一段', '第二段']
    assert parsed['chunks'][0]['locator']['line_start'] == 1
    assert parsed['chunks'][0]['locator']['line_end'] == 2
    assert parsed['chunks'][1]['locator']['line_start'] == 4
    path.write_bytes(b'\xff\xfeinvalid utf8')
    with pytest.raises(CoreDataError) as error:
        parse_document(path)
    assert error.value.code == 'rag_document_encoding_invalid'


def test_pdfium_calls_are_serialized_across_import_threads(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import pypdfium2
    import threading
    import time

    active = 0
    largest_active = 0
    counter_lock = threading.Lock()
    class TextPage:
        def count_chars(self):
            return 20
        def get_text_range(self):
            return '25.1309 System safety'
        def close(self):
            pass
    class Page:
        def get_textpage(self):
            return TextPage()
        def close(self):
            pass
    class PdfDocument:
        def __init__(self, path):
            nonlocal active, largest_active
            with counter_lock:
                active += 1
                largest_active = max(largest_active, active)
            time.sleep(.02)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            nonlocal active
            with counter_lock:
                active -= 1
        def __len__(self):
            return 1
        def __getitem__(self, index):
            return Page()
    monkeypatch.setattr(pypdfium2, 'PdfDocument', PdfDocument)
    path = tmp_path / 'parallel.pdf'
    path.write_bytes(b'PDF content is supplied by the instrumented native boundary')
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(parse_document, [path] * 8))
    assert all(result['chunks'] for result in results)
    assert largest_active == 1


def test_long_clause_preserves_exception_sentence_and_parent_relationship():
    sentence = '设备应保持安全。'
    exception = '除非经局方批准，否则不得省略独立的验证试验。'
    text = '25.1309 ' + sentence * 147 + exception + sentence * 100
    parsed = parse_text(text)
    assert any(exception in chunk['text'] for chunk in parsed['chunks'])
    assert ''.join(chunk['text'] for chunk in parsed['chunks']) == text
    assert len({chunk['locator']['block_id'] for chunk in parsed['chunks']}) == 1
    assert all(chunk['locator']['parent_length'] == len(text) for chunk in parsed['chunks'])
    assert all(chunk['locator']['context_required'] for chunk in parsed['chunks'])
    assert all(chunk['locator']['splitting_version'] == 'source-block-v2' for chunk in parsed['chunks'])
    assert parsed['chunks'][0]['locator']['continues_after']
    assert parsed['chunks'][-1]['locator']['continues_before']
    assert parsed['warnings']


def test_docx_heading_paths_table_continuation_and_note_anchor(tmp_path):
    path = tmp_path / 'headings.docx'
    namespace = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('word/styles.xml', f'''<w:styles xmlns:w="{namespace}">
            <w:style w:type="paragraph" w:styleId="CustomSafety"><w:basedOn w:val="Heading1"/></w:style>
            </w:styles>''')
        archive.writestr('word/document.xml', f'''<w:document xmlns:w="{namespace}"><w:body>
            <w:p><w:pPr><w:pStyle w:val="CustomSafety"/></w:pPr><w:r><w:t>25.1309 系统安全性</w:t></w:r></w:p>
            <w:p><w:pPr><w:outlineLvl w:val="1"/></w:pPr><w:r><w:t>失效条件</w:t></w:r></w:p>
            <w:p><w:r><w:t>应保存试验记录。</w:t><w:footnoteReference w:id="3"/></w:r></w:p>
            <w:tbl><w:tr><w:tc><w:p><w:r><w:t>第一行</w:t></w:r></w:p></w:tc></w:tr>
                <w:tr><w:tc><w:p><w:r><w:t>第二行</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
            <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>25.1310 其他要求</w:t></w:r></w:p>
            <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>附录</w:t></w:r></w:p>
            <w:p><w:r><w:t>独立的附录内容。</w:t></w:r></w:p>
            </w:body></w:document>''')
        archive.writestr('word/footnotes.xml', f'''<w:footnotes xmlns:w="{namespace}"><w:footnote w:id="3">
            <w:p><w:r><w:t>除经批准的替代方法外。</w:t></w:r></w:p></w:footnote></w:footnotes>''')
    chunks = parse_document(path)['chunks']
    by_text = {chunk['text']: chunk for chunk in chunks}
    expected_path = ['25.1309 系统安全性', '失效条件']
    anchor = by_text['应保存试验记录。']['locator']
    assert anchor['heading_path'] == expected_path
    assert by_text['第一行']['locator']['heading_path'] == expected_path
    assert by_text['第二行']['locator']['previous_block_id'] == by_text['第一行']['locator']['block_id']
    note = by_text['除经批准的替代方法外。']
    assert note['locator']['reference_blocks'] == [anchor['block_id']]
    assert note['locator']['heading_path'] == expected_path
    assert note['clause_number'] == '25.1309'
    assert note['locator']['clause_parent_id'] == chunks[0]['locator']['block_id']
    assert by_text['独立的附录内容。']['clause_number'] is None
    assert by_text['独立的附录内容。']['locator']['heading_path'] == ['附录']
