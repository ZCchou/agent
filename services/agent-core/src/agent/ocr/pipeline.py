"""Native text first, bounded OCR, persistent page cache and visible coverage gaps."""
from __future__ import annotations

import asyncio
from contextlib import closing
import hashlib
import json
import os
import math

from agent.ocr.client import OcrClient
from agent.ocr.store import OcrModelStore
from agent.rag.documents import _chunks
from agent.storage import CoreDataError, _utc_now


class OcrPipeline:
    def __init__(self, repository, *, client=None, parser=None):
        self.repository = repository
        self.parser = parser
        self.models = OcrModelStore(repository.data_dir / 'ocr-models')
        self.client = client or OcrClient(repository.data_dir)
        self.preview_client = OcrClient(repository.data_dir)
        self.clients = [self.client]
        self._lock = asyncio.Lock()

    def status(self):
        return {**self.models.list_models(), 'runtime': self.client.available()}

    def _cache_key(self, digest, page, profile_id, device):
        identity = [digest, page, profile_id, device, 'pp-structure-v3-page-v2-200dpi-page-ocr']
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
        phase = changes.get('phase')
        if phase == 'page_analysis':
            self.repository.task_progress(job_id, 'parsing')
        elif phase == 'recognizing':
            self.repository.task_progress(job_id, 'ocr')
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
                if self.parser is None:
                    raise CoreDataError('ocr_runtime_missing', 'OCR and managed parser are unavailable')
                parsed = await self.parser.parse(original)
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
            selected_numbers = {page['page'] for page in selected}
            pending_pages = [page['page'] for page in pages if page['needs_ocr'] and page['page'] not in selected_numbers]
            native = [(page['text'], {'kind': 'pdf', 'page': page['page'], 'extraction_method': 'native',
                'review_status': 'machine'}) for page in pages if page['text'].strip()]
            warnings = []
            failed = []
            blocks = list(native)
            cache_keys = []
            if on_initial and selected and native:
                initial = _chunks(native, ['OCR is running. Image regions are not yet included.'])
                initial['ocr'] = {'coverage': 'partial', 'total_pages': len(pages), 'pending_pages': [p['page'] for p in selected]}
                await on_initial(initial)
            # Each worker owns one loaded pipeline. Bound concurrency by free RAM,
            # physical CPU cores, and the selected runtime's device support.
            if len(self.clients) == 1 and isinstance(self.client, OcrClient) and len(selected) > 1:
                from agent.gpu_resources import system_memory
                cpu_bundle = False
                if profile_id:
                    profile = await asyncio.to_thread(self.models.get_profile, profile_id)
                    cpu_bundle = 'paddlepaddle' in profile['manifest'].get('runtime', {})
                if options.get('device', 'auto') != 'cuda' and cpu_bundle and system_memory().get('available_physical_bytes', 0) >= 12 * 1024 ** 3 and (os.cpu_count() or 1) >= 8:
                    self.clients.append(OcrClient(self.repository.data_dir))
            self.repository.task_progress(job['id'], 'ocr', 0, len(selected))
            queue = asyncio.Queue()
            for page in selected:
                queue.put_nowait(page)
            completed = 0
            async def recognize_pages(client):
                nonlocal completed
                while not queue.empty():
                    page = queue.get_nowait()
                    with closing(self.repository.connect()) as connection:
                        self.repository.require_running(connection, job['id'])
                    number = page['page']
                    key = self._cache_key(digest, number, profile_id, options.get('device', 'auto'))
                    try:
                        if not profile_id:
                            raise CoreDataError('ocr_model_missing', 'Import an OCR model package')
                        result = self._cached(key)
                        if result is None:
                            result = await client.request('recognize', original, page=number,
                                profile_id=profile_id, device=options.get('device', 'auto'), threads=4)
                            self._validate_page(result, number)
                            self._save_page(key, digest, profile_id, number, result)
                        self._validate_page(result, number)
                        cache_keys.append(key)
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
                                'orientation': result.get('orientation', 0), 'table': 'table' in region}))
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        failed.append(number)
                        warnings.append(f'Physical page {number}: {getattr(error, "code", "ocr_page_failed")}')
                    completed += 1
                    self.progress(job['id'], completed_pages=completed)
                    self.repository.task_progress(job['id'], 'ocr', completed, len(selected))
                    self.repository.update_job(job['id'], progress=.1 + .65 * completed / max(1, len(selected)))
            workers = self.clients[:1] if options.get('device') == 'cuda' else self.clients
            tasks = [asyncio.create_task(recognize_pages(client)) for client in workers]
            try:
                await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            if pending_pages:
                warnings.append('Image regions not recognized on physical pages: ' + ', '.join(map(str, pending_pages)))
            if failed:
                warnings.append('OCR unavailable or failed on physical pages: ' + ', '.join(map(str, failed)))
            if any(locator.get('extraction_method') == 'ocr' for _, locator in blocks):
                warnings.append('Machine OCR and table interpretation. Check source images when using numerical limits.')
            blocks.sort(key=lambda item: item[1]['page'])
            parsed = _chunks(blocks, warnings)
            parsed['ocr'] = {'coverage': 'partial' if failed or pending_pages else 'complete', 'total_pages': len(pages),
                'pending_pages': pending_pages, 'ocr_pages': len(selected) - len(failed), 'failed_pages': failed, 'profile_id': profile_id, 'cache_keys': sorted(cache_keys), 'workers': len(workers)}
            self.progress(job['id'], phase='ready')
            return parsed

    def _validate_page(self, result, number):
        if result.get('page') != number or not isinstance(result.get('regions'), list) or len(result['regions']) > 10_000:
            raise CoreDataError('ocr_result_invalid', 'OCR page identity or region count is invalid')
        for region in result['regions']:
            bbox = region.get('bbox')
            if not isinstance(region.get('text'), str) or len(region['text']) > 2_000_000 or not isinstance(region.get('id'), str) or not isinstance(region.get('kind'), str):
                raise CoreDataError('ocr_result_invalid', 'Invalid OCR region content')
            if not isinstance(bbox, list) or len(bbox) != 4 or any(type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1 for value in bbox):
                raise CoreDataError('ocr_result_invalid', 'Invalid normalized OCR coordinates')
            if bbox[0] > bbox[2] or bbox[1] > bbox[3]:
                raise CoreDataError('ocr_result_invalid', 'Invalid OCR coordinate order')

    async def close(self):
        await asyncio.gather(*(client.close() for client in self.clients), self.preview_client.close())
