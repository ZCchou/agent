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


def _symbol_text(node):
    """Resolve only a known font glyph; never pretend private-use codes are Unicode."""
    import json
    font=node.get(W+'font','')
    code=node.get(W+'char','')
    if font.strip().casefold()=='wingdings' and code.upper() in {'F0E0','00E0','E0'}:
        return '→'
    return '⟦符号 font='+json.dumps(font,ensure_ascii=False)+' char='+json.dumps(code,ensure_ascii=False)+'⟧'


def _paragraph_text(node):
    def walk(element):
        for child in element:
            if child.tag == W + 'p':
                continue
            if child.tag == W + 't':
                yield child.text or ''
            elif child.tag == W+'sym':
                yield _symbol_text(child)
            elif child.tag in {W + 'br', W + 'cr'}:
                yield '\n'
            elif child.tag == W + 'tab':
                yield '\t'
            else:
                yield from walk(child)
    return ''.join(walk(node))


def nonempty_position(field):
    text = field.get('original_content', field.get('text', ''))
    return bool(text.strip()) or field.get('extraction_status') == 'unreadable' or field.get('field_type') == 'unreadable'


def _verified_table_grids(document):
    """Map original XML anchors to verified half-open grid rectangles.

    No semantic ownership is inferred. An inconsistent table contributes no
    geometry; existing inventory identities/coordinates remain untouched.
    """
    result = {}
    for table in document.element.body.iter(W + 'tbl'):
        grid = table.find(W + 'tblGrid')
        width = len(grid.findall(W + 'gridCol')) if grid is not None else 0
        anchors, active = {}, {}
        try:
            if not width:
                continue
            for row_index, row in enumerate(table.findall(W + 'tr')):
                properties = row.find(W + 'trPr')
                def omitted(name):
                    node = properties.find(W + name) if properties is not None else None
                    return int(node.get(W + 'val')) if node is not None else 0
                column, after = omitted('gridBefore'), omitted('gridAfter')
                if column < 0 or after < 0:
                    raise ValueError('Negative omitted grid')
                continued = {}
                for cell in row.findall(W + 'tc'):
                    properties_cell = cell.find(W + 'tcPr')
                    if properties_cell is not None and properties_cell.find(W + 'hMerge') is not None:
                        raise ValueError('Legacy horizontal merge is not verified')
                    span = properties_cell.find(W + 'gridSpan') if properties_cell is not None else None
                    span = int(span.get(W + 'val')) if span is not None else 1
                    merge = properties_cell.find(W + 'vMerge') if properties_cell is not None else None
                    interval = (column, column + span)
                    if span < 1 or interval[1] > width:
                        raise ValueError('Invalid cell grid span')
                    if merge is not None and merge.get(W + 'val', 'continue') == 'continue':
                        if interval not in active:
                            raise ValueError('Unmatched vertical continuation')
                        anchor = active[interval]
                        anchors[anchor]['row_end'] = row_index + 1
                        anchors[anchor]['row_span'] += 1
                        continued[interval] = anchor
                    else:
                        if merge is not None and merge.get(W + 'val') != 'restart':
                            raise ValueError('Unknown vertical merge')
                        anchors[cell] = {'row_start': row_index, 'row_end': row_index + 1,
                            'column_start': column, 'column_end': column + span,
                            'grid_span': span, 'row_span': 1}
                        if merge is not None:
                            continued[interval] = cell
                    column += span
                if column + after != width:
                    raise ValueError('Row does not match table grid')
                active = continued
            result.update(anchors)
        except (ValueError, TypeError):
            continue
    return result


def inventory(payload, *, include_selection_context=False):
    from agent.documents import load_docx, targets
    from agent.document_parser.office import _xml
    document = load_docx(payload)
    fields, sections = document_structure(document, targets)
    grid_by_cell = _verified_table_grids(document)
    grids = {}
    body = document.element.body
    positions = {element: index for index, element in enumerate(body.iter())}

    represented = set()
    order = {}
    source_targets = {}
    symbol_texts = {}
    for identity, target, kind in targets(document):
        paragraphs=[target._p] if kind=='paragraph' else [paragraph._p for paragraph in target.paragraphs]
        if any(paragraph.find('.//'+W+'sym') is not None for paragraph in paragraphs):
            symbol_texts[identity]='\n'.join(_paragraph_text(paragraph) for paragraph in paragraphs)
        if include_selection_context:
            source_targets[identity] = (target, kind)
        if kind.startswith('table_') and target._tc in grid_by_cell:
            grids[identity] = grid_by_cell[target._tc]
        if kind == 'paragraph':
            represented.add(target._p)
            element = target._p
        else:
            represented.update(paragraph._p for paragraph in target.paragraphs)
            element = target._tc
        order[identity] = positions[element]
    for index, paragraph in enumerate(body.iter(W + 'p')):
        if paragraph not in represented:
            text = _paragraph_text(paragraph)
            if text.strip():
                field = _field('word/document.xml', f'p-{index}', text, field_type(text))
                fields.append(field)
                order[field['id']] = positions[paragraph]
    fields.sort(key=lambda item: order.get(item['id'], len(positions)))
    for field in fields:
        if field['id'] in symbol_texts:
            field['text']=field['original_content']=symbol_texts[field['id']]
        if field['id'] in grids:
            field['table_grid'] = grids[field['id']]
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
    if include_selection_context:
        from agent.document_selection_context import selection_annotations
        annotations = selection_annotations(document, fields, source_targets)
        for field in fields:
            if field['id'] in annotations:
                field['selection_context'] = annotations[field['id']]
    return {'sha256': hashlib.sha256(payload).hexdigest(), 'fields': fields, 'sections': sections}


class ReviewSource:
    """One authorized review's parsed source and lazily materialized table contexts."""
    def __init__(self, workflow, run_id, artifact_id):
        self.owner = (run_id, artifact_id)
        parsed = inventory(workflow.path_for(run_id, artifact_id).read_bytes(), include_selection_context=True)
        self.sha256 = parsed['sha256']
        self.fields = parsed['fields']
        self._contexts = {}

    def table_contexts(self, ids):
        from agent.review_table_context import table_contexts
        missing = [key for key in ids if key not in self._contexts]
        if missing:
            fresh = table_contexts(self.fields, missing)
            self._contexts.update((key, fresh.get(key)) for key in missing)
        return {key: self._contexts[key] for key in ids if self._contexts[key] is not None}


def _field(part, key, text, category):
    return {'id': part + '#' + key, 'kind': 'story', 'text': text, 'original_content': text,
            'chapter_path': [part], 'section_id': part, 'section_ids': [part],
            'position': {'part': part, 'xml_position': key}, 'field_type': category,
            'status': 'pending', 'writable': False, 'extraction_status': 'text_extracted'}
