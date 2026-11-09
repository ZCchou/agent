"""Native text first, bounded OCR, persistent page cache and visible coverage gaps."""
from __future__ import annotations

import asyncio
from contextlib import closing
import hashlib
import json

from agent.ocr.client import OcrClient
from agent.ocr.store import OcrModelStore
from agent.rag.documents import _chunks
from agent.storage import CoreDataError, _utc_now


class OcrPipeline:
    def __init__(self, repository, *, client=None):
        self.repository = repository
        self.models = OcrModelStore(repository.data_dir / 'ocr-models')
        self.client = client or OcrClient(repository.data_dir)
        self._lock = asyncio.Lock()

    def status(self):
        return {**self.models.list_models(), 'runtime': self.client.available()}

    def _cache_key(self, digest, page, profile_id, device):
        identity = [digest, page, profile_id, device, 'pp-structure-v3-page-v1-200dpi']
        return hashlib.sha256(json.dumps(identity).encode()).hexdigest()

    def _cached(self, key):
        with closing(self.repository.connect()) as connection:
            row = connection.execute('SELECT payload FROM rag_ocr_pages WHERE cache_key=?', (key,)).fetchone()
            return json.loads(row['payload']) if row else None

    def _save_page(self, key, digest, profile_id, page, payload):
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode()) > 8 * 1024 ** 2:
            raise CoreDataError('ocr_result_invalid', 'Page recognition result exceeds limit')
        with closing(self.repository.connect()) as connection, connection:
            connection.execute('INSERT OR REPLACE INTO rag_ocr_pages VALUES (?,?,?,?,?,?)',
                (key, digest, profile_id, page, encoded, _utc_now()))

    def progress(self, job_id, **changes):
        allowed = {'total_pages', 'completed_pages', 'phase', 'latest_version_id', 'original_path', 'original_hash'}
        if not set(changes).issubset(allowed):
            raise ValueError('Invalid OCR progress fields')
        with closing(self.repository.connect()) as connection, connection:
            self.repository.require_running(connection, job_id)
            connection.execute('UPDATE rag_ocr_jobs SET ' + ','.join(key + '=?' for key in changes) + ' WHERE job_id=?',
                (*changes.values(), job_id))

    async def process(self, job, original, digest, options, on_initial=None):
        async with self._lock:
            with closing(self.repository.connect()) as connection, connection:
                self.repository.require_running(connection, job['id'])
                connection.execute('INSERT OR IGNORE INTO rag_ocr_jobs(job_id,options) VALUES (?,?)',
                    (job['id'], json.dumps(options)))
            self.progress(job['id'], phase='page_analysis', original_path=str(original), original_hash=digest)
            if not self.client.available()['available']:
                from agent.rag.documents import parse_document
                parsed = await asyncio.to_thread(parse_document, original)
                parsed['warnings'].append('OCR runtime unavailable. Image regions have not been recognized.')
                parsed['ocr'] = {'coverage': 'partial', 'failed_pages': [], 'runtime_missing': True}
                return parsed
            scan = await self.client.request('scan', original)
            pages = scan['pages']
            if not isinstance(pages, list) or not 1 <= len(pages) <= 10_000:
                raise CoreDataError('ocr_result_invalid', 'Invalid page scan result')
            self.progress(job['id'], total_pages=len(pages), phase='recognizing')
            models = self.models.list_models()['models']
            profile_id = options.get('profile_id') or (models[0]['id'] if models else None)
            page_range = set(options.get('pages', []))
            selected = [page for page in pages if options.get('ocr_mode', 'auto') != 'off'
                and (page['page'] in page_range if page_range else (options.get('ocr_mode') == 'force' or page['needs_ocr']))]
            native = [(page['text'], {'kind': 'pdf', 'page': page['page'], 'extraction_method': 'native',
                'review_status': 'machine'}) for page in pages if page['text'].strip()]
            warnings = []
            failed = []
            blocks = list(native)
            if on_initial and selected and native:
                initial = _chunks(native, ['OCR is running. Image regions are not yet included.'])
                initial['ocr'] = {'coverage': 'partial', 'total_pages': len(pages), 'pending_pages': [p['page'] for p in selected]}
                await on_initial(initial)
            for index, page in enumerate(selected):
                with closing(self.repository.connect()) as connection:
                    self.repository.require_running(connection, job['id'])
                number = page['page']
                if not profile_id:
                    failed.append(number)
                    continue
                key = self._cache_key(digest, number, profile_id, options.get('device', 'auto'))
                try:
                    result = self._cached(key)
                    if result is None:
                        result = await self.client.request('recognize', original, page=number,
                            profile_id=profile_id, device=options.get('device', 'auto'), threads=2)
                        if result.get('page') != number or not isinstance(result.get('regions'), list):
                            raise CoreDataError('ocr_result_invalid', 'OCR page identity mismatch')
                        self._save_page(key, digest, profile_id, number, result)
                    native_text = ' '.join(page['text'].split()).casefold()
                    for region in result['regions']:
                        text = region['text']
                        if region['kind'] in {'header', 'footer', 'number', 'header_image', 'footer_image'}:
                            continue
                        if len(text) >= 20 and ' '.join(text.split()).casefold() in native_text:
                            continue
                        blocks.append((text, {'kind': 'pdf', 'page': number, 'region_id': region['id'],
                            'bbox': region['bbox'], 'extraction_method': 'ocr', 'ocr_profile_id': profile_id,
                            'review_status': region.get('review_status', 'machine'),
                            'orientation': result.get('orientation', 0),
                            'table': 'table' in region}))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    failed.append(number)
                self.progress(job['id'], completed_pages=index + 1)
                self.repository.update_job(job['id'], progress=.1 + .65 * (index + 1) / max(1, len(selected)))
            if failed:
                warnings.append('OCR unavailable or failed on physical pages: ' + ', '.join(map(str, failed)))
            if any(locator.get('extraction_method') == 'ocr' for _, locator in blocks):
                warnings.append('Machine OCR and table interpretation. Check source images when using numerical limits.')
            blocks.sort(key=lambda item: item[1]['page'])
            parsed = _chunks(blocks, warnings)
            parsed['ocr'] = {'coverage': 'partial' if failed else 'complete', 'total_pages': len(pages),
                'ocr_pages': len(selected) - len(failed), 'failed_pages': failed, 'profile_id': profile_id}
            self.progress(job['id'], phase='ready')
            return parsed

    async def close(self):
        await self.client.close()
