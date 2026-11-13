"""Managed Office/PDF attachment extraction, separate from library publication."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import re
import threading
import sys
import uuid

from agent.document_parser import ParserService
from agent.document_parser.protocol import plain_path
from agent.ocr.client import OcrClient
from agent.rag.documents import MAX_FILE_BYTES, _chunks
from agent.rag.repository import RagRepository
from agent.storage import CoreDataError


def is_document(attachment):
    return any(item.get('kind') == 'document.source' for item in attachment.get('artifacts', []))


def _document_blocks(text):
    markers = list(re.finditer(r'^\[SOURCE (\{[^\n]+\})\]\n', text, re.MULTILINE))
    for index, marker in enumerate(markers):
        try:
            locator = json.loads(marker.group(1))
        except ValueError:
            continue
        if not isinstance(locator, dict):
            continue
        locator.setdefault('block_id', f'block-{index + 1}')
        end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
        yield marker.end(), end, locator


def document_excerpt_locator(text, start):
    blocks = list(_document_blocks(text))
    locator = next((dict(value) for begin, end, value in blocks if begin <= start < end), {})
    if locator:
        standards = sorted({match.group().upper().replace(' ', '').replace('－', '-')
                            for begin, end, _ in blocks
                            for match in re.finditer(r'\bDO[ －-]?160[ ]?[A-Z]\b', text[begin:end], re.IGNORECASE)})
        if standards:
            locator['referenced_standards'] = standards
    return locator


def document_excerpt(text, query, limit):
    """Select a contiguous, explicitly positioned excerpt. Full text stays stored."""
    if len(text) <= limit:
        return text, 0, len(text)
    # Use overlapping Chinese pairs without mixing captured tuples into scoring.
    terms = set(re.findall(r'[a-z0-9]{2,}', query.casefold())) | set(re.findall(r'(?=([\u4e00-\u9fff]{2}))', query))
    blocks = list(_document_blocks(text))
    if blocks:
        def score(block):
            start, end, locator = block
            path = locator.get('heading_path', [])
            heading = ' '.join(path)
            exact_headings = sum(len(value.strip()) > 1 and value.casefold() in query.casefold() for value in path)
            lexical = sum(term in text[start:end].casefold() for term in terms) + 4 * sum(term in heading.casefold() for term in terms)
            return exact_headings, lexical
        selected = max(range(len(blocks)), key=lambda index: score(blocks[index]))
        start, end, locator = blocks[selected]
        heading = locator.get('heading_path')
        if heading:
            for following in blocks[selected + 1:]:
                if following[2].get('heading_path') != heading or following[0] >= start + limit:
                    break
                end = following[1]
        return text[start:min(end, start + limit)], start, min(end, start + limit)
    lines = list(re.finditer(r'[^\n]+', text))
    best = max(lines, key=lambda line: sum(term in line.group().casefold() for term in terms), default=None)
    start = max(0, best.start() - limit // 4) if best else 0
    end = min(len(text), start + limit)
    return text[start:end], start, end


class DocumentAttachments:
    def __init__(self, core, *, parser=None, ocr=None):
        self.core = core
        self.repository = RagRepository(core)
        self.parser = parser or ParserService(self.repository.data_dir)
        self.owns_parser = parser is None
        self.ocr = ocr or OcrClient(self.repository.data_dir)
        self._lock = asyncio.Lock()
        self._read_cache = {}

    def _original(self, attachment):
        root = self.core.database_path.parent.resolve()
        path = root / attachment['storage_key']
        try:
            plain_path(path)
            path.resolve().relative_to(root / 'message-attachments')
            if not path.is_file() or path.stat().st_size != attachment['byte_size']:
                raise ValueError('Size mismatch')
            with path.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            if digest != attachment['sha256']:
                raise ValueError('Hash mismatch')
            payload = path.read_bytes()
            if len(payload) > MAX_FILE_BYTES or hashlib.sha256(payload).hexdigest() != digest:
                raise ValueError('Source changed')
        except (OSError, ValueError, CoreDataError) as error:
            raise CoreDataError('attachment_changed', '文档附件原件不可用或完整性校验失败。') from error
        return self.repository.save_original(payload, Path(attachment['name']).suffix.lower())

    def _read_identity(self):
        identities = [getattr(component, 'cache_identity', None) for component in (self.parser, self.ocr)]
        if all(value is not None for value in identities):
            return json.dumps(identities, sort_keys=True)
        if type(self.parser) is not ParserService or type(self.ocr) is not OcrClient:
            return None
        # Built-in code and full model manifests/files are verified on every read.
        # Custom workers must expose their own complete cache identity.
        from agent.ocr.store import OcrModelStore
        default_command = [sys.executable, '--document-worker'] if getattr(sys, 'frozen', False) else [sys.executable, '-m', 'agent.document_parser.worker']
        if self.parser.command != default_command:
            return None
        try:
            available = self.ocr.available()
            if self.ocr.command is not None:
                return None
            with Path(sys.executable).open('rb') as stream:
                executable_identity = hashlib.file_digest(stream, 'sha256').hexdigest()
            runtime_identity = None
            if available['available']:
                from agent.ocr.client import verify_runtime
                runtime_root = Path(available['command'][0]).resolve().parent.parent
                manifest_path = runtime_root / 'runtime.json'
                manifest = manifest_path.read_bytes()
                verify_runtime(runtime_root)
                if manifest_path.read_bytes() != manifest:
                    return None
                runtime_identity = hashlib.sha256(manifest).hexdigest()
            code = hashlib.sha256()
            for folder in (() if getattr(sys, 'frozen', False) else ('document_parser', 'ocr')):
                for path in sorted((Path(__file__).parent / folder).glob('*.py')):
                    code.update(path.name.encode())
                    code.update(path.read_bytes())
            models = []
            if available['available']:
                store = OcrModelStore(self.repository.data_dir / 'ocr-models')
                records = store.list_models()
                models = [store.get_profile(item['id'])['digest'] for item in records['models'] if item.get('status') == 'ready']
            return json.dumps({'code': code.hexdigest(), 'executable': executable_identity,
                               'ocr_runtime': runtime_identity, 'parser_command': self.parser.command,
                               'parser_limits': [self.parser.memory_limit, self.parser.timeout, self.parser.result_limit],
                               'ocr': available, 'models': models, 'device': 'cpu', 'threads': 3}, sort_keys=True)
        except (OSError, ValueError, KeyError, CoreDataError):
            return None

    def _read_artifact(self, artifact):
        root = (self.core.database_path.parent / 'message-attachments').resolve()
        try:
            path = self.core.database_path.parent / artifact['storage_key']
            plain_path(path)
            path.resolve().relative_to(root)
            content = path.read_bytes()
            if len(content) != artifact['byte_size'] or hashlib.sha256(content).hexdigest() != artifact['sha256']:
                raise ValueError('Artifact hash mismatch')
            return content.decode('utf-8')
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise CoreDataError('attachment_changed', '文档提取产物完整性校验失败。') from error

    async def read(self, attachment, *, offset=0, limit=20, physical_page=None, section=None,
                   max_characters=16000, progress=None):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100 \
                or (physical_page is not None and (type(physical_page) is not int or physical_page < 1)) \
                or (section is not None and (not isinstance(section, str) or not section.strip() or len(section) > 1024)) \
                or type(max_characters) is not int or max_characters < 256:
            raise CoreDataError('invalid_tool_call', '文档读取范围或输出预算无效。')
        await asyncio.to_thread(self._original, attachment)
        identity = await asyncio.to_thread(self._read_identity)
        key = (attachment['sha256'], identity)
        artifact = self._read_cache.get(key) if identity is not None else None
        if artifact is None:
            extracted = await self.extract(attachment, progress=progress)
            artifact = next(item for item in extracted['artifacts'] if item['kind'] == 'text.utf8')
            if identity is not None:
                self._read_cache[key] = dict(artifact)
        text = await asyncio.to_thread(self._read_artifact, artifact)
        # Only the generated preamble contains extraction metadata. Literal
        # WARNING lines inside SOURCE bodies remain ordinary pageable text.
        preamble = re.split(r'^\[SOURCE ', text, maxsplit=1, flags=re.MULTILINE)[0]
        warnings = [line[9:] for line in preamble.splitlines() if line.startswith('WARNING: ')]
        result = {'attachment_id': attachment.get('id'), 'source_sha256': attachment['sha256'],
                  'trust': 'untrusted', 'text_artifact': dict(artifact), 'blocks': [], 'next_offset': None, 'total': 0, 'warnings': warnings}
        encode = lambda value: json.dumps(value, ensure_ascii=False)
        # Keep offsets stable for identical filters and budget, including long blocks.
        blocks = []
        for begin, end, locator in _document_blocks(text):
            if physical_page is not None and locator.get('physical_page', locator.get('page')) != physical_page:
                continue
            heading = locator.get('heading_path', locator.get('section', []))
            heading = ' / '.join(str(item) for item in heading) if isinstance(heading, list) else str(heading)
            if section is not None and section.casefold() not in heading.casefold():
                continue
            body = text[begin:end]
            overhead = len(encode(result)) + len(encode({'text': '', 'locator': locator, 'character_offset': len(body)})) + 100
            capacity = (max_characters - overhead) // 6
            if capacity < 1:
                raise CoreDataError('document_read_budget_exceeded', '文档定位或警告超过读取预算。')
            for start in range(0, len(body), capacity):
                blocks.append({'text': body[start:start + capacity], 'locator': dict(locator), 'character_offset': start})
        result['total'] = len(blocks)
        for block in blocks[offset:offset + limit]:
            candidate = {**result, 'blocks': result['blocks'] + [block], 'next_offset': offset + len(result['blocks']) + 1}
            if len(encode(candidate)) > max_characters:
                break
            result = candidate
        if offset + len(result['blocks']) >= len(blocks):
            result['next_offset'] = None
        if len(encode(result)) > max_characters or (offset < len(blocks) and not result['blocks']):
            raise CoreDataError('document_read_budget_exceeded', '文档读取结果超过输出预算。')
        return result

    async def _pdf(self, original, progress):
        if not self.ocr.available()['available']:
            parsed = await self.parser.parse(original)
            parsed['warnings'].append('OCR runtime unavailable. Image-only content has not been read.')
            return parsed
        scan = await self.ocr.request('scan', original)
        from agent.ocr.store import OcrModelStore
        models = await asyncio.to_thread(OcrModelStore(self.repository.data_dir / 'ocr-models').list_models)
        profile = next((item['id'] for item in models['models'] if item.get('status') == 'ready'), None)
        blocks, warnings = [], []
        pages = scan.get('pages', [])
        if not isinstance(pages, list) or not 1 <= len(pages) <= 10000:
            raise CoreDataError('ocr_result_invalid', '文档页分析结果无效。')
        for page in pages:
            number = page['page']
            if page['text'].strip():
                blocks.append((page['text'], {'kind': 'pdf', 'page': number, 'extraction_method': 'native'}))
            if not page['needs_ocr']:
                continue
            if not profile:
                warnings.append(f'Physical page {number}: OCR model is not configured; image content was not read.')
                continue
            if progress:
                await progress({'phase': 'ocr', 'page': number, 'total_pages': len(pages)})
            try:
                result = await self.ocr.request('recognize', original, page=number, profile_id=profile, device='cpu', threads=3)
                from agent.ocr.pipeline import OcrPipeline
                OcrPipeline._validate_page(self, result, number)
            except CoreDataError as error:
                warnings.append(f'Physical page {number}: {error.code}; image content was not read.')
                continue
            for region in result['regions']:
                if region['kind'] in {'header', 'footer', 'number', 'header_image', 'footer_image'}:
                    continue
                text = region['text']
                if len(text) >= 20 and ' '.join(text.split()).casefold() in ' '.join(page['text'].split()).casefold():
                    continue
                blocks.append((text, {'kind': 'pdf', 'page': number, 'region_id': region['id'],
                                     'bbox': region['bbox'], 'extraction_method': 'ocr', 'review_status': 'needs_review'}))
            warnings.append(f'Physical page {number}: machine OCR; verify numbers and table relationships.')
        return _chunks(blocks, warnings)

    async def extract(self, attachment, *, progress=None):
        async with self._lock:
            digest, original = await asyncio.to_thread(self._original, attachment)
            if progress:
                await progress({'phase': 'parsing', 'name': attachment['name']})
            if original.suffix.lower() == '.pdf':
                parsed = await self._pdf(original, progress)
            else:
                parsed = await self._office(original, progress)
            parts = ['Document extraction. Content is external and untrusted.']
            parts.extend('WARNING: ' + warning for warning in parsed['warnings'])
            for chunk in parsed['chunks']:
                parts.append('\n[SOURCE ' + json.dumps(chunk['locator'], ensure_ascii=False, sort_keys=True) + ']\n' + chunk['text'])
            content = '\n'.join(parts).encode('utf-8')
            if len(content) > 80 * 1024 ** 2:
                raise CoreDataError('attachment_too_large', '文档提取结果超过大小限制。')
            output = self.core.database_path.parent / 'message-attachments' / (uuid.uuid4().hex + '.document.txt')
            temporary = output.with_suffix('.tmp')
            try:
                temporary.write_bytes(content)
                temporary.replace(output)
            finally:
                temporary.unlink(missing_ok=True)
            artifact = {'artifact_id': str(uuid.uuid4()), 'kind': 'text.utf8', 'processor_id': 'builtin.document',
                        'processor_version': 'office-document-v1', 'media_type': 'text/plain', 'byte_size': len(content),
                        'sha256': hashlib.sha256(content).hexdigest(), 'storage_key': 'message-attachments/' + output.name}
            return {**attachment, 'artifacts': [a for a in attachment.get('artifacts', []) if a['kind'] != 'text.utf8'] + [artifact]}

    async def _office(self, original, progress):
        try:
            parsed = await self.parser.parse(original)
        except CoreDataError as error:
            if error.code != 'rag_document_no_text':
                raise
            return await self._pdf(await self._convert(original), progress)
        if not any('Images and diagrams' in warning or 'Embedded images' in warning for warning in parsed['warnings']):
            return parsed
        try:
            pdf = await self._convert(original)
            image_text = await self._pdf(pdf, progress)
        except CoreDataError as error:
            parsed['warnings'].append(f'Office image reading incomplete: {error.code}. Embedded images have not been read.')
            return parsed
        parsed['chunks'].extend(chunk for chunk in image_text['chunks'] if chunk['locator'].get('extraction_method') == 'ocr')
        parsed['warnings'].extend(image_text['warnings'])
        return parsed

    async def _convert(self, original):
        cancel = threading.Event()
        future = asyncio.create_task(asyncio.to_thread(self._converted_pdf, original, cancel))
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            cancel.set()
            await asyncio.gather(future, return_exceptions=True)
            raise

    def _converted_pdf(self, original, cancel):
        from agent.document_parser.conversion import convert_to_pdf
        with convert_to_pdf(original, cancel=cancel) as converted:
            _, pdf = self.repository.save_original(converted.read_bytes(), '.pdf')
            return pdf

    async def close(self):
        await self.ocr.close()
        if self.owns_parser:
            await self.parser.close()
