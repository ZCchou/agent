"""Managed Office/PDF attachment extraction, separate from library publication."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import re
import threading
import uuid

from agent.document_parser import ParserService
from agent.document_parser.protocol import plain_path
from agent.ocr.client import OcrClient
from agent.rag.documents import MAX_FILE_BYTES, _chunks
from agent.rag.repository import RagRepository
from agent.storage import CoreDataError


def is_document(attachment):
    return any(item.get('kind') == 'document.source' for item in attachment.get('artifacts', []))


def document_excerpt(text, query, limit):
    """Select a contiguous, explicitly positioned excerpt. Full text stays stored."""
    if len(text) <= limit:
        return text, 0, len(text)
    terms = set(re.findall(r'[a-z0-9]{2,}|[\u4e00-\u9fff]{2}', query.casefold()))
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
