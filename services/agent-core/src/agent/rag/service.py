"""Local RAG application boundary, cancellable jobs and project-scoped evidence."""
from __future__ import annotations

import asyncio
from contextlib import closing
import inspect
import json
import math
from pathlib import Path
import sqlite3
import tempfile

from agent.document_parser import ParserService
from agent.rag.documents import MAX_FILE_BYTES, MAX_TEXT_CHARS, SUPPORTED_SUFFIXES, SPLITTING_VERSION, has_readable_text
from agent.rag.repository import RagRepository, identifier, required_text
from agent.rag.retrieval import candidates, load_vector_extension, vector_blob
from agent.storage import CoreDataError, _utc_now

DOCUMENT_PARSE_TIMEOUT = 125
MEDIA_TYPES = {'.pdf': 'application/pdf', '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
               '.txt': 'text/plain', '.md': 'text/markdown', '.markdown': 'text/markdown'}
LEGACY_SPLITTING_WARNING = 'Reimport the original documents and rebuild their indexes to refresh section and clause metadata from the updated parser.'


class RagService:
    def __init__(self, repository, models=None, *, parser=None):
        self.repository = RagRepository(repository)
        if models is None:
            from agent.rag.models import ModelStore
            models = ModelStore(self.repository.data_dir)
        self.models = models
        self.parser = parser if parser is not None else ParserService(self.repository.data_dir)
        self._tasks = {}
        self._closed = False
        from agent.ocr.pipeline import OcrPipeline
        self.ocr = OcrPipeline(self.repository, parser=self.parser)
        self._index_locks = {}
        from agent.rag.workflow import ImportWorkflow
        self.workflow = ImportWorkflow(self)

    def _model(self, model_id, role=None, digest=None):
        records = self.models.list_models()
        if isinstance(records, dict):
            records = records.get('models', [])
        model = next((item for item in records if item['id'] == model_id), None)
        if model is None:
            raise CoreDataError('rag_model_not_found', 'The selected auxiliary model is unavailable.')
        if role and model.get('role') != role:
            raise CoreDataError('rag_model_role_mismatch', 'The auxiliary model role is incompatible.')
        if digest is not None and model.get('digest') != digest:
            raise CoreDataError('rag_model_changed', 'The auxiliary model fingerprint differs from the index.')
        return model

    async def _model_action(self, action, params):
        if action == 'model.list':
            def model_status():
                records = self.models.list_models()
                if isinstance(records, dict):
                    return records
                return {'models': records, 'root': str(getattr(self.models, 'root', '')),
                        'runtime': self.models.runtime_status() if hasattr(self.models, 'runtime_status') else {}}
            return await asyncio.to_thread(model_status)
        methods = {
            'model.inspect': ('inspect', (params.get('path'),), {'profile': params.get('profile')}),
            'model.import': ('import_model', (params.get('path'), params.get('profile')), {'device': params.get('device', 'cpu')}),
            'model.root': ('set_root', (params.get('path'),), {}),
            'model.device': ('set_device', (params.get('model_id'), params.get('device')), {}),
            'model.remove': ('remove', (params.get('model_id'),), {}),
            'model.probe': ('probe', (params.get('model_id'),), {}),
        }
        if action not in methods:
            raise CoreDataError('rag_action_unsupported', 'The RAG action is unsupported.')
        if action == 'model.remove':
            with closing(self.repository.connect()) as connection:
                used = connection.execute('''SELECT 1 FROM rag_libraries WHERE archived_at IS NULL AND
                    (embedding_model_id=? OR reranker_model_id=?) UNION ALL SELECT 1 FROM rag_indexes i
                    JOIN rag_libraries l ON l.active_index_id=i.id AND l.archived_at IS NULL WHERE
                    i.embedding_model_id=? OR i.reranker_model_id=? UNION ALL SELECT 1 FROM rag_indexes i
                    JOIN rag_bindings b ON b.index_id=i.id JOIN rag_libraries l ON l.id=b.library_id
                    WHERE l.archived_at IS NULL AND (i.embedding_model_id=? OR i.reranker_model_id=?)
                    LIMIT 1''', (params.get('model_id'),) * 6).fetchone()
                if used:
                    raise CoreDataError('rag_model_in_use', 'A library or active index still uses this model.')
        method, args, kwargs = methods[action]
        target = getattr(self.models, method)
        if inspect.iscoroutinefunction(target):
            result = await target(*args, **kwargs)
        else:
            result = await asyncio.to_thread(target, *args, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result if isinstance(result, dict) else {'result': result}

    async def handle(self, action, params):
        if self._closed:
            raise CoreDataError('rag_closed', 'The RAG service is closed.')
        if not isinstance(params, dict):
            raise CoreDataError('rag_invalid_request', 'RAG parameters must be an object.')
        if action == 'ocr.model.list':
            return await asyncio.to_thread(self.ocr.status)
        if action == 'ocr.model.inspect':
            return await asyncio.to_thread(self.ocr.models.inspect, params.get('path'))
        if action == 'ocr.model.import':
            return await asyncio.to_thread(self.ocr.models.import_bundle, params.get('path'))
        if action == 'document.ocr.pages':
            return self.repository.ocr_pages(params.get('version_id'), params.get('page'))
        if action == 'document.ocr.render':
            path = self.repository.original_for_version(params.get('version_id'))
            result = await self.ocr.preview_client.request('render', path, page=params.get('page'))
            self.repository.original_for_version(params.get('version_id'))
            return result
        if action.startswith('model.'):
            return await self._model_action(action, params)
        if action == 'library.overview':
            return self.repository.overview(params.get('project_id'))
        if action == 'document.list':
            return self.repository.documents(params.get('library_id'), params.get('offset', 0), params.get('limit', 50), params.get('query', ''), params.get('include_withdrawn', False))
        if action == 'document.history':
            return self.repository.version_history(params.get('document_id'))
        if action == 'document.inspect':
            return await asyncio.to_thread(self.workflow.inspect_files, params.get('library_id'), params.get('paths'))
        if action == 'job.detail':
            return self.repository.task_detail(params.get('job_id'))
        if action == 'job.resume':
            return await self.workflow.resume(params.get('job_id'), accept_partial=params.get('accept_partial', False))
        if action == 'library.search':
            return await self.search_library(params.get('library_id'), params.get('query'))
        if action == 'document.metadata':
            return await self.update_metadata(params)
        if action == 'document.original':
            return {'path': str(self.repository.original_for_version(params.get('version_id')))}
        if action == 'library.list':
            return {'libraries': self.repository.list_libraries(params.get('project_id'))}
        if action == 'library.create':
            params = dict(params)
            available = self.models.list_models()
            if isinstance(available, dict):
                available = available.get('models', [])
            for role, key in [('embedding', 'embedding_model_id'), ('reranker', 'reranker_model_id')]:
                if not params.get(key):
                    params[key] = next((m['id'] for m in available if m['role'] == role and m.get('status') == 'ready'), '')
                if params[key]:
                    self._model(params[key], role)
            return {'library': self.repository.create_library(params)}
        if action == 'library.update':
            if 'embedding_model_id' in params:
                self._model(params['embedding_model_id'], 'embedding')
            if 'reranker_model_id' in params:
                self._model(params['reranker_model_id'], 'reranker')
            return {'library': self.repository.update_library(params)}
        if action == 'library.bind':
            return self.repository.bind(params)
        if action == 'library.remove':
            result = self.repository.remove_library(params.get('library_id'))
            for job in self.repository.jobs(params.get('library_id')):
                self.repository.terminate_task(job['id'], 'library_removal')
                task = self._tasks.get(job['id'])
                if task:
                    task.cancel()
            return result
        if action == 'document.paste':
            title = required_text(params.get('title'), 'Document title')
            content = params.get('content')
            if not isinstance(content, str):
                raise CoreDataError('rag_invalid_document', 'Pasted content must be text.')
            if len(content) > MAX_TEXT_CHARS:
                raise CoreDataError('rag_document_too_large', 'The text document exceeds its character limit.')
            digest, original = await asyncio.to_thread(self.repository.save_original, content.encode('utf-8'), '.txt')
            parsed = await self.parser.parse(original)
            if self._closed:
                raise CoreDataError('rag_closed', 'The RAG service closed before the document was saved.')
            if asyncio.current_task().cancelling():
                raise asyncio.CancelledError
            return self.repository.save_version(params.get('library_id'), title, parsed, digest, title + '.txt', 'text/plain', document_id=params.get('document_id'))
        if action == 'document.import':
            return await self.workflow.start(params)
        if action == 'document.preview':
            return self.repository.preview(params.get('version_id'), params.get('offset', 0), params.get('limit', 200), params.get('page'))
        if action == 'document.confirm':
            return self.repository.confirm(params)
        if action == 'document.withdraw':
            preview = self.repository.preview(params.get('version_id'), limit=1)
            result = self.repository.withdraw(params.get('version_id'))
            job_id = preview['version']['ocr_summary'].get('job_id')
            if job_id:
                job = self.repository.job(job_id)
                job_id = job.get('parent_id') or job_id
                self.repository.terminate_task(job_id, 'document_withdrawal')
            task = self._tasks.get(job_id)
            if task:
                task.cancel()
            return result
        if action == 'job.list':
            return {'jobs': self.repository.jobs(params.get('library_id'))}
        if action in {'job.cancel','job.pause'}:
            job_id = params.get('job_id')
            self.repository.job(job_id)
            self.repository.terminate_task(job_id, 'user_action' if action == 'job.cancel' else 'user_pause', interrupted=action == 'job.pause')
            task = self._tasks.get(job_id)
            if task:
                task.cancel()
            return {'job': self.repository.job(job_id)}
        if action == 'index.build':
            self._require_index_idle(params.get('library_id'))
            versions = self.repository.index_versions(params.get('library_id'), params.get('version_ids'))
            job = self.repository.new_job('index.build', params.get('library_id'), payload={'version_ids': versions})
            self._start(job, self._manual_index(job, versions))
            return {'job': job}
        if action == 'search':
            return await self.search(params.get('project_id'), params.get('query'), params.get('run_id'), params.get('library_ids'), params.get('version_ids'), params.get('limit', 8))
        if action == 'evidence.preview':
            return self.read_evidence(params.get('project_id'), params.get('evidence_id'), params.get('offset', 0), params.get('length', 8000))
        raise CoreDataError('rag_action_unsupported', 'The RAG action is unsupported.')

    def _start(self, job, coroutine):
        task = asyncio.create_task(self._run_job(job, coroutine), name='rag-' + job['id'])
        self._tasks[job['id']] = task
        def finished(completed):
            coroutine.close()
            self._tasks.pop(job['id'], None)
        task.add_done_callback(finished)

    async def _run_job(self, job, coroutine):
        try:
            self.repository.update_job(job['id'], status='running')
            await coroutine
            self.repository.update_job(job['id'], status='completed', progress=1)
        except asyncio.CancelledError:
            current = self.repository.job(job['id'])
            if current['status'] in {'queued','running'}:
                self.repository.terminate_task(job['id'], 'system_failure', interrupted=True)
        except Exception as error:
            if self.repository.job(job['id'])['status'] not in {'queued','running'}:
                return
            attention = getattr(error, 'code', '') in {'rag_needs_attention','rag_models_missing','rag_ocr_missing'}
            self.repository.update_job(job['id'], status='interrupted' if attention else 'failed', error=str(error)[:2000])
            with closing(self.repository.connect()) as c, c:
                c.execute('UPDATE rag_jobs SET phase=?,termination_source=?,error_code=? WHERE id=?',
                          ('needs_attention' if attention else 'failed', 'system_failure', getattr(error, 'code', 'rag_operation_failed'), job['id']))
            self.repository.task_event(job['id'], 'needs_attention' if attention else 'failed', 'system_failure')
        finally:
            coroutine.close()

    def _extract_original(self, path):
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            raise CoreDataError('rag_document_unsupported', 'Supported documents are Office, WPS, PDF, UTF-8 TXT and Markdown.')
        with path.open('rb') as source:
            content = source.read(MAX_FILE_BYTES + 1)
        if len(content) > MAX_FILE_BYTES:
            raise CoreDataError('rag_document_too_large', 'The document exceeds 100 MiB.')
        digest, original = self.repository.save_original(content, path.suffix.lower())
        return digest, original

    async def _build_index(self, job, version_ids):
        library_id = job['library_id']
        version_ids = self.repository.index_versions(library_id, version_ids)
        with closing(self.repository.connect()) as connection:
            library = self.repository.library(connection, library_id)
            chunks = connection.execute('''SELECT c.id,c.text,c.version_id,c.locator FROM rag_chunks c
                JOIN rag_versions v ON v.id=c.version_id JOIN rag_documents d ON d.id=v.document_id
                WHERE d.library_id=? AND v.status='confirmed' AND v.id IN (SELECT value FROM json_each(?))
                ORDER BY c.version_id,c.ordinal''', (library_id, json.dumps(version_ids))).fetchall()
        chunks = [row for row in chunks if has_readable_text(row['text'])]
        if not chunks:
            raise CoreDataError('rag_no_confirmed_documents', 'Confirm a readable document before building its index.')
        embedding = self._model(library['embedding_model_id'], 'embedding')
        reranker = self._model(library['reranker_model_id'], 'reranker')
        dimension = embedding.get('dimension')
        if not isinstance(dimension, int) or dimension <= 0 or dimension > 65536:
            raise CoreDataError('rag_dimension_unknown', 'Probe the embedding model to establish its dimension.')
        versions = sorted({row['version_id'] for row in chunks})
        splitter_versions = sorted({json.loads(row['locator']).get('splitting_version', 'fixed-1200-v1') for row in chunks})
        index_id = identifier()
        # A private staging database bounds vector memory and disappears on failure.
        with tempfile.TemporaryDirectory(prefix='index-', dir=self.repository.data_dir) as temporary:
            staging_path = Path(temporary) / 'vectors.db'
            with closing(sqlite3.connect(staging_path)) as staging:
                staging.execute('CREATE TABLE vectors(chunk_id TEXT PRIMARY KEY,embedding BLOB NOT NULL)')
                self.repository.task_progress(job['id'], 'indexing', 0, len(chunks))
                for offset in range(0, len(chunks), 32):
                    batch = chunks[offset:offset + 32]
                    vectors = await self.models.embed(embedding['id'], [row['text'] for row in batch])
                    if len(vectors) != len(batch):
                        raise CoreDataError('rag_invalid_embedding', 'The embedding batch size is incorrect.')
                    with closing(self.repository.connect()) as connection:
                        self.repository.require_running(connection, job['id'])
                    staging.executemany('INSERT INTO vectors(chunk_id,embedding) VALUES (?,?)',
                                        [(row['id'], vector_blob(vector, dimension)) for row, vector in zip(batch, vectors)])
                    staging.commit()
                    self.repository.update_job(job['id'], progress=min(.95, (offset + len(batch)) / len(chunks) * .95))
                    self.repository.task_progress(job['id'], 'indexing', offset + len(batch), len(chunks))
                self.repository.task_progress(job['id'], 'publishing')
                with closing(self.repository.connect()) as connection, connection:
                    # Verify the extension before publishing a searchable index.
                    load_vector_extension(connection)
                    connection.execute('BEGIN IMMEDIATE')
                    self.repository.require_running(connection, job['id'])
                    current = self.repository.library(connection, library_id)
                    if any(current[field] != library[field] for field in ('embedding_model_id', 'reranker_model_id', 'generation')):
                        raise CoreDataError('rag_index_stale', 'The library changed during index construction. Build again.')
                    self._model(embedding['id'], 'embedding', embedding['digest'])
                    self._model(reranker['id'], 'reranker', reranker['digest'])
                    connection.execute('''INSERT INTO rag_indexes(id,library_id,embedding_model_id,embedding_digest,
                        reranker_model_id,reranker_digest,dimension,version_ids,splitter_versions,chunk_count,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                        (index_id, library_id, embedding['id'], embedding['digest'], reranker['id'], reranker['digest'], dimension,
                         json.dumps(versions), json.dumps(splitter_versions), len(chunks), _utc_now()))
                    connection.executemany('INSERT INTO rag_index_chunks(index_id,chunk_id,embedding) VALUES (?,?,?)',
                                           ((index_id, row[0], row[1]) for row in staging.execute('SELECT chunk_id,embedding FROM vectors')))
                    self.repository.publish_index(connection, library_id, index_id)
                    connection.execute("UPDATE rag_jobs SET status='completed',progress=1,updated_at=? WHERE id=?", (_utc_now(), job['id']))

    def project_state(self, project_id):
        return self.repository.state(project_id)

    def assert_generation(self, project_id, generation):
        state = self.project_state(project_id)
        if state['generation'] != generation:
            raise CoreDataError('rag_generation_changed', 'The project evidence configuration changed during this run.')
        return state

    def validate_evidence(self, project_id, evidence_ids, run_id=None):
        if not isinstance(evidence_ids, list) or not evidence_ids or any(not isinstance(value, str) for value in evidence_ids):
            raise CoreDataError('rag_evidence_invalid', 'At least one evidence identifier is required.')
        with closing(self.repository.connect()) as connection:
            return [self.repository.checked_evidence(project_id, evidence_id, run_id, connection) for evidence_id in evidence_ids]

    def read_evidence(self, project_id, evidence_id, offset=0, length=8000):
        return self.repository.read_evidence_context(project_id, evidence_id, offset, length)

    def reauthorize_evidence(self, project_id, evidence_ids, *, run_id):
        """Mint fresh run identities only while all original sources remain authorized."""
        if self._closed:
            raise CoreDataError('rag_closed', 'The RAG service is closed.')
        if not isinstance(evidence_ids, list) or not 1 <= len(evidence_ids) <= 8 or any(not isinstance(value, str) for value in evidence_ids):
            raise CoreDataError('rag_evidence_invalid', 'Select between one and eight evidence identifiers.')
        search_id = identifier()
        with closing(self.repository.connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            state = self.repository.state(project_id, connection)
            sources = [self.repository.checked_evidence(project_id, key, connection=connection)
                       for key in dict.fromkeys(evidence_ids)]
            if any(not has_readable_text(item['text']) for item in sources):
                raise CoreDataError('rag_evidence_invalid', 'The previous evidence has no readable text. Search again.')
            connection.execute('''INSERT INTO rag_searches(id,project_id,run_id,query,generation,index_ids,created_at)
                VALUES (?,?,?,?,?,?,?)''', (search_id, project_id, run_id, '', state['generation'], json.dumps(state['index_ids']), _utc_now()))
            evidence, aliases = [], {}
            for source in sources:
                record = {**source, 'id': 'rag-' + identifier(), 'run_id': run_id, 'search_id': search_id}
                if source.get('locator', {}).get('splitting_version') != SPLITTING_VERSION:
                    record['warnings'] = [LEGACY_SPLITTING_WARNING]
                connection.execute('''INSERT INTO rag_evidence(id,search_id,project_id,library_id,index_id,version_id,chunk_id,payload)
                    SELECT ?,?,?,?,?,?,chunk_id,? FROM rag_evidence WHERE id=?''',
                    (record['id'], search_id, project_id, record['library_id'], record['index_id'], record['version_id'],
                     json.dumps(record, ensure_ascii=False), source['id']))
                evidence.append(record)
                aliases[source['id']] = record['id']
        return {'evidence': evidence, 'generation': state['generation'], 'index_ids': state['index_ids'],
                'external_untrusted': True, 'search_id': search_id, 'aliases': aliases}

    async def search(self, project_id, query, run_id=None, library_ids=None, version_ids=None, limit=8):
        if self._closed:
            raise CoreDataError('rag_closed', 'The RAG service is closed.')
        query = required_text(query, 'Search query', 2000)
        if not isinstance(limit, int) or not 1 <= limit <= 8:
            raise CoreDataError('rag_invalid_request', 'The evidence limit must be between 1 and 8.')
        for selection in (library_ids, version_ids):
            if selection is not None and (not isinstance(selection, list) or not selection or any(not isinstance(value, str) for value in selection)):
                raise CoreDataError('rag_invalid_request', 'Explicit library and version filters must be nonempty identifier lists.')
        with closing(self.repository.connect()) as connection:
            state = self.repository.state(project_id, connection)
            authorized = state['library_ids']
            selected = library_ids if library_ids is not None else authorized
            if not selected or not set(selected).issubset(authorized):
                raise CoreDataError('rag_library_forbidden', 'No authorized project library was selected.')
            indexes = []
            for library_id in dict.fromkeys(selected):
                row = connection.execute('''SELECT i.* FROM rag_bindings b JOIN rag_indexes i ON i.id=b.index_id
                    WHERE b.library_id=? AND b.project_id=? AND b.enabled=1''', (library_id, project_id)).fetchone()
                if row:
                    indexes.append(dict(row))
            if version_ids is not None:
                allowed_versions = set()
                for index in indexes:
                    allowed_versions.update(json.loads(index['version_ids']))
                if not set(version_ids).issubset(allowed_versions):
                    raise CoreDataError('rag_version_forbidden', 'A selected version is outside the authorized active indexes.')
        if not indexes:
            raise CoreDataError('rag_no_index', 'Build an authorized library index before searching.')
        chosen, index_by_chunk = await self._retrieve_indexes(indexes, query, version_ids, limit)
        evidence = []
        search_id = identifier()
        if self._closed:
            raise CoreDataError('rag_closed', 'The RAG service closed before retrieval completed.')
        with closing(self.repository.connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            current = self.repository.state(project_id, connection)
            if current['generation'] != state['generation']:
                raise CoreDataError('rag_generation_changed', 'The project evidence changed during retrieval. Search again.')
            connection.execute('''INSERT INTO rag_searches(id,project_id,run_id,query,generation,index_ids,created_at)
                VALUES (?,?,?,?,?,?,?)''', (search_id, project_id, run_id, query, state['generation'], json.dumps([index['id'] for index in indexes]), _utc_now()))
            for row in chosen:
                index = index_by_chunk[row['id']]
                version = connection.execute('SELECT status FROM rag_versions WHERE id=?', (row['version_id'],)).fetchone()
                if not version or version['status'] != 'confirmed':
                    raise CoreDataError('rag_evidence_invalid', 'A retrieved document was withdrawn during retrieval.')
                record = {'id': 'rag-' + identifier(), 'library_id': index['library_id'], 'version_id': row['version_id'],
                          'title': row['title'], 'revision': row['revision'], 'source': row['source'], 'document_number': row['document_number'],
                          'clause_number': row['clause_number'], 'locator': json.loads(row['locator']), 'text': row['text'],
                          'text_hash': row['text_hash'], 'score': row['score'], 'external_untrusted': True,
                          'run_id': run_id, 'search_id': search_id, 'index_id': index['id']}
                if record['locator'].get('splitting_version') != SPLITTING_VERSION:
                    record['warnings'] = [LEGACY_SPLITTING_WARNING]
                connection.execute('''INSERT INTO rag_evidence(id,search_id,project_id,library_id,index_id,version_id,chunk_id,payload)
                    VALUES (?,?,?,?,?,?,?,?)''', (record['id'], search_id, project_id, index['library_id'], index['id'], row['version_id'], row['id'], json.dumps(record, ensure_ascii=False)))
                evidence.append(record)
        return {'evidence': evidence, 'generation': state['generation'], 'index_ids': [index['id'] for index in indexes],
                'external_untrusted': True, 'search_id': search_id,
                'warnings': [LEGACY_SPLITTING_WARNING] if any(
                    set(json.loads(index['splitter_versions'])) != {SPLITTING_VERSION} for index in indexes) else []}

    async def close(self):
        self._closed = True
        tasks = list(self._tasks.values())
        for job_id, task in list(self._tasks.items()):
            self.repository.terminate_task(job_id, 'app_exit', interrupted=True)
            task.cancel()
        await self.parser.close()
        await self.ocr.close()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        result = self.models.close()
        if inspect.isawaitable(result):
            await result

    async def _retrieve_indexes(self, indexes, query, version_ids=None, limit=8):
        retrieved = []
        index_by_chunk = {}
        for index in indexes:
            self._model(index['embedding_model_id'], 'embedding', index['embedding_digest'])
            self._model(index['reranker_model_id'], 'reranker', index['reranker_digest'])
            query_vectors = await self.models.embed(index['embedding_model_id'], [query], query=True)
            if len(query_vectors) != 1:
                raise CoreDataError('rag_invalid_embedding', 'The query embedding batch is invalid.')
            blob = vector_blob(query_vectors[0], index['dimension'])
            with closing(self.repository.connect()) as connection:
                ids, scores = candidates(connection, index['id'], blob, query, version_ids)
                if not ids:
                    continue
                placeholders = ','.join('?' for _ in ids)
                rows = connection.execute('''SELECT c.*,v.title,v.revision,v.document_number,v.source FROM rag_chunks c
                    JOIN rag_versions v ON v.id=c.version_id WHERE c.id IN (''' + placeholders + ')', ids).fetchall()
                by_id = {row['id']: dict(row) for row in rows}
                ordered = [by_id[chunk_id] for chunk_id in ids]
            for row in ordered:
                if not has_readable_text(row['text']):
                    continue
                row['fusion_score'] = scores[row['id']]
                retrieved.append(row)
                index_by_chunk[row['id']] = index
        retrieved.sort(key=lambda row: (-row['fusion_score'], row['id']))
        shortlisted = retrieved[:40]
        reranker_groups = {}
        for row in shortlisted:
            model_id = index_by_chunk[row['id']]['reranker_model_id']
            reranker_groups.setdefault(model_id, []).append(row)
        ranked = []
        for model_id, rows in reranker_groups.items():
            reranked = await self.models.rerank(model_id, query, [row['text'] for row in rows])
            if len(reranked) != len(rows):
                raise CoreDataError('rag_invalid_rerank', 'The reranker returned an invalid batch.')
            for row, score in zip(rows, reranked):
                if not isinstance(score, (int, float)) or not math.isfinite(score):
                    raise CoreDataError('rag_invalid_rerank', 'The reranker returned a nonfinite score.')
                row['score'] = float(score)
                ranked.append(row)
        ranked.sort(key=lambda row: (-row['score'], -row['fusion_score'], row['id']))
        chosen = ranked[:limit]
        if not chosen:
            raise CoreDataError('rag_no_evidence', 'No confirmed evidence matches the authorized search scope.')
        return chosen, index_by_chunk

    async def search_library(self, library_id, query):
        query = required_text(query, 'Search query', 2000)
        with closing(self.repository.connect()) as c:
            library = self.repository.library(c, library_id)
            index = c.execute('SELECT * FROM rag_indexes WHERE id=?', (library['active_index_id'],)).fetchone()
            if not index:
                raise CoreDataError('rag_no_index', 'This library has no ready retrieval index.')
        chosen, _ = await self._retrieve_indexes([dict(index)], query)
        with closing(self.repository.connect()) as c:
            current = self.repository.library(c, library_id)
            if current['generation'] != library['generation']:
                raise CoreDataError('rag_generation_changed', 'The library changed. Search again.')
            result = []
            for row in chosen:
                version = self.repository.version(c, row['version_id'])
                if version['status'] != 'confirmed':
                    raise CoreDataError('rag_evidence_invalid', 'The source is no longer available.')
                result.append({**row, 'locator': json.loads(row['locator']), 'version_id': row['version_id']})
        return {'evidence': result, 'index_id': index['id']}

    async def update_metadata(self, params):
        version_id = params.get('version_id')
        if params.get('warnings_acknowledged') is not True:
            raise CoreDataError('rag_warnings_unaccepted', 'Confirm that you reviewed the source and parsing notices.')
        with closing(self.repository.connect()) as c:
            old = self.repository.version(c, version_id)
        self._require_index_idle(old['library_id'])
        if old['status'] == 'withdrawn':
            raise CoreDataError('rag_evidence_invalid', 'This version was withdrawn.')
        values = {field: required_text(params.get(field, old[field]), field) for field in ('title','revision','source','document_number')}
        new_id = identifier()
        job_id = identifier()
        summary = json.loads(old['ocr_summary'])
        if 'job_id' in summary:
            summary['origin_job_id'] = summary.pop('job_id')
        with closing(self.repository.connect()) as c, c:
            c.execute('BEGIN IMMEDIATE')
            current = self.repository.version(c, version_id)
            if current['status'] == 'withdrawn':
                raise CoreDataError('rag_evidence_invalid', 'This version was withdrawn.')
            c.execute("""INSERT INTO rag_versions(id,document_id,title,revision,source,document_number,original_hash,original_name,media_type,
                status,warnings,chunk_count,created_at,confirmed_at,ocr_summary,processing_stage,human_reviewed)
                VALUES (?,?,?,?,?,?,?,?,?,'confirmed',?,?,?,?,?,'final',1)""",
                (new_id,old['document_id'],values['title'],values['revision'],values['source'],values['document_number'],
                 old['original_hash'],old['original_name'],old['media_type'],old['warnings'],old['chunk_count'],_utc_now(),_utc_now(),json.dumps(summary)))
            rows = c.execute('SELECT * FROM rag_chunks WHERE version_id=? ORDER BY ordinal', (version_id,)).fetchall()
            from agent.rag.documents import MAX_CHUNKS
            count = c.execute('SELECT count(*) FROM rag_chunks ch JOIN rag_versions v ON v.id=ch.version_id JOIN rag_documents d ON d.id=v.document_id WHERE d.library_id=?', (old['library_id'],)).fetchone()[0]
            if count + len(rows) > MAX_CHUNKS:
                raise CoreDataError('rag_capacity_exceeded', 'This library has reached its stored fragment limit.')
            if c.execute("SELECT 1 FROM rag_jobs WHERE library_id=? AND kind='index.build' AND status IN ('queued','running')", (old['library_id'],)).fetchone():
                raise CoreDataError('rag_job_active', 'Wait for the current index task before saving a reviewed version.')
            from agent.rag.retrieval import lexical_tokens
            for chunk in rows:
                cid = identifier()
                c.execute('INSERT INTO rag_chunks(id,version_id,ordinal,text,text_hash,clause_number,locator) VALUES (?,?,?,?,?,?,?)',
                    (cid,new_id,chunk['ordinal'],chunk['text'],chunk['text_hash'],chunk['clause_number'],chunk['locator']))
                c.execute('INSERT INTO rag_fts(chunk_id,tokens) VALUES (?,?)', (cid, ' '.join(lexical_tokens(chunk['text']))))
            self.repository.bump_library(c, old['library_id'], revoke_projects=False)
            versions = self.repository.adopted_versions(old['library_id'], {old['document_id']: new_id})
            c.execute("INSERT INTO rag_jobs(id,kind,library_id,status,payload,created_at,updated_at) VALUES (?,'index.build',?,'queued',?,?,?)",
                      (job_id,old['library_id'],json.dumps({'version_ids':versions}),_utc_now(),_utc_now()))
        job = self.repository.job(job_id)
        self.repository.task_event(job_id, 'created', 'user_action')
        self._start(job, self._manual_index(job, versions))
        return {'version_id': new_id, 'job': job}

    def _require_index_idle(self, library_id):
        with closing(self.repository.connect()) as c:
            if c.execute("SELECT 1 FROM rag_jobs WHERE library_id=? AND kind='import.batch' AND status IN ('queued','running')", (library_id,)).fetchone():
                raise CoreDataError('rag_job_active', 'Files are still being processed. Wait for the import before rebuilding an index.')

    async def _manual_index(self, job, versions):
        lock = self._index_locks.setdefault(job['library_id'], asyncio.Lock())
        async with lock:
            await self._build_index(job, versions)
