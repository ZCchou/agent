"""Stable source text inventory including Word stories and unreadable objects."""
import hashlib
import io
import re
import xml.etree.ElementTree as ET
import zipfile

from agent.document_plan import document_structure, field_type


W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
A = '{http://schemas.openxmlformats.org/drawingml/2006/main}'
V = '{urn:schemas-microsoft-com:vml}'


def _paragraph_text(node):
    def walk(element):
        for child in element:
            if child.tag == W + 'p':
                continue
            if child.tag == W + 't':
                yield child.text or ''
            elif child.tag in {W + 'br', W + 'cr'}:
                yield '\n'
            elif child.tag == W + 'tab':
                yield '\t'
            else:
                yield from walk(child)
    return ''.join(walk(node))


def inventory(payload):
    from agent.documents import load_docx, targets
    from agent.document_parser.office import _xml
    document = load_docx(payload)
    fields, sections = document_structure(document, targets)
    represented = set()
    for _, target, kind in targets(document):
        if kind == 'paragraph':
            represented.add(target._p)
        else:
            represented.update(paragraph._p for paragraph in target.paragraphs)
    for index, paragraph in enumerate(document.element.iter(W + 'p')):
        if paragraph not in represented:
            text = _paragraph_text(paragraph)
            if text.strip():
                fields.append(_field('word/document.xml', f'p-{index}', text, field_type(text)))
    positions = {element: index for index, element in enumerate(document.element.body)}
    order = {f'p-{index}': positions[paragraph._p] for index, paragraph in enumerate(document.paragraphs)}
    order.update({f't-{index}': positions[table._tbl] for index, table in enumerate(document.tables)})
    fields.sort(key=lambda item: order.get(item['id'].split('-r', 1)[0], len(order)))
    for field in fields:
        field['writable'] = field['kind'] in {'paragraph', 'table_cell'}
        field['extraction_status'] = 'text_extracted'
        if field['field_type'] == 'container':
            field['field_type'] = field_type(field['text'])
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = sorted(name for name in archive.namelist() if name == 'word/document.xml'
                       or re.fullmatch(r'word/(?:header\d+|footer\d+|footnotes|endnotes)\.xml', name))
        for name in names:
            root = _xml(archive, name)
            paragraphs = [] if name == 'word/document.xml' else root.findall('.//' + W + 'p')
            for index, paragraph in enumerate(paragraphs):
                text = _paragraph_text(paragraph)
                if text.strip():
                    fields.append(_field(name, f'p-{index}', text, field_type(text)))
            objects = [node for node in root.iter() if node.tag in {
                W + 'drawing', W + 'pict', W + 'object',
                '{http://schemas.openxmlformats.org/officeDocument/2006/math}oMath'}]
            for index, _ in enumerate(objects):
                field = _field(name, f'object-{index}', '', 'unreadable')
                field['extraction_status'] = 'requires_ocr_or_visual_review'
                field['warning'] = '该图形或嵌入对象尚需 OCR 或人工查看。'
                fields.append(field)
    return {'sha256': hashlib.sha256(payload).hexdigest(), 'fields': fields, 'sections': sections}


def _field(part, key, text, category):
    return {'id': part + '#' + key, 'kind': 'story', 'text': text, 'original_content': text,
            'chapter_path': [part], 'section_id': part, 'section_ids': [part],
            'position': {'part': part, 'xml_position': key}, 'field_type': category,
            'status': 'pending', 'writable': False, 'extraction_status': 'text_extracted'}
