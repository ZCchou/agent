"""Incremental native extraction. Conversion-backed formats retain a full stage."""
from __future__ import annotations

import hashlib
import time
import xml.etree.ElementTree as ET
import zipfile

from agent.storage import CoreDataError
from agent.rag import documents as d

PARSER_VERSION = 'document-parser-v2'


class SafeXmlReader:
    """Reject declarations even when a token straddles two parser reads."""
    def __init__(self, source):
        self.source = source
        self.tail = b''

    def read(self, size=-1):
        data = self.source.read(size)
        combined = self.tail + data
        if b'<!DOCTYPE' in combined or b'<!ENTITY' in combined:
            raise CoreDataError('rag_invalid_document', 'DOCX XML declarations are not supported.')
        self.tail = combined[-16:]
        return data


def docx_blocks(path, warnings, max_expanded_bytes):
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > 10000 or sum(item.file_size for item in entries) > max_expanded_bytes:
                raise CoreDataError('rag_document_too_large', 'The expanded DOCX exceeds its limit.')
            styles = {}
            if 'word/styles.xml' in archive.namelist():
                with archive.open('word/styles.xml') as source:
                    root = ET.parse(SafeXmlReader(source)).getroot()
                styles = {node.get(d.NS + 'styleId'): node for node in root.findall(d.NS + 'style')}
            headings, anchors = [], {}
            ordinal = paragraph = table = row = 0
            table_parent = None
            stack = []
            saw_body = False
            with archive.open('word/document.xml') as source:
                for event, node in ET.iterparse(SafeXmlReader(source), events=('start', 'end')):
                    if event == 'start':
                        stack.append(node)
                        if node.tag == d.NS + 'body':
                            saw_body = True
                        if node.tag == d.NS + 'tbl' and not any(p.tag == d.NS + 'tbl' for p in stack[:-1]):
                            table += 1
                            row = 0
                            table_parent = f'block-{ordinal + 1}'
                        continue
                    tag = node.tag
                    in_table = any(p.tag == d.NS + 'tbl' for p in stack[:-1])
                    if tag in {d.NS + 'drawing', d.NS + 'pict'}:
                        warnings.append('Images and diagrams are excluded from text extraction.')
                    if tag in {d.NS + 'del', d.NS + 'ins'}:
                        warnings.append('Tracked revisions are present. Verify the extracted text against the original.')
                    if tag == d.NS + 'tbl' and in_table:
                        warnings.append('Nested tables are flattened. Verify reading order.')
                    if tag == d.NS + 'altChunk':
                        warnings.append('Embedded external document parts are excluded from text extraction.')
                    if tag == '{http://schemas.openxmlformats.org/officeDocument/2006/math}oMath':
                        warnings.append('Mathematical equations are excluded from text extraction.')
                    if tag in {d.NS + 'gridSpan', d.NS + 'vMerge'}:
                        warnings.append('Merged table cells are flattened. Verify row and column alignment.')
                    block = None
                    if (tag == d.NS + 'p' and not in_table and any(p.tag == d.NS + 'body' for p in stack)
                            and not any(p.tag == d.NS + 'p' for p in stack[:-1])):
                        paragraph += 1
                        text = d._paragraph(node)
                        level = d._heading_level(node, styles)
                        if level and text.strip():
                            while headings and headings[-1][0] >= level:
                                headings.pop()
                            headings.append((level, text.strip()))
                        block = (text, {'kind': 'docx', 'part': 'body', 'paragraph': paragraph,
                                       'heading_level': level})
                    elif tag == d.NS + 'tr' and sum(p.tag == d.NS + 'tbl' for p in stack) == 1:
                        row += 1
                        cells = ['\n'.join(d._paragraph(p) for p in cell.findall('.//' + d.NS + 'p'))
                                 for cell in node.findall(d.NS + 'tc')]
                        block = (' | '.join(cells), {'kind': 'docx', 'part': 'body', 'table': table,
                                                   'row': row, 'table_parent_id': table_parent})
                    if block is not None:
                        ordinal += 1
                        block[1]['heading_path'] = [value for _, value in headings]
                        block[1]['note_references'] = d._record_note_references(node, f'block-{ordinal}', anchors)
                        yield block
                        node.clear()
                        if len(stack) > 1:
                            stack[-2].remove(node)
                    elif tag == d.NS + 'tbl' and not any(p.tag == d.NS + 'tbl' for p in stack[:-1]):
                        node.clear()
                        if len(stack) > 1:
                            stack[-2].remove(node)
                    stack.pop()
            if not saw_body:
                raise CoreDataError('rag_invalid_document', 'DOCX body is missing.')
            for part in ('footnote', 'endnote'):
                filename = 'word/' + part + 's.xml'
                if filename not in archive.namelist():
                    continue
                with archive.open(filename) as source:
                    for event, note in ET.iterparse(SafeXmlReader(source), events=('end',)):
                        if note.tag != d.NS + part:
                            continue
                        note_id = note.get(d.NS + 'id', '')
                        if not note_id.startswith('-') and not note.get(d.NS + 'type'):
                            for index, paragraph_node in enumerate(note.findall(d.NS + 'p'), 1):
                                yield d._paragraph(paragraph_node), {'kind': 'docx', 'part': part,
                                    'note_id': note_id, 'paragraph': index,
                                    'reference_blocks': list(dict.fromkeys(anchors.get((part, note_id), [])))}
                        note.clear()
                warnings.append('Notes follow the document body in note order. Verify references and reading order.')
            if any(name.startswith(('word/header', 'word/footer', 'word/comments')) for name in archive.namelist()):
                warnings.append('Headers, footers and comments are excluded from main document evidence.')
    except (zipfile.BadZipFile, KeyError, ET.ParseError, OSError) as error:
        raise CoreDataError('rag_invalid_document', 'Unable to parse DOCX.') from error


def pdf_blocks(path, warnings, adaptive):
    import pypdfium2 as pdfium
    warnings.append('PDF reading order and table structure may be uncertain. Verify the page locators.')
    deadline = float('inf') if adaptive else time.monotonic() + d.PDF_PARSE_TIMEOUT
    acquired = d._PDF_LOCK.acquire() if adaptive else d._PDF_LOCK.acquire(timeout=d.PDF_PARSE_TIMEOUT)
    if not acquired:
        raise CoreDataError('rag_document_timeout', 'PDF parsing timed out waiting for the PDF parser.')
    try:
        with pdfium.PdfDocument(str(path)) as pdf:
            if len(pdf) > 10000:
                raise CoreDataError('rag_document_too_large', 'PDF exceeds 10,000 pages.')
            for index in range(len(pdf)):
                if time.monotonic() >= deadline:
                    raise CoreDataError('rag_document_timeout', 'PDF parsing exceeded its time limit.')
                page = pdf[index]
                try:
                    textpage = page.get_textpage()
                    try:
                        if textpage.count_chars() > d.MAX_TEXT_CHARS:
                            raise CoreDataError('rag_document_too_large', 'PDF page exceeds the text limit.')
                        text = textpage.get_text_range()
                    finally:
                        textpage.close()
                finally:
                    page.close()
                if not text.strip():
                    warnings.append(f'Page {index + 1} has no text and may require OCR.')
                yield text, {'kind': 'pdf', 'page': index + 1}

    except CoreDataError:
        raise
    except Exception as error:
        raise CoreDataError('rag_invalid_document', 'Unable to parse PDF.') from error
    finally:
        d._PDF_LOCK.release()


def chunk_stream(blocks, warnings):
    """Keep one source block in memory, with conservative clause context flags."""
    clause = clause_parent = section = None
    clause_headings = []
    anchors = {}
    previous = None
    total = count = 0
    for ordinal, (text, raw_locator) in enumerate(d._pdf_sections(blocks), 1):
        text = text.replace('\x00', '').strip()
        if not d.has_readable_text(text):
            continue
        total += len(text)
        if total > d.MAX_TEXT_CHARS:
            raise CoreDataError('rag_document_too_large', 'The extracted document exceeds the text limit.')
        block_id = f'block-{ordinal}'
        locator = {**raw_locator, 'block_id': block_id, 'splitting_version': d.SPLITTING_VERSION}
        pdf_clause = locator.pop('_pdf_clause', False)
        pdf_section = locator.pop('_pdf_section', None)
        if pdf_section and clause and clause.split('.')[0] != pdf_section:
            clause = clause_parent = None
            clause_headings = []
        if locator.get('part') in {'footnote', 'endnote'}:
            anchor = next((anchors[ref] for ref in locator.get('reference_blocks', []) if ref in anchors), None)
            block_clause, parent, heading_path = anchor if anchor else (None, block_id, [])
            locator['heading_path'] = heading_path
        else:
            match = d.CLAUSE.match(text) if locator['kind'] != 'pdf' or pdf_clause else None
            section_match = d.SECTION.search(text) if locator['kind'] == 'pdf' else d.SECTION.match(text)
            new_section = ' '.join(section_match.group(0).split()).casefold() if section_match else None
            if match:
                clause, clause_parent = match.group(1), block_id
                clause_headings = locator.get('heading_path', [])
            elif (new_section and new_section != section) or (locator.get('heading_level') and locator.get('heading_path', [])[:1] != clause_headings[:1]):
                clause = clause_parent = None
            if new_section:
                section = new_section
            block_clause, parent = clause, clause_parent or block_id
        locator['clause_parent_id'] = parent
        if locator.get('note_references'):
            anchors[block_id] = (block_clause, parent, locator.get('heading_path', []))
        fragments = list(d._split_block(text))
        if len(fragments) > 1:
            warnings.append('Long source blocks are split into linked fragments. Read their parent context before using conditions or exceptions.')
        for index, (start, end, boundary) in enumerate(fragments):
            excerpt = text[start:end]
            if not d.has_readable_text(excerpt):
                continue
            count += 1
            if count > d.MAX_CHUNKS:
                raise CoreDataError('rag_capacity_exceeded', 'The document exceeds 100,000 chunks.')
            yield {'text': excerpt, 'text_hash': hashlib.sha256(excerpt.encode()).hexdigest(),
                'clause_number': block_clause, 'locator': {**locator, 'offset': start, 'parent_length': len(text),
                    'fragment_index': index, 'fragment_count': len(fragments), 'continues_before': start > 0,
                    'continues_after': end < len(text), 'context_required': len(fragments) > 1 or block_clause is not None,
                    'split_boundary': boundary, 'previous_block_id': previous, 'next_block_id': None}}
            if boundary == 'forced':
                warnings.append('A sentence exceeds the fragment limit. Its continuation and full parent context must be reviewed.')
        previous = block_id
    if not count:
        raise CoreDataError('rag_document_no_text', 'No readable text was found. Scanned PDFs require OCR before import.')


def linked_chunks(chunks):
    """Resolve the next-block link with one source block of lookahead."""
    pending = []
    block_id = None
    for chunk in chunks:
        following = chunk['locator']['block_id']
        if block_id is not None and following != block_id:
            for previous in pending:
                previous['locator']['next_block_id'] = following
                yield previous
            pending = []
        pending.append(chunk)
        block_id = following
    yield from pending


def stream_document(path, *, max_expanded_bytes=d.MAX_EXPANDED_BYTES, adaptive=False):
    warnings = []
    if path.stat().st_size > d.MAX_FILE_BYTES:
        raise CoreDataError('rag_document_too_large', 'The document exceeds 100 MiB.')
    if path.suffix.lower() == '.docx':
        blocks = docx_blocks(path, warnings, max_expanded_bytes)
    elif path.suffix.lower() == '.pdf':
        blocks = pdf_blocks(path, warnings, adaptive)
    else:
        # Legacy Office conversion and text decoding remain full extraction stages.
        return d.parse_document(path, max_expanded_bytes=max_expanded_bytes, adaptive=adaptive)
    return {'chunks': linked_chunks(chunk_stream(blocks, warnings)), 'warnings': warnings}
