"""Read Office XML without running formulas, macros, or external relationships."""
from __future__ import annotations

from pathlib import Path
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile

from agent.storage import CoreDataError

WORD_SUFFIXES = {'.docx', '.docm', '.dotx', '.dotm'}
SHEET_SUFFIXES = {'.xlsx', '.xlsm', '.xltx', '.xltm'}
SLIDE_SUFFIXES = {'.pptx', '.pptm', '.potx', '.potm', '.ppsx', '.ppsm'}
LEGACY_WORD = {'.doc', '.dot', '.wps', '.wpt', '.rtf'}
LEGACY_SHEET = {'.xls', '.xlt', '.xlsb', '.et', '.ett'}
LEGACY_SLIDE = {'.ppt', '.pot', '.pps', '.dps', '.dpt'}
OFFICE_SUFFIXES = WORD_SUFFIXES | SHEET_SUFFIXES | SLIDE_SUFFIXES | LEGACY_WORD | LEGACY_SHEET | LEGACY_SLIDE
S = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
R = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
A = '{http://schemas.openxmlformats.org/drawingml/2006/main}'
P = '{http://schemas.openxmlformats.org/presentationml/2006/main}'


def is_office_suffix(suffix):
    return str(suffix).lower() in OFFICE_SUFFIXES


def _xml(archive, name):
    data = archive.read(name)
    # Null removal also catches declarations in UTF-16/32 XML before parsing.
    declarations = data.replace(b'\x00', b'').upper()
    if b'<!DOCTYPE' in declarations or b'<!ENTITY' in declarations:
        raise CoreDataError('rag_invalid_document', 'Office XML declarations are not supported.')
    return ET.fromstring(data)


def _relationships(archive, source):
    directory, filename = posixpath.split(source)
    root = _xml(archive, f'{directory}/_rels/{filename}.rels')
    result = {}
    for relation in root:
        target = relation.get('Target', '')
        if relation.get('TargetMode') == 'External':
            continue
        resolved = posixpath.normpath(posixpath.join(directory, target)) if not target.startswith('/') else target[1:]
        if not target or '\\' in target or ':' in target or resolved.startswith('../'):
            raise CoreDataError('rag_invalid_document', 'Office relationship target is invalid.')
        result[relation.get('Id')] = resolved
    return result


def _sheet_blocks(archive, warnings):
    strings = []
    if 'xl/sharedStrings.xml' in archive.namelist():
        strings = [''.join(n.text or '' for n in item.iter(S + 't')) for item in _xml(archive, 'xl/sharedStrings.xml')]
    workbook = _xml(archive, 'xl/workbook.xml')
    relationships = _relationships(archive, 'xl/workbook.xml')
    warnings.append('Spreadsheet formulas use cached results only and are never calculated. Dates and numbers retain their stored values.')
    for sheet_index, sheet in enumerate(workbook.findall(S + 'sheets/' + S + 'sheet'), 1):
        root = _xml(archive, relationships[sheet.get(R + 'id')])
        for row_index, row in enumerate(root.findall(S + 'sheetData/' + S + 'row'), 1):
            for column, cell in enumerate(row.findall(S + 'c'), 1):
                address = cell.get('r', '')
                if not re.fullmatch(r'[A-Z]+[1-9][0-9]*', address):
                    raise CoreDataError('rag_invalid_document', 'Spreadsheet cell position is missing or invalid.')
                value = cell.findtext(S + 'v', '')
                kind = cell.get('t')
                if kind == 's':
                    try:
                        index = int(value)
                        if index < 0:
                            raise ValueError('negative shared string index')
                        value = strings[index]
                    except (ValueError, IndexError) as error:
                        raise CoreDataError('rag_invalid_document', 'Spreadsheet shared string index is invalid.') from error
                elif kind == 'inlineStr':
                    value = ''.join(n.text or '' for n in cell.iter(S + 't'))
                if cell.find(S + 'f') is not None and not value:
                    warnings.append('Some formulas have no cached result and are excluded from extracted text.')
                yield value, {'kind': 'xlsx', 'sheet': sheet.get('name', ''), 'sheet_index': sheet_index,
                              'cell': address, 'row': int(re.search(r'[0-9]+$', address)[0]),
                              'column': re.match(r'[A-Z]+', address)[0]}
        if root.find(S + 'mergeCells') is not None:
            warnings.append('Merged spreadsheet cells retain their anchor cell position.')


def _drawing_paragraph(node):
    return ''.join('\n' if child.tag == A + 'br' else child.text or ''
                   for child in node.iter() if child.tag in {A + 't', A + 'br'})


def _slide_blocks(archive, warnings):
    root = _xml(archive, 'ppt/presentation.xml')
    relationships = _relationships(archive, 'ppt/presentation.xml')
    warnings.append('Slide text follows shape order. Images, charts, animations, speaker notes and diagrams may require visual review.')
    for slide, item in enumerate(root.findall(P + 'sldIdLst/' + P + 'sldId'), 1):
        slide_root = _xml(archive, relationships[item.get(R + 'id')])
        tree = slide_root.find(P + 'cSld/' + P + 'spTree')
        if tree is None:
            continue
        paragraph = table = 0
        # Recursive shape traversal preserves grouped shapes and tables in source order.
        def walk(node):
            nonlocal paragraph, table
            if node.tag == A + 'tbl':
                table += 1
                for row, tr in enumerate(node.findall(A + 'tr'), 1):
                    for column, cell in enumerate(tr.findall(A + 'tc'), 1):
                        text = '\n'.join(_drawing_paragraph(p) for p in cell.iter(A + 'p'))
                        yield text, {'kind': 'pptx', 'slide': slide, 'table': table, 'row': row, 'column': column}
            elif node.tag == A + 'p':
                paragraph += 1
                yield _drawing_paragraph(node), {'kind': 'pptx', 'slide': slide, 'paragraph': paragraph}
            else:
                for child in node:
                    yield from walk(child)
        yield from walk(tree)


def parse_office_document(path, max_expanded_bytes=200 * 1024 * 1024):
    from agent.rag.documents import MAX_FILE_BYTES, _chunks, _parse_docx
    from agent.document_parser.conversion import convert_office

    path = Path(path)
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise CoreDataError('rag_document_too_large', 'The document exceeds 100 MiB.')
        with path.open('rb') as source:
            signature = source.read(8)
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                entries = archive.infolist()
                if len(entries) > 10_000 or sum(entry.file_size for entry in entries) > max_expanded_bytes:
                    raise CoreDataError('rag_document_too_large', 'The expanded Office document exceeds its limit.')
                if len({e.filename for e in entries}) != len(entries):
                    raise CoreDataError('rag_invalid_document', 'Duplicate Office package members are unsupported.')
                if any(e.flag_bits & 1 for e in entries):
                    raise CoreDataError('rag_document_encrypted', 'Password-protected Office documents are unsupported.')
                names = archive.namelist()
                media_warnings = (['Embedded images are excluded from text extraction and require OCR.']
                                  if any(name.startswith(('word/media/', 'xl/media/', 'ppt/media/')) for name in names) else [])
                if 'word/document.xml' in names:
                    # Validate every XML part before reusing the established Word reader.
                    for name in names:
                        if name.endswith(('.xml', '.rels')):
                            _xml(archive, name)
                    result = _parse_docx(path, max_expanded_bytes)
                    result['warnings'].extend(media_warnings)
                    return result
                warnings = media_warnings
                if 'xl/workbook.xml' in names:
                    return _chunks(_sheet_blocks(archive, warnings), warnings)
                if 'ppt/presentation.xml' in names:
                    return _chunks(_slide_blocks(archive, warnings), warnings)
                if path.suffix.lower() != '.xlsb' or 'xl/workbook.bin' not in names:
                    raise CoreDataError('rag_invalid_document', 'No supported Office content was found in the package.')
        elif signature.startswith(b'PK'):
            raise CoreDataError('rag_invalid_document', 'The Office ZIP package is damaged.')
        elif path.suffix.lower() in WORD_SUFFIXES | SHEET_SUFFIXES | SLIDE_SUFFIXES:
            code = 'rag_document_encrypted' if signature == b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1' else 'rag_invalid_document'
            raise CoreDataError(code, 'The Office document is encrypted or has an invalid container.')
        suffix = path.suffix.lower()
        if suffix not in LEGACY_WORD | LEGACY_SHEET | LEGACY_SLIDE:
            raise CoreDataError('rag_document_unsupported', 'Unsupported Office document suffix.')
        target = '.docx' if suffix in LEGACY_WORD else '.xlsx' if suffix in LEGACY_SHEET else '.pptx'
        with convert_office(path, target) as converted:
            result = parse_office_document(converted, max_expanded_bytes)
        result['warnings'].append('This document was converted locally. Verify layout and source positions against the original.')
        return result
    except CoreDataError:
        raise
    except (OSError, KeyError, ET.ParseError, zipfile.BadZipFile, RuntimeError, ValueError, RecursionError) as error:
        raise CoreDataError('rag_invalid_document', f'Unable to parse Office document: {error}') from error
