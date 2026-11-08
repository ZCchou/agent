"""Bounded on-disk document worker interchange and managed-path checks."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import stat

from agent.storage import CoreDataError

MEMORY_LIMIT = 1024 * 1024 * 1024
RESULT_LIMIT = 256 * 1024 * 1024
STATUS_LIMIT = 1024 * 1024
TIMEOUT = 125
MARKER = '.agent-parser-owned'
RESULT = 'result.ndjson'
STAGE_NAME = re.compile(r'parser-[0-9a-f]{32}')
ORIGINAL_NAME = re.compile(r'([0-9a-f]{64})(\.(?:txt|md|markdown|pdf|docx))')


def invalid(message='The document worker returned an invalid result.'):
    return CoreDataError('rag_parser_invalid_result', message)


def plain_path(path):
    """Reject symlinks, junctions and other reparse points along every ancestor."""
    path = Path(path).absolute()
    for component in [*reversed(path.parents), path]:
        info = component.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise invalid('Document parser paths must not contain links or junctions.')
    return path


def managed_original(data_dir, original):
    try:
        original = Path(original)
        if not original.is_absolute() or original != original.resolve(strict=True):
            raise ValueError('Noncanonical original')
        plain_path(original)
        relative = original.relative_to(Path(data_dir) / 'originals')
        match = ORIGINAL_NAME.fullmatch(original.name)
        info = original.stat()
        if len(relative.parts) != 2 or not match or relative.parts[0] != match[1][:2]:
            raise ValueError('Not a repository original')
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('Not an exclusive regular original')
        if info.st_size > 100 * 1024 * 1024:
            raise CoreDataError('rag_document_too_large', 'The document exceeds 100 MiB.')
        return original
    except (OSError, ValueError, CoreDataError) as error:
        if isinstance(error, CoreDataError) and error.code == 'rag_document_too_large':
            raise
        raise CoreDataError('rag_document_unavailable', 'Select a managed repository original without links.') from error


def stage_marker(stage):
    return 'agent-document-parser-v1\n' + stage.name + '\n'


def owned_stage(stage):
    plain_path(stage)
    marker = plain_path(stage / MARKER)
    return (bool(STAGE_NAME.fullmatch(stage.name)) and marker.is_file()
            and marker.stat().st_nlink == 1 and marker.stat().st_size < 128
            and marker.read_text(encoding='ascii') == stage_marker(stage))


def clean_stage(stage):
    """Only remove our flat, explicitly marked staging records, never recurse."""
    try:
        if not owned_stage(stage):
            return
        entries = list(stage.iterdir())
        if any(item.name not in {MARKER, RESULT} or not plain_path(item).is_file()
               or item.stat().st_nlink != 1 for item in entries):
            return
        for item in entries:
            item.unlink()
        stage.rmdir()
    except (OSError, UnicodeError, CoreDataError):
        return


def encode_frame(frame):
    return (json.dumps(frame, ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n').encode('utf-8')


def decode_frame(data):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError('Nonfinite JSON constant')

    return json.loads(data, object_pairs_hook=unique_object, parse_constant=reject_constant)


def write_result(path, request_id, parsed, result_limit):
    total = 0
    header = encode_frame({'type': 'header', 'request_id': request_id, 'warnings': parsed['warnings']})
    if len(header) > STATUS_LIMIT:
        raise invalid('Document metadata exceeds its limit.')
    with path.open('xb') as output:
        frames = iter(parsed['chunks'])
        frame = header
        while True:
            total += len(frame)
            if total > result_limit:
                raise invalid('Document result exceeds its byte limit.')
            output.write(frame)
            chunk = next(frames, None)
            if chunk is None:
                break
            frame = encode_frame({'type': 'chunk', 'chunk': chunk})
        trailer = encode_frame({'type': 'end', 'request_id': request_id, 'chunks': len(parsed['chunks'])})
        if total + len(trailer) > result_limit:
            raise invalid('Document result exceeds its byte limit.')
        output.write(trailer)


def validate_locator(locator, text_length):
    if locator.get('kind') not in {'docx', 'pdf', 'text', 'markdown'}:
        raise invalid()
    for field in ('block_id', 'clause_parent_id', 'splitting_version'):
        if not isinstance(locator.get(field), str) or not locator[field]:
            raise invalid()
    for field in ('offset', 'parent_length', 'fragment_index', 'fragment_count'):
        if type(locator.get(field)) is not int or locator[field] < 0:
            raise invalid()
    if (locator['offset'] + text_length > locator['parent_length']
            or locator['fragment_index'] >= locator['fragment_count']):
        raise invalid()
    for field in ('continues_before', 'continues_after', 'context_required'):
        if type(locator.get(field)) is not bool:
            raise invalid()
    if (locator['continues_before'] != (locator['offset'] > 0)
            or locator['continues_after'] != (locator['offset'] + text_length < locator['parent_length'])
            or locator.get('split_boundary') not in {'end', 'sentence', 'forced'}):
        raise invalid()
    for field in ('paragraph', 'page', 'table', 'row', 'line_start', 'line_end'):
        if field in locator and (type(locator[field]) is not int or locator[field] < 1):
            raise invalid()
    if locator['kind'] == 'pdf' and 'page' not in locator:
        raise invalid()
    if locator['kind'] in {'text', 'markdown'}:
        if not {'paragraph', 'line_start', 'line_end'}.issubset(locator) or locator['line_start'] > locator['line_end']:
            raise invalid()
    if 'heading_path' in locator:
        if not isinstance(locator['heading_path'], list) or any(not isinstance(item, str) for item in locator['heading_path']):
            raise invalid()


def read_result(path, request_id, result_limit):
    plain_path(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > result_limit:
        raise invalid()
    chunks, total_chars, metadata_bytes, total_bytes = [], 0, 0, 0
    header = None
    ended = False
    try:
        with path.open('rb') as source:
            while line := source.readline(STATUS_LIMIT + 1):
                total_bytes += len(line)
                if ended or len(line) > STATUS_LIMIT or not line.endswith(b'\n') or total_bytes > result_limit:
                    raise invalid()
                frame = decode_frame(line)
                if not isinstance(frame, dict):
                    raise invalid()
                if header is None:
                    if frame.get('type') != 'header' or frame.get('request_id') != request_id:
                        raise invalid()
                    warnings = frame.get('warnings')
                    if not isinstance(warnings, list) or any(not isinstance(item, str) for item in warnings):
                        raise invalid()
                    header = frame
                    metadata_bytes += len(line)
                elif frame.get('type') == 'chunk':
                    chunk = frame.get('chunk')
                    if not isinstance(chunk, dict) or set(chunk) != {'text', 'text_hash', 'clause_number', 'locator'}:
                        raise invalid()
                    text, locator = chunk['text'], chunk['locator']
                    if not isinstance(text, str) or not text or len(text) > 1200 or not isinstance(locator, dict):
                        raise invalid()
                    if chunk['text_hash'] != hashlib.sha256(text.encode('utf-8')).hexdigest():
                        raise invalid('Document text integrity validation failed.')
                    if chunk['clause_number'] is not None and not isinstance(chunk['clause_number'], str):
                        raise invalid()
                    validate_locator(locator, len(text))
                    total_chars += len(text)
                    if total_chars > 20_000_000 or len(chunks) >= 100_000:
                        raise invalid()
                    chunks.append(chunk)
                elif frame.get('type') == 'end':
                    if frame.get('request_id') != request_id or frame.get('chunks') != len(chunks):
                        raise invalid()
                    metadata_bytes += len(line)
                    ended = True
                else:
                    raise invalid()
        if not ended or not chunks or metadata_bytes > STATUS_LIMIT:
            raise invalid()
        return {'chunks': chunks, 'warnings': header['warnings']}
    except (ValueError, UnicodeError, TypeError, RecursionError) as error:
        raise invalid() from error
