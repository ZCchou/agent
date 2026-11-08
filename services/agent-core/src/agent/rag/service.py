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
from agent.rag.documents import MAX_FILE_BYTES, MAX_TEXT_CHARS, SUPPORTED_SUFFIXES
from agent.rag.repository import RagRepository, identifier, required_text
from agent.rag.retrieval import candidates, load_vector_extension, vector_blob
from agent.storage import CoreDataError, _utc_now

DOCUMENT_PARSE_TIMEOUT = 125
MEDIA_TYPES = {'.pdf': 'application/pdf', '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
               '.txt': 'text/plain', '.md': 'text/markdown', '.markdown': 'text/markdown'}


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
        if action.startswith('model.'):
            return await self._model_action(action, params)
        if action == 'library.list':
            return {'libraries': self.repository.list_libraries(params.get('project_id'))}
        if action == 'library.create':
            self._model(params.get('embedding_model_id'), 'embedding')
            self._model(params.get('reranker_model_id'), 'reranker')
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
            paths = params.get('paths')
            if not isinstance(paths, list) or not 1 <= len(paths) <= 32 or any(not isinstance(path, str) or not path for path in paths):
                raise CoreDataError('rag_invalid_request', 'Select between 1 and 32 document paths.')
            jobs = []
            for path in paths:
                job = self.repository.new_job('document.import', params.get('library_id'))
                self._start(job, self._import_document(job, Path(path)))
                jobs.append(job)
            return {'job': jobs[0], 'jobs': jobs}
        if action == 'document.preview':
            return self.repository.preview(params.get('version_id'), params.get('offset', 0), params.get('limit', 200))
        if action == 'document.confirm':
            return self.repository.confirm(params)
        if action == 'document.withdraw':
            return self.repository.withdraw(params.get('version_id'))
        if action == 'job.list':
            return {'jobs': self.repository.jobs(params.get('library_id'))}
        if action == 'job.cancel':
            job_id = params.get('job_id')
            self.repository.job(job_id)
            self.repository.update_job(job_id, status='cancelled')
            task = self._tasks.get(job_id)
            if task:
                task.cancel()
            return {'job': self.repository.job(job_id)}
        if action == 'index.build':
            versions = self.repository.index_versions(params.get('library_id'), params.get('version_ids'))
            job = self.repository.new_job('index.build', params.get('library_id'))
            self._start(job, self._build_index(job, versions))
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
        except asyncio.CancelledError:
            self.repository.update_job(job['id'], status='cancelled')
        except Exception as error:
            self.repository.update_job(job['id'], status='failed', error=str(error)[:2000])
        finally:
            coroutine.close()

    def _extract_original(self, path):
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            raise CoreDataError('rag_document_unsupported', 'Supported documents are DOCX, text PDF, UTF-8 TXT and Markdown.')
        with path.open('rb') as source:
            content = source.read(MAX_FILE_BYTES + 1)
        if len(content) > MAX_FILE_BYTES:
            raise CoreDataError('rag_document_too_large', 'The document exceeds 100 MiB.')
        digest, original = self.repository.save_original(content, path.suffix.lower())
        return digest, original

    async def _import_document(self, job, path):
        try:
            digest, original = await asyncio.wait_for(
                asyncio.to_thread(self._extract_original, path), timeout=DOCUMENT_PARSE_TIMEOUT)
        except TimeoutError as error:
            raise CoreDataError('rag_document_timeout', 'Document import exceeded its time limit.') from error
        # ParserService bounds execution after its permit, excluding queue time.
        parsed = await self.parser.parse(original)
        if self._closed:
            raise CoreDataError('rag_closed', 'The RAG service closed before the document was saved.')
        self.repository.save_version(job['library_id'], path.stem, parsed, digest, path.name,
                                     MEDIA_TYPES[path.suffix.lower()],
                                     str(path.resolve()), job['id'])

    async def _build_index(self, job, version_ids):
        library_id = job['library_id']
        version_ids = self.repository.index_versions(library_id, version_ids)
        with closing(self.repository.connect()) as connection:
            library = self.repository.library(connection, library_id)
            chunks = connection.execute('''SELECT c.id,c.text,c.version_id,c.locator FROM rag_chunks c
                JOIN rag_versions v ON v.id=c.version_id JOIN rag_documents d ON d.id=v.document_id
                WHERE d.library_id=? AND v.status='confirmed' AND v.id IN (SELECT value FROM json_each(?))
                ORDER BY c.version_id,c.ordinal''', (library_id, json.dumps(version_ids))).fetchall()
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
                connection.execute('''INSERT INTO rag_evidence(id,search_id,project_id,library_id,index_id,version_id,chunk_id,payload)
                    VALUES (?,?,?,?,?,?,?,?)''', (record['id'], search_id, project_id, index['library_id'], index['id'], row['version_id'], row['id'], json.dumps(record, ensure_ascii=False)))
                evidence.append(record)
        return {'evidence': evidence, 'generation': state['generation'], 'index_ids': [index['id'] for index in indexes],
                'external_untrusted': True, 'search_id': search_id}

    async def close(self):
        self._closed = True
        tasks = list(self._tasks.values())
        for job_id, task in list(self._tasks.items()):
            self.repository.update_job(job_id, status='cancelled')
            task.cancel()
        await self.parser.close()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        result = self.models.close()
        if inspect.isawaitable(result):
            await result
