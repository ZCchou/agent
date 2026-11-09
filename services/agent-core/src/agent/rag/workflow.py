"""A resumable import batch publishes one final candidate index."""
from contextlib import closing
import asyncio
import hashlib
import json
from pathlib import Path

from agent.rag.repository import identifier
from agent.rag.documents import SPLITTING_VERSION
from agent.storage import CoreDataError, _utc_now


class ImportWorkflow:
    def __init__(self, service):
        self.service = service
        self.repository = service.repository

    def inspect_files(self, library_id, paths):
        result = []
        with closing(self.repository.connect()) as c:
            self.repository.library(c, library_id)
            for name in paths:
                path = Path(name)
                size = path.stat().st_size
                if not path.is_file() or size > 100 * 1024 ** 2:
                    raise CoreDataError('rag_document_too_large', 'Select files no larger than 100 MiB.')
                with path.open('rb') as stream:
                    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                duplicate = c.execute('''SELECT v.id,v.document_id,v.title FROM rag_versions v JOIN rag_documents d ON d.id=v.document_id
                    WHERE d.library_id=? AND v.original_hash=? AND v.status!='withdrawn' AND v.processing_stage='final'
                    ORDER BY v.created_at DESC LIMIT 1''', (library_id, digest)).fetchone()
                previous = c.execute('SELECT id FROM rag_documents WHERE library_id=? AND source_key=?', (library_id, str(path.resolve()))).fetchone()
                result.append({'path': str(path), 'name': path.name, 'size_bytes': size, 'sha256': digest,
                               'duplicate': dict(duplicate) if duplicate else None,
                               'conflict_document_id': previous['id'] if previous and not duplicate else None})
        return {'files': result}

    async def start(self, params):
        library_id = params.get('library_id')
        paths = params.get('paths')
        if not isinstance(paths, list) or not 1 <= len(paths) <= 32 or any(not isinstance(p, str) or not p for p in paths):
            raise CoreDataError('rag_invalid_request', 'Select between 1 and 32 files.')
        key = params.get('request_key')
        if key:
            with closing(self.repository.connect()) as c:
                existing = c.execute('SELECT id FROM rag_jobs WHERE library_id=? AND request_key=?', (library_id, key)).fetchone()
            if existing:
                return {'job': self.repository.job(existing[0]), 'jobs': []}
        options = {k: params[k] for k in ('ocr_mode','device','profile_id','pages','auto_index','accept_partial') if k in params}
        options.setdefault('ocr_mode', 'auto')
        options.setdefault('device', 'auto')
        options.setdefault('auto_index', True)
        project_id = params.get('auto_apply_project_id')
        if project_id:
            with closing(self.repository.connect()) as c:
                self.repository._project(c, project_id)
            options['auto_apply_project_id'] = project_id
        if options['ocr_mode'] not in {'auto','off','force'} or options['device'] not in {'auto','cpu','cuda'}:
            raise CoreDataError('rag_invalid_request', 'Invalid OCR mode or device.')
        if type(options['auto_index']) is not bool or type(options.get('accept_partial', False)) is not bool:
            raise CoreDataError('rag_invalid_request', 'Import flags must be boolean.')
        inspected = await asyncio.to_thread(self.inspect_files, library_id, paths)
        decisions = params.get('decisions', {})
        if not isinstance(decisions, dict):
            raise CoreDataError('rag_invalid_request', 'Document decisions must be an object.')
        entries = []
        for item in inspected['files']:
            decision = decisions.get(item['path'], '')
            if decision not in {'', 'reprocess', 'replace', 'separate'}:
                raise CoreDataError('rag_invalid_request', 'Invalid document version decision.')
            if item['duplicate'] and decision != 'reprocess':
                continue
            if item['conflict_document_id'] and decision not in {'replace','separate'}:
                raise CoreDataError('rag_revision_conflict', 'Choose a replacement revision or an independent document.')
            document_id = item['duplicate']['document_id'] if item['duplicate'] else item['conflict_document_id'] if decision == 'replace' else None
            entries.append({**item, 'document_id': document_id, 'separate': decision == 'separate'})
        if not entries:
            return {'job': None, 'jobs': [], 'duplicates': inspected['files']}
        job = self.repository.new_job('import.batch', library_id, filename=entries[0]['name'] if len(entries) == 1 else f'{len(entries)} files',
                                      payload={'entries': entries, 'options': options}, request_key=key)
        if project_id and library_id not in self.repository.state(project_id)['library_ids']:
            self.repository.bind({'project_id': project_id, 'library_id': library_id, 'enabled': True})
        if job['id'] not in self.service._tasks and job['status'] == 'queued':
            self.service._start(job, self.run(job))
        return {'job': job, 'jobs': [job]}

    async def resume(self, job_id, *, accept_partial=False):
        job = self.repository.job(job_id)
        if job['status'] not in {'interrupted','failed','cancelled'} or job['kind'] != 'import.batch':
            raise CoreDataError('rag_job_active', 'Only a stopped import can be resumed.')
        if job['status'] == 'cancelled':
            new = self.repository.new_job('import.batch', job['library_id'], filename=job['filename'], payload=job['payload'], attempt=job['attempt'] + 1)
            job = new
        else:
            with closing(self.repository.connect()) as c, c:
                payload = job['payload']
                payload['options']['accept_partial'] = bool(accept_partial or payload['options'].get('accept_partial'))
                c.execute("UPDATE rag_jobs SET status='queued',phase='queued',error=NULL,termination_source=NULL,finished_at=NULL,error_code=NULL,resolved_at=NULL,attempt=attempt+1,payload=? WHERE id=?", (json.dumps(payload), job_id))
            job = self.repository.job(job_id)
        self.repository.task_event(job['id'], 'resume', 'user_action')
        self.service._start(job, self.run(job))
        return {'job': job}

    def checkpoint(self, job_id, payload):
        with closing(self.repository.connect()) as c, c:
            self.repository.require_running(c, job_id)
            c.execute('UPDATE rag_jobs SET payload=? WHERE id=?', (json.dumps(payload), job_id))

    async def run(self, job):
        service, repo = self.service, self.repository
        library_id = job['library_id']
        lock = service._index_locks.setdefault(library_id, asyncio.Lock())
        async with lock:
            repo.task_progress(job['id'], 'validating')
            payload = repo.job(job['id'])['payload']
            options = payload['options']
            if options.get('ocr_mode') != 'off' and not options.get('profile_id'):
                profiles = service.ocr.models.list_models()['models']
                if profiles:
                    options['profile_id'] = profiles[0]['id']
            replacements = {}
            for entry in payload['entries']:
                child = repo.new_job('document.import', library_id, parent_id=job['id'], filename=entry['name'])
                repo.update_job(child['id'], status='running')
                try:
                    repo.task_progress(child['id'], 'validating')
                    if entry.get('original'):
                        original = Path(entry['original'])
                        if not original.resolve().is_relative_to((repo.data_dir / 'originals').resolve()):
                            raise CoreDataError('rag_document_invalid', 'Resume original escaped managed storage.')
                        with original.open('rb') as stream:
                            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                    else:
                        from agent.rag.service import DOCUMENT_PARSE_TIMEOUT
                        try:
                            digest, original = await asyncio.wait_for(asyncio.to_thread(service._extract_original, Path(entry['path'])), timeout=DOCUMENT_PARSE_TIMEOUT)
                        except TimeoutError as error:
                            raise CoreDataError('rag_document_timeout', 'Document import exceeded its time limit.') from error
                        entry.update(original=str(original))
                    if digest != entry['sha256']:
                        raise CoreDataError('rag_document_changed', 'The selected file changed. Select it again.')
                    self.checkpoint(job['id'], payload)
                    repo.task_progress(child['id'], 'parsing')
                    source_identity = str(Path(entry['path']).resolve())
                    source_key = 'independent:' + job['id'] + ':' + hashlib.sha256(source_identity.encode()).hexdigest() if entry['separate'] else source_identity
                    async def initial(parsed):
                        version = repo.save_version(library_id, Path(entry['name']).stem, parsed, digest, entry['name'], 'application/pdf', source_key,
                            child['id'], entry['document_id'], complete_job=False, processing_stage='initial')
                        entry['document_id'] = version['version']['document_id']
                        with closing(repo.connect()) as c, c:
                            c.execute('UPDATE rag_jobs SET last_version_id=? WHERE id=?', (version['version']['id'], child['id']))
                        self.checkpoint(job['id'], payload)
                    identity = hashlib.sha256(json.dumps([digest, SPLITTING_VERSION, options.get('profile_id'), options.get('ocr_mode'), options.get('pages')], sort_keys=True).encode()).hexdigest()
                    cache = repo.data_dir / 'processed' / (identity + '.json')
                    if cache.is_file() and entry.get('parsed_digest'):
                        raw = cache.read_bytes()
                        parsed = json.loads(raw) if hashlib.sha256(raw).hexdigest() == entry['parsed_digest'] else None
                    else:
                        parsed = None
                    # A partial parse is retried unless the user explicitly accepts its gaps.
                    if parsed and parsed.get('ocr', {}).get('coverage') == 'partial' and not options.get('accept_partial'):
                        parsed = None
                    if parsed is None:
                        if original.suffix.lower() == '.pdf':
                            parsed = await service.ocr.process(child, original, digest, options, on_initial=initial)
                        else:
                            parsed = await service.parser.parse(original)
                        raw = json.dumps(parsed, ensure_ascii=False).encode('utf-8')
                        if len(raw) > 256 * 1024 ** 2:
                            raise CoreDataError('rag_document_too_large', 'Processed content exceeds the storage limit.')
                        cache.parent.mkdir(exist_ok=True)
                        temporary = cache.with_suffix('.' + child['id'] + '.tmp')
                        temporary.write_bytes(raw)
                        temporary.replace(cache)
                        entry['parsed_digest'] = hashlib.sha256(raw).hexdigest()
                        self.checkpoint(job['id'], payload)
                    repo.task_progress(child['id'], 'validating_content')
                    summary = parsed.get('ocr', {})
                    incomplete = summary.get('coverage') == 'partial' and options.get('ocr_mode') != 'off'
                    ready = not incomplete or options.get('accept_partial', False)
                    version = repo.save_version(library_id, Path(entry['name']).stem, parsed, digest, entry['name'],
                        'application/pdf' if original.suffix == '.pdf' else 'application/octet-stream', source_key,
                        child['id'], entry['document_id'], machine_confirm=ready and options.get('auto_index', True), complete_job=False)
                    entry.update(document_id=version['version']['document_id'], version_id=version['version']['id'])
                    self.checkpoint(job['id'], payload)
                    with closing(repo.connect()) as c, c:
                        c.execute('UPDATE rag_jobs SET last_version_id=? WHERE id=?', (entry['version_id'], child['id']))
                    if incomplete and not options.get('accept_partial'):
                        missing_ocr = summary.get('runtime_missing') or any('ocr_model_missing' in warning for warning in parsed.get('warnings', []))
                        raise CoreDataError('rag_ocr_missing' if missing_ocr else 'rag_needs_attention', 'Some pages need recognition. Retry, inspect pages, or explicitly accept incomplete content.')
                    replacements[entry['document_id']] = entry['version_id']
                    repo.update_job(child['id'], status='completed', progress=1)
                except asyncio.CancelledError:
                    repo.terminate_task(child['id'], repo.job(job['id']).get('termination_source') or 'system_failure', interrupted=True)
                    raise
                except Exception:
                    repo.update_job(child['id'], status='failed', error='Document processing needs attention. See parent task.')
                    raise
            if not options.get('auto_index', True):
                return
            with closing(repo.connect()) as c:
                library = repo.library(c, library_id)
            if not library['embedding_model_id'] or not library['reranker_model_id']:
                raise CoreDataError('rag_models_missing', 'Configure the retrieval and ranking models, then continue this task.')
            index = repo.new_job('index.build', library_id, parent_id=job['id'])
            repo.update_job(index['id'], status='running')
            try:
                await service._build_index(index, repo.adopted_versions(library_id, replacements))
            except Exception:
                repo.update_job(index['id'], status='failed', error='Index build failed. See parent task.')
                raise
            repo.task_progress(job['id'], 'completed', len(replacements), len(replacements))
            repo.resolve_previous_tasks(job['id'])
            repo.task_event(job['id'], 'published')
