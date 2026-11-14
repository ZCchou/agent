"""Bounded local document extraction with source-positioned evidence chunks."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import threading
import time
import xml.etree.ElementTree as ET
import zipfile

from agent.storage import CoreDataError
from agent.document_parser.office import OFFICE_SUFFIXES

MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_EXPANDED_BYTES = 200 * 1024 * 1024
MAX_TEXT_CHARS = 20_000_000
MAX_CHUNKS = 100_000
CHUNK_CHARS = 1200
SPLITTING_VERSION = 'source-block-v4'
PDF_PARSE_TIMEOUT = 120
SUPPORTED_SUFFIXES = OFFICE_SUFFIXES | {'.pdf', '.txt', '.md', '.markdown'}
_PDF_LOCK = threading.Lock()
NS = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
CLAUSE = re.compile(r'^\s*(?:§\s*)?((?:[A-Z]+[- ]?)?\d+(?:\.\d+)+(?:\([a-zA-Z0-9]+\))*)(?=$|[^0-9A-Za-z.])')
SECTION = re.compile(r'^\s*(?:section[ \t]+\d+\b|第[一二三四五六七八九十百零〇\d]+[章节])', re.IGNORECASE | re.MULTILINE)


def has_readable_text(text):
    return any(character.isalnum() for character in text)


def _split_block(text):
    start = 0
    while start < len(text):
        end = min(start + CHUNK_CHARS, len(text))
        boundary = 'end'
        if end < len(text):
            candidates = [start + match.end() for match in re.finditer(r'[。！？!?；;]|\.(?=\s|$)|\n', text[start:end + 1])
                          if start + match.end() <= end]
            boundary = 'sentence'
            if candidates:
                end = candidates[-1]
            else:
                spaces = list(re.finditer(r'\s+', text[start:end]))
                end = start + spaces[-1].end() if spaces else end
                boundary = 'forced'
        yield start, end, boundary
        start = end


def _pdf_sections(blocks):
    """Split page text at actual clause headings, preserving physical-page offsets."""
    for text, locator in blocks:
        if locator.get('kind') != 'pdf':
            yield text, locator
            continue
        lines = text.splitlines(keepends=True)
        page_section = next((match.group(1).upper() for line in lines[:3]
                             if (match := re.fullmatch(r'\s*([A-Z]|\d+)[－–-]\d+\s*', line, re.IGNORECASE))), None)
        headings = {}
        offset = 0
        for index, line in enumerate(lines):
            match = CLAUSE.match(line)
            if match:
                title = line[match.end():].strip()
                if not title and index + 1 < len(lines):
                    title = lines[index + 1].strip()
                major = match.group(1).split('.')[0]
                if (page_section is None or major == page_section) and len(title) <= 160 \
                        and re.match(r'(?:[A-Za-z]{3}|[\u3400-\u9fff]{2})', title) \
                        and not re.search(r'…{2,}|\.{5,}|·{5,}', title):
                    headings[offset] = line.strip()
            offset += len(line)
        boundaries = sorted({0, len(text), *headings})
        for start, end in zip(boundaries, boundaries[1:]):
            part_locator = {**locator, '_pdf_clause': start in headings, '_pdf_section': page_section,
                            'page_char_start': locator.get('page_char_start', 0) + start,
                            'page_char_end': locator.get('page_char_start', 0) + end}
            if start in headings:
                part_locator['heading_path'] = [headings[start]]
            yield text[start:end], part_locator


def _chunks(blocks, warnings, max_chars=MAX_TEXT_CHARS):
    chunks, parents = [], []
    total = 0
    clause = clause_parent = None
    section = None
    clause_heading_path = []
    by_block = {}
    for ordinal, (text, locator) in enumerate(_pdf_sections(blocks), 1):
        text = text.replace('\x00', '').strip()
        if not has_readable_text(text):
            continue
        total += len(text)
        if total > max_chars:
            raise CoreDataError('rag_document_too_large', 'The extracted document exceeds the text limit.')
        block_id = f'block-{ordinal}'
        locator = {**locator, 'block_id': block_id, 'splitting_version': SPLITTING_VERSION}
        pdf_clause = locator.pop('_pdf_clause', False)
        pdf_section = locator.pop('_pdf_section', None)
        if pdf_section and clause and clause.split('.')[0] != pdf_section:
            clause = clause_parent = None
            clause_heading_path = []
        if locator.get('part') in {'footnote', 'endnote'}:
            anchor = next((by_block[reference] for reference in locator.get('reference_blocks', []) if reference in by_block), None)
            block_clause = anchor['clause_number'] if anchor else None
            block_parent = anchor['locator']['clause_parent_id'] if anchor else block_id
            locator['heading_path'] = anchor['locator'].get('heading_path', []) if anchor else []
        else:
            match = CLAUSE.match(text) if locator.get('kind') != 'pdf' or pdf_clause else None
            section_match = SECTION.search(text) if locator.get('kind') == 'pdf' else SECTION.match(text)
            new_section = ' '.join(section_match.group(0).split()).casefold() if section_match else None
            if match:
                clause, clause_parent = match.group(1), block_id
                clause_heading_path = locator.get('heading_path', [])
            elif (new_section and new_section != section) or (locator.get('heading_level') and locator.get('heading_path', [])[:1] != clause_heading_path[:1]):
                clause = clause_parent = None
            if new_section:
                section = new_section
            block_clause, block_parent = clause, clause_parent or block_id
        locator['clause_parent_id'] = block_parent
        parent = {'text': text, 'locator': locator, 'clause_number': block_clause}
        parents.append(parent)
        by_block[block_id] = parent
    group_sizes = {}
    for parent in parents:
        key = parent['locator']['clause_parent_id']
        group_sizes[key] = group_sizes.get(key, 0) + 1
    for index, parent in enumerate(parents):
        text, locator = parent['text'], parent['locator']
        fragments = list(_split_block(text))
        if len(fragments) > 1:
            warnings.append('Long source blocks are split into linked fragments. Read their parent context before using conditions or exceptions.')
        for fragment_index, (start, end, boundary) in enumerate(fragments):
            excerpt = text[start:end]
            if not has_readable_text(excerpt):
                continue
            chunks.append({'text': excerpt, 'text_hash': hashlib.sha256(excerpt.encode('utf-8')).hexdigest(),
                           'clause_number': parent['clause_number'], 'locator': {
                               **locator, 'offset': start, 'parent_length': len(text),
                               'fragment_index': fragment_index, 'fragment_count': len(fragments),
                               'continues_before': start > 0, 'continues_after': end < len(text),
                               'context_required': len(fragments) > 1 or group_sizes[locator['clause_parent_id']] > 1,
                               'split_boundary': boundary,
                               'previous_block_id': parents[index - 1]['locator']['block_id'] if index else None,
                               'next_block_id': parents[index + 1]['locator']['block_id'] if index + 1 < len(parents) else None}})
            if boundary == 'forced':
                warnings.append('A sentence exceeds the fragment limit. Its continuation and full parent context must be reviewed.')
            if len(chunks) > MAX_CHUNKS:
                raise CoreDataError('rag_capacity_exceeded', 'The document exceeds 100,000 chunks.')
    if not chunks:
        raise CoreDataError('rag_document_no_text', 'No readable text was found. Scanned PDFs require OCR before import.')
    return {'chunks': chunks, 'warnings': list(dict.fromkeys(warnings))}


def parse_text(content: str, max_chars=MAX_TEXT_CHARS):
    if not isinstance(content, str):
        raise CoreDataError('rag_invalid_document', 'Pasted content must be text.')
    blocks = [(text, {'kind': 'text', 'paragraph': index + 1})
              for index, text in enumerate(re.split(r'\n\s*\n', content))]
    return _chunks(blocks, [], max_chars)


def _paragraph(node):
    pieces = []
    for child in node.iter():
        if child.tag == NS + 't':
            pieces.append(child.text or '')
        elif child.tag == NS + 'tab':
            pieces.append('\t')
        elif child.tag in {NS + 'br', NS + 'cr'}:
            pieces.append('\n')
    return ''.join(pieces)


def _body_blocks(parent):
    for node in parent:
        if node.tag in {NS + 'p', NS + 'tbl'}:
            yield node
        elif node.tag in {NS + 'sdt', NS + 'sdtContent', NS + 'customXml', NS + 'ins', NS + 'del'}:
            yield from _body_blocks(node)


def _heading_level(paragraph, styles):
    node = paragraph.find(NS + 'pPr')
    seen = set()
    while node is not None:
        outline = node.find(NS + 'outlineLvl')
        if outline is not None:
            value = outline.get(NS + 'val', '')
            return int(value) + 1 if value.isdigit() and int(value) < 9 else None
        style = node.find(NS + 'pStyle')
        if style is None:
            style = node.find(NS + 'basedOn')
        if style is None:
            return None
        style_id = style.get(NS + 'val', '')
        if style_id in seen:
            return None
        seen.add(style_id)
        definition = styles.get(style_id)
        name = definition.find(NS + 'name') if definition is not None else None
        for candidate in (style_id, name.get(NS + 'val', '') if name is not None else ''):
            match = re.fullmatch(r'(?:heading|标题)\s*([1-9])', candidate, re.IGNORECASE)
            if match:
                return int(match.group(1))
        if definition is None:
            return None
        properties = definition.find(NS + 'pPr')
        if properties is not None and properties.find(NS + 'outlineLvl') is not None:
            node = properties
        else:
            node = definition
    return None


def _record_note_references(node, block_id, anchors):
    references = []
    for part in ('footnote', 'endnote'):
        for reference in node.iter(NS + part + 'Reference'):
            note_id = reference.get(NS + 'id', '')
            anchors.setdefault((part, note_id), []).append(block_id)
            references.append({'part': part, 'note_id': note_id})
    return references


def _parse_docx(path, max_expanded_bytes):
    blocks, warnings = [], []
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > 10_000 or sum(item.file_size for item in entries) > max_expanded_bytes:
                raise CoreDataError('rag_document_too_large', 'The expanded DOCX exceeds its limit.')
            main = archive.read('word/document.xml')
            if b'<!DOCTYPE' in main or b'<!ENTITY' in main:
                raise CoreDataError('rag_invalid_document', 'DOCX XML declarations are not supported.')
            root = ET.fromstring(main)
            body = root.find(NS + 'body')
            if body is None:
                raise CoreDataError('rag_invalid_document', 'DOCX body is missing.')
            styles = {}
            if 'word/styles.xml' in archive.namelist():
                style_xml = archive.read('word/styles.xml')
                if b'<!DOCTYPE' in style_xml or b'<!ENTITY' in style_xml:
                    raise CoreDataError('rag_invalid_document', 'DOCX style declarations are not supported.')
                styles = {style.get(NS + 'styleId'): style for style in ET.fromstring(style_xml).findall(NS + 'style')}
            heading_path, note_anchors = [], {}
            paragraph = table = 0
            for node in _body_blocks(body):
                if node.tag == NS + 'p':
                    paragraph += 1
                    text = _paragraph(node)
                    level = _heading_level(node, styles)
                    if level and text.strip():
                        while heading_path and heading_path[-1][0] >= level:
                            heading_path.pop()
                        heading_path.append((level, text.strip()))
                    references = _record_note_references(node, f'block-{len(blocks) + 1}', note_anchors)
                    blocks.append((text, {'kind': 'docx', 'part': 'body', 'paragraph': paragraph,
                                          'heading_path': [heading for _, heading in heading_path],
                                          'heading_level': level, 'note_references': references}))
                elif node.tag == NS + 'tbl':
                    table += 1
                    if node.findall('.//' + NS + 'tbl'):
                        warnings.append('Nested tables are flattened. Verify reading order.')
                    if node.findall('.//' + NS + 'gridSpan') or node.findall('.//' + NS + 'vMerge'):
                        warnings.append('Merged table cells are flattened. Verify row and column alignment.')
                    table_parent = f'block-{len(blocks) + 1}'
                    for row_index, row in enumerate(node.findall(NS + 'tr'), 1):
                        cells = ['\n'.join(_paragraph(p) for p in cell.findall('.//' + NS + 'p'))
                                 for cell in row.findall(NS + 'tc')]
                        references = _record_note_references(row, f'block-{len(blocks) + 1}', note_anchors)
                        blocks.append((' | '.join(cells), {'kind': 'docx', 'part': 'body', 'table': table, 'row': row_index,
                                                          'table_parent_id': table_parent, 'note_references': references,
                                                          'heading_path': [heading for _, heading in heading_path]}))
            for part, filename in [('footnote', 'word/footnotes.xml'), ('endnote', 'word/endnotes.xml')]:
                if filename not in archive.namelist():
                    continue
                note_xml = archive.read(filename)
                if b'<!DOCTYPE' in note_xml or b'<!ENTITY' in note_xml:
                    raise CoreDataError('rag_invalid_document', 'DOCX note declarations are not supported.')
                notes = ET.fromstring(note_xml)
                for note in notes:
                    note_id = note.get(NS + 'id', '')
                    if note_id.startswith('-') or note.get(NS + 'type'):
                        continue
                    for index, p in enumerate(note.findall(NS + 'p'), 1):
                        blocks.append((_paragraph(p), {'kind': 'docx', 'part': part, 'note_id': note_id, 'paragraph': index,
                                                       'reference_blocks': list(dict.fromkeys(note_anchors.get((part, note_id), [])))}))
                warnings.append('Notes follow the document body in note order. Verify references and reading order.')
            if root.findall('.//' + NS + 'drawing') or root.findall('.//' + NS + 'pict'):
                warnings.append('Images and diagrams are excluded from text extraction.')
            if root.findall('.//' + NS + 'del') or root.findall('.//' + NS + 'ins'):
                warnings.append('Tracked revisions are present. Verify the extracted text against the original.')
            if root.findall('.//' + NS + 'altChunk'):
                warnings.append('Embedded external document parts are excluded from text extraction.')
            if root.findall('.//{http://schemas.openxmlformats.org/officeDocument/2006/math}oMath'):
                warnings.append('Mathematical equations are excluded from text extraction.')
            if any(name.startswith(('word/header', 'word/footer', 'word/comments')) for name in archive.namelist()):
                warnings.append('Headers, footers and comments are excluded from main document evidence.')
    except (zipfile.BadZipFile, KeyError, ET.ParseError, OSError) as error:
        raise CoreDataError('rag_invalid_document', f'Unable to parse DOCX: {error}') from error
    return _chunks(blocks, warnings)


def _parse_pdf_document(path, deadline):
    import pypdfium2 as pdfium
    blocks, warnings = [], ['PDF reading order and table structure may be uncertain. Verify the page locators.']
    try:
        with pdfium.PdfDocument(str(path)) as pdf:
            if time.monotonic() >= deadline:
                raise CoreDataError('rag_document_timeout', 'PDF parsing exceeded its time limit.')
            if len(pdf) > 10_000:
                raise CoreDataError('rag_document_too_large', 'PDF exceeds 10,000 pages.')
            total_characters = 0
            for index in range(len(pdf)):
                if time.monotonic() >= deadline:
                    raise CoreDataError('rag_document_timeout', 'PDF parsing exceeded its time limit.')
                page = pdf[index]
                textpage = page.get_textpage()
                total_characters += textpage.count_chars()
                if total_characters > MAX_TEXT_CHARS:
                    textpage.close()
                    page.close()
                    raise CoreDataError('rag_document_too_large', 'The extracted PDF exceeds the text limit.')
                text = textpage.get_text_range()
                textpage.close()
                page.close()
                if time.monotonic() >= deadline:
                    raise CoreDataError('rag_document_timeout', 'PDF parsing exceeded its time limit.')
                if not text.strip():
                    warnings.append(f'Page {index + 1} has no text and may require OCR.')
                blocks.append((text, {'kind': 'pdf', 'page': index + 1}))
    except CoreDataError:
        raise
    except Exception as error:
        raise CoreDataError('rag_invalid_document', f'Unable to parse PDF: {error}') from error
    return _chunks(blocks, warnings)


def _parse_pdf(path, *, adaptive=False):
    deadline = float('inf') if adaptive else time.monotonic() + PDF_PARSE_TIMEOUT
    acquired = _PDF_LOCK.acquire() if adaptive else _PDF_LOCK.acquire(timeout=PDF_PARSE_TIMEOUT)
    if not acquired:
        raise CoreDataError('rag_document_timeout', 'PDF parsing timed out waiting for the PDF parser.')
    try:
        return _parse_pdf_document(path, deadline)
    finally:
        _PDF_LOCK.release()


def _parse_text_file(path):
    with path.open('rb') as source:
        content_bytes = source.read(MAX_FILE_BYTES + 1)
    if len(content_bytes) > MAX_FILE_BYTES:
        raise CoreDataError('rag_document_too_large', 'The document exceeds 100 MiB.')
    try:
        content = content_bytes.decode('utf-8-sig')
    except UnicodeDecodeError as error:
        raise CoreDataError('rag_document_encoding_invalid', 'TXT and Markdown documents must use UTF-8 encoding.') from error
    if len(content) > MAX_TEXT_CHARS:
        raise CoreDataError('rag_document_too_large', 'The text document exceeds its character limit.')
    blocks, paragraph, line_start = [], [], 1
    kind = 'markdown' if path.suffix.lower() in {'.md', '.markdown'} else 'text'
    lines = content.splitlines()
    for number, line in enumerate(lines, 1):
        if line.strip():
            if not paragraph:
                line_start = number
            paragraph.append(line)
        elif paragraph:
            blocks.append(('\n'.join(paragraph), {'kind': kind, 'paragraph': len(blocks) + 1,
                                                'line_start': line_start, 'line_end': number - 1}))
            paragraph = []
    if paragraph:
        blocks.append(('\n'.join(paragraph), {'kind': kind, 'paragraph': len(blocks) + 1,
                                            'line_start': line_start, 'line_end': len(lines)}))
    return _chunks(blocks, [])


def parse_document(path: Path, max_expanded_bytes=MAX_EXPANDED_BYTES, *, adaptive=False):
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise CoreDataError('rag_document_unsupported', 'Supported documents are Office, WPS, PDF, UTF-8 TXT and Markdown.')
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise CoreDataError('rag_document_too_large', 'The document exceeds 100 MiB.')
    except OSError as error:
        raise CoreDataError('rag_document_unavailable', 'The document file is unavailable.') from error
    if suffix == '.docx':
        return _parse_docx(path, max_expanded_bytes)
    if suffix in OFFICE_SUFFIXES:
        from agent.document_parser.office import parse_office_document
        return parse_office_document(path, max_expanded_bytes=max_expanded_bytes)
    if suffix == '.pdf':
        return _parse_pdf(path, adaptive=adaptive)
    return _parse_text_file(path)
