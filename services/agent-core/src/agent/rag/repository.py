"""Transactional storage for local libraries, immutable versions and evidence."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import uuid

from agent.migrations.v24_rag import create_tables
from agent.rag.documents import MAX_CHUNKS
from agent.rag.retrieval import lexical_tokens
from agent.storage import CoreDataError, _open_database, _utc_now


def identifier():
    return str(uuid.uuid4())


def required_text(value, label, maximum=500):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= maximum:
        raise CoreDataError('rag_invalid_request', f'{label} must contain 1 to {maximum} characters.')
    return value.strip()


from agent.rag.workspace_repository import WorkspaceRepository


class RagRepository(WorkspaceRepository):
    def __init__(self, core_repository, recover_jobs=True):
        self.core = core_repository
        self.database_path = Path(core_repository.database_path)
        self.data_dir = self.database_path.parent / 'rag'
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as connection, connection:
            create_tables(connection)
            if recover_jobs:
                connection.execute("UPDATE rag_jobs SET status='interrupted',termination_source='app_exit',error='Application stopped before completion',updated_at=? WHERE status IN ('queued','running')", (_utc_now(),))

    def connect(self):
        return _open_database(self.database_path)

    def library(self, connection, library_id):
        row = connection.execute('SELECT * FROM rag_libraries WHERE id=? AND archived_at IS NULL', (library_id,)).fetchone()
        if row is None:
            raise CoreDataError('rag_library_not_found', 'The library does not exist.')
        return dict(row)

    def _project(self, connection, project_id):
        if not connection.execute('SELECT id FROM projects WHERE id=? AND deleted_at IS NULL', (project_id,)).fetchone():
            raise CoreDataError('project_not_found', 'The project does not exist.')

    def state(self, project_id, connection=None):
        if connection is None:
            with closing(self.connect()) as own:
                return self.state(project_id, own)
        self._project(connection, project_id)
        row = connection.execute('SELECT generation FROM rag_projects WHERE project_id=?', (project_id,)).fetchone()
        bindings = connection.execute('''SELECT l.id,b.index_id FROM rag_bindings b
            JOIN rag_libraries l ON l.id=b.library_id
            WHERE b.project_id=? AND b.enabled=1 AND l.archived_at IS NULL ORDER BY l.id''', (project_id,)).fetchall()
        return {'enabled': bool(bindings), 'generation': row['generation'] if row else 0,
                'library_ids': [row['id'] for row in bindings],
                'index_ids': [row['index_id'] for row in bindings if row['index_id']]}

    def bump_library(self, connection, library_id, revoke_projects=True):
        connection.execute('UPDATE rag_libraries SET generation=generation+1 WHERE id=?', (library_id,))
        if revoke_projects:
            connection.execute('''UPDATE rag_projects SET generation=generation+1 WHERE project_id IN
                (SELECT project_id FROM rag_bindings WHERE library_id=? AND enabled=1)''', (library_id,))

    def create_library(self, params):
        record = {'id': identifier(), 'name': required_text(params.get('name'), 'Library name', 80),
                  'embedding_model_id': params.get('embedding_model_id') or '',
                  'reranker_model_id': params.get('reranker_model_id') or ''}
        with closing(self.connect()) as connection, connection:
            connection.execute('''INSERT INTO rag_libraries
                (id,name,embedding_model_id,reranker_model_id,created_at) VALUES (?,?,?,?,?)''',
                (record['id'], record['name'], record['embedding_model_id'], record['reranker_model_id'], _utc_now()))
            return self.library(connection, record['id'])

    def update_library(self, params):
        with closing(self.connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            library = self.library(connection, params.get('library_id'))
            for field in ('name', 'embedding_model_id', 'reranker_model_id'):
                if field in params:
                    library[field] = required_text(params[field], field, 80 if field == 'name' else 500)
            if 'ocr_mode' in params:
                if params['ocr_mode'] not in {'auto','off'}:
                    raise CoreDataError('rag_invalid_request', 'Select automatic OCR or native text.')
                connection.execute('UPDATE rag_libraries SET ocr_mode=? WHERE id=?', (params['ocr_mode'], library['id']))
            connection.execute('UPDATE rag_libraries SET name=?,embedding_model_id=?,reranker_model_id=? WHERE id=?',
                               (library['name'], library['embedding_model_id'], library['reranker_model_id'], library['id']))
            return self.library(connection, library['id'])

    def bind(self, params):
        project_id, library_id = params.get('project_id'), params.get('library_id')
        if not isinstance(params.get('enabled'), bool):
            raise CoreDataError('rag_invalid_request', 'Binding enabled must be boolean.')
        enabled = int(params['enabled'])
        with closing(self.connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            library = self.library(connection, library_id)
            self._project(connection, project_id)
            previous = connection.execute('SELECT enabled FROM rag_bindings WHERE library_id=? AND project_id=?', (library_id, project_id)).fetchone()
            connection.execute('INSERT OR IGNORE INTO rag_projects(project_id) VALUES (?)', (project_id,))
            connection.execute('''INSERT INTO rag_bindings(library_id,project_id,enabled,index_id) VALUES (?,?,?,?)
                ON CONFLICT(library_id,project_id) DO UPDATE SET enabled=excluded.enabled,index_id=excluded.index_id''',
                (library_id, project_id, enabled, library['active_index_id'] if enabled else None))
            if enabled or previous is None or previous['enabled'] != enabled:
                connection.execute('UPDATE rag_projects SET generation=generation+1 WHERE project_id=?', (project_id,))
            return self.state(project_id, connection)

    def remove_library(self, library_id):
        with closing(self.connect()) as connection, connection:
            self.library(connection, library_id)
            self.bump_library(connection, library_id)
            connection.execute('UPDATE rag_libraries SET archived_at=? WHERE id=?', (_utc_now(), library_id))
            connection.execute('UPDATE rag_bindings SET enabled=0 WHERE library_id=?', (library_id,))
            connection.execute("UPDATE rag_jobs SET status='cancelled',termination_source='library_removal',finished_at=?,updated_at=? WHERE library_id=? AND status IN ('queued','running')", (_utc_now(), _utc_now(), library_id))
        return {'removed': True, 'library_id': library_id}

    def list_libraries(self, project_id=None):
        with closing(self.connect()) as connection:
            if project_id:
                self._project(connection, project_id)
                rows = connection.execute('''SELECT l.*,b.project_id,b.enabled,b.index_id FROM rag_libraries l
                    JOIN rag_bindings b ON b.library_id=l.id WHERE b.project_id=? AND l.archived_at IS NULL
                    ORDER BY l.created_at,l.id''', (project_id,)).fetchall()
            else:
                rows = connection.execute('SELECT *,NULL AS project_id FROM rag_libraries WHERE archived_at IS NULL ORDER BY created_at,id').fetchall()
            result = []
            for row in rows:
                library = dict(row)
                documents = connection.execute('''SELECT d.id,v.id AS version_id,v.title,v.revision,v.status,v.warnings,v.chunk_count,
                    v.source,v.document_number,v.original_name,v.media_type,v.created_at,v.processing_stage,v.human_reviewed,v.ocr_summary,(d.adopted_version_id=v.id) AS adopted FROM rag_documents d JOIN rag_versions v ON v.document_id=d.id
                    WHERE d.library_id=? ORDER BY v.created_at,v.rowid''', (row['id'],)).fetchall()
                library['documents'] = [{**dict(item), 'warnings': json.loads(item['warnings']), 'ocr_summary': json.loads(item['ocr_summary'])} for item in documents]
                result.append(library)
            return result

    def save_original(self, content, suffix):
        digest = hashlib.sha256(content).hexdigest()
        destination = self.data_dir / 'originals' / digest[:2] / (digest + suffix)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            temporary = destination.with_name(destination.name + '.' + identifier() + '.tmp')
            try:
                temporary.write_bytes(content)
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
        return digest, destination

    def save_version(self, library_id, title, parsed, original_hash, original_name, media_type, source_key=None, job_id=None, document_id=None, machine_confirm=False, complete_job=True, processing_stage='final'):
        now, version_id = _utc_now(), identifier()
        with closing(self.connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            self.library(connection, library_id)
            if job_id:
                self.require_running(connection, job_id)
            count = connection.execute('''SELECT COUNT(*) FROM rag_chunks c JOIN rag_versions v ON v.id=c.version_id
                JOIN rag_documents d ON d.id=v.document_id WHERE d.library_id=?''', (library_id,)).fetchone()[0]
            if count + len(parsed['chunks']) > MAX_CHUNKS:
                raise CoreDataError('rag_capacity_exceeded', 'The library capacity is 100,000 chunks including stored versions.')
            source_key = source_key or identifier()
            if document_id is not None:
                previous = connection.execute('SELECT id FROM rag_documents WHERE id=? AND library_id=?', (document_id, library_id)).fetchone()
                if previous is None:
                    raise CoreDataError('rag_document_not_found', 'The selected document does not belong to this library.')
            else:
                previous = connection.execute('SELECT id FROM rag_documents WHERE library_id=? AND source_key=?', (library_id, source_key)).fetchone()
            document_id = previous['id'] if previous else identifier()
            if previous is None:
                connection.execute('INSERT INTO rag_documents(id,library_id,source_key,created_at) VALUES (?,?,?,?)', (document_id, library_id, source_key, now))
            connection.execute('''INSERT INTO rag_versions(id,document_id,title,original_hash,original_name,media_type,status,warnings,chunk_count,created_at)
                VALUES (?,?,?,?,?,?,'pending',?,?,?)''',
                (version_id, document_id, required_text(title, 'Document title'), original_hash, original_name, media_type,
                 json.dumps(parsed['warnings'], ensure_ascii=False), len(parsed['chunks']), now))
            for ordinal, chunk in enumerate(parsed['chunks']):
                chunk_id = identifier()
                connection.execute('''INSERT INTO rag_chunks(id,version_id,ordinal,text,text_hash,clause_number,locator)
                    VALUES (?,?,?,?,?,?,?)''', (chunk_id, version_id, ordinal, chunk['text'], chunk['text_hash'], chunk.get('clause_number'), json.dumps(chunk['locator'], ensure_ascii=False)))
                connection.execute('INSERT INTO rag_fts(chunk_id,tokens) VALUES (?,?)', (chunk_id, ' '.join(lexical_tokens(chunk['text']))))
            connection.execute('UPDATE rag_versions SET processing_stage=? WHERE id=?', (processing_stage, version_id))
            if job_id:
                connection.execute('UPDATE rag_versions SET ocr_summary=? WHERE id=?', (json.dumps({**parsed.get('ocr', {}), 'job_id': job_id}), version_id))
            if machine_confirm:
                if not job_id:
                    raise ValueError('Automatic snapshots require an active job')
                summary = {**parsed.get('ocr', {}), 'job_id': job_id}
                connection.execute("UPDATE rag_versions SET ocr_summary=?,revision=?,source=?,document_number=?,status='confirmed',human_reviewed=1,confirmed_at=? WHERE id=?",
                    (json.dumps(summary), '未标注', '本机导入 · 机器识别', title, now, version_id))
                connection.execute('UPDATE rag_ocr_jobs SET latest_version_id=? WHERE job_id=?', (version_id, job_id))
                self.bump_library(connection, library_id, revoke_projects=False)
            if job_id and not machine_confirm and complete_job:
                connection.execute("UPDATE rag_jobs SET status='completed',progress=1,updated_at=? WHERE id=?", (now, job_id))
        return self.preview(version_id)

    def version(self, connection, version_id):
        row = connection.execute('''SELECT v.*,d.library_id FROM rag_versions v JOIN rag_documents d ON d.id=v.document_id
            JOIN rag_libraries l ON l.id=d.library_id WHERE v.id=? AND l.archived_at IS NULL''', (version_id,)).fetchone()
        if row is None:
            raise CoreDataError('rag_version_not_found', 'The document version does not exist.')
        return dict(row)

    def preview(self, version_id, offset=0, limit=200, page=None):
        if not isinstance(offset, int) or offset < 0 or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise CoreDataError('rag_invalid_range', 'Preview range is invalid.')
        with closing(self.connect()) as connection:
            version = self.version(connection, version_id)
            version['adopted'] = connection.execute('SELECT adopted_version_id FROM rag_documents WHERE id=?', (version['document_id'],)).fetchone()[0] == version_id
            version['warnings'] = json.loads(version['warnings'])
            version['ocr_summary'] = json.loads(version.get('ocr_summary', '{}'))
            if page is not None and (type(page) is not int or not 1 <= page <= 10000):
                raise CoreDataError('rag_invalid_request', 'Invalid physical page.')
            chunks = connection.execute("SELECT * FROM rag_chunks WHERE version_id=? AND (? IS NULL OR json_extract(locator,'$.page')=?) ORDER BY ordinal LIMIT ? OFFSET ?", (version_id, page, page, limit, offset)).fetchall()
            total = connection.execute("SELECT count(*) FROM rag_chunks WHERE version_id=? AND (? IS NULL OR json_extract(locator,'$.page')=?)", (version_id, page, page)).fetchone()[0]
            return {'version': version, 'chunks': [{**dict(row), 'locator': json.loads(row['locator'])} for row in chunks],
                    'warnings': version['warnings'], 'total_chunks': total, 'offset': offset}

    def original_for_version(self, version_id):
        with closing(self.connect()) as connection:
            version = self.version(connection, version_id)
            if version['status'] == 'withdrawn':
                raise CoreDataError('rag_evidence_invalid', 'The document was withdrawn')
        suffix = Path(version['original_name']).suffix.lower()
        return self.data_dir / 'originals' / version['original_hash'][:2] / (version['original_hash'] + suffix)

    def ocr_pages(self, version_id, page=None):
        with closing(self.connect()) as connection:
            version = self.version(connection, version_id)
            if version['status'] == 'withdrawn':
                raise CoreDataError('rag_evidence_invalid', 'The document was withdrawn')
            summary = json.loads(version.get('ocr_summary', '{}'))
            keys = summary.get('cache_keys', [])
            rows = connection.execute('SELECT payload FROM rag_ocr_pages WHERE original_hash=? AND cache_key IN (SELECT value FROM json_each(?)) ORDER BY page',
                (version['original_hash'], json.dumps(keys))).fetchall()
            if page is not None:
                rows = [row for row in rows if json.loads(row['payload']).get('page') == page]
            return {'pages': [json.loads(row['payload']) for row in rows], 'summary': summary}


    def confirm(self, params):
        with closing(self.connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            version = self.version(connection, params.get('version_id'))
            if version['status'] != 'pending':
                raise CoreDataError('rag_version_immutable', 'Only a pending version may be confirmed.')
            if json.loads(version['warnings']) and params.get('warnings_acknowledged', params.get('accept_warnings')) is not True:
                raise CoreDataError('rag_warnings_unaccepted', 'Explicitly acknowledge extraction warnings before publishing.')
            values = [required_text(params.get(field), field) for field in ('title', 'revision', 'source', 'document_number')]
            connection.execute("UPDATE rag_versions SET title=?,revision=?,source=?,document_number=?,status='confirmed',confirmed_at=? WHERE id=? AND status='pending'", (*values, _utc_now(), version['id']))
            self.bump_library(connection, version['library_id'], revoke_projects=False)
        return self.preview(version['id'])

    def withdraw(self, version_id):
        with closing(self.connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            version = self.version(connection, version_id)
            summary = json.loads(version.get('ocr_summary', '{}'))
            job_id = summary.get('job_id')
            versions = [version_id]
            if job_id:
                versions = [row['id'] for row in connection.execute(
                    "SELECT id FROM rag_versions WHERE document_id=? AND json_extract(ocr_summary,'$.job_id')=?",
                    (version['document_id'], job_id))]
                connection.execute("UPDATE rag_jobs SET status='cancelled',termination_source='document_withdrawal',finished_at=?,updated_at=? WHERE id=? AND status IN ('queued','running')", (_utc_now(), _utc_now(), job_id))
            connection.execute("UPDATE rag_versions SET status='withdrawn',withdrawn_at=? WHERE id IN (SELECT value FROM json_each(?))", (_utc_now(), json.dumps(versions)))
            self.bump_library(connection, version['library_id'], revoke_projects=False)
            connection.execute('''UPDATE rag_projects SET generation=generation+1 WHERE project_id IN (SELECT b.project_id FROM rag_bindings b WHERE b.library_id=? AND b.enabled=1 AND EXISTS (SELECT 1 FROM rag_index_chunks ic JOIN rag_chunks c ON c.id=ic.chunk_id WHERE ic.index_id=b.index_id AND c.version_id IN (SELECT value FROM json_each(?))))''', (version['library_id'], json.dumps(versions)))
        return {'withdrawn': True, 'version_id': version_id}

    def new_job(self, kind, library_id, *, parent_id=None, filename='', payload=None, request_key=None, attempt=1):
        job_id, now = identifier(), _utc_now()
        with closing(self.connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            self.library(connection, library_id)
            if request_key:
                previous = connection.execute('SELECT * FROM rag_jobs WHERE library_id=? AND request_key=?', (library_id, request_key)).fetchone()
                if previous:
                    result = dict(previous)
                    result['payload'] = json.loads(result['payload'])
                    return result
            if kind == 'index.build' and connection.execute("SELECT 1 FROM rag_jobs WHERE library_id=? AND kind=? AND status IN ('queued','running')", (library_id, kind)).fetchone():
                raise CoreDataError('rag_job_active', 'An index build is already active for this library.')
            connection.execute("INSERT INTO rag_jobs(id,kind,library_id,status,created_at,updated_at) VALUES (?,?,?,'queued',?,?)", (job_id, kind, library_id, now, now))
            connection.execute('UPDATE rag_jobs SET parent_id=?,filename=?,payload=?,request_key=?,attempt=? WHERE id=?',
                               (parent_id, filename, json.dumps(payload or {}), request_key, attempt, job_id))
        self.task_event(job_id, 'created')
        return self.job(job_id)

    def job(self, job_id):
        with closing(self.connect()) as connection:
            row = connection.execute('SELECT * FROM rag_jobs WHERE id=?', (job_id,)).fetchone()
            if row is None:
                raise CoreDataError('rag_job_not_found', 'The background job does not exist.')
            result = dict(row)
            result['payload'] = json.loads(result['payload'])
            return result

    def jobs(self, library_id=None):
        with closing(self.connect()) as connection:
            if library_id:
                rows = connection.execute('SELECT * FROM rag_jobs WHERE library_id=? ORDER BY created_at DESC', (library_id,)).fetchall()
            else:
                rows = connection.execute('SELECT * FROM rag_jobs ORDER BY created_at DESC LIMIT 200').fetchall()
            return [dict(row) for row in rows]

    def require_running(self, connection, job_id):
        row = connection.execute('SELECT status FROM rag_jobs WHERE id=?', (job_id,)).fetchone()
        if row is None or row['status'] != 'running':
            raise CoreDataError('rag_job_cancelled', 'The job is no longer active.')

    def update_job(self, job_id, status=None, progress=None, error=None):
        with closing(self.connect()) as connection, connection:
            row = connection.execute('SELECT * FROM rag_jobs WHERE id=?', (job_id,)).fetchone()
            if row is None or row['status'] not in {'queued', 'running'}:
                return
            connection.execute('UPDATE rag_jobs SET status=?,progress=?,error=?,updated_at=? WHERE id=?',
                               (status or row['status'], progress if progress is not None else row['progress'], error, _utc_now(), job_id))
            if status == 'running':
                connection.execute('UPDATE rag_jobs SET started_at=COALESCE(started_at,?) WHERE id=?', (_utc_now(), job_id))
            if status in {'completed','failed','cancelled','interrupted'}:
                connection.execute("UPDATE rag_jobs SET finished_at=?,phase=CASE WHEN ?='completed' THEN 'completed' ELSE phase END WHERE id=?", (_utc_now(), status, job_id))

    def checked_evidence(self, project_id, evidence_id, run_id=None, connection=None):
        if connection is None:
            with closing(self.connect()) as own:
                return self.checked_evidence(project_id, evidence_id, run_id, own)
        self._project(connection, project_id)
        row = connection.execute('''SELECT e.payload,s.run_id,v.original_hash FROM rag_evidence e
            JOIN rag_searches s ON s.id=e.search_id
            JOIN rag_versions v ON v.id=e.version_id
            JOIN rag_bindings b ON b.library_id=e.library_id AND b.project_id=e.project_id
            JOIN rag_libraries l ON l.id=e.library_id
            WHERE e.id=? AND e.project_id=? AND v.status='confirmed' AND b.enabled=1
                AND b.index_id=e.index_id AND l.archived_at IS NULL''',
            (evidence_id, project_id)).fetchone()
        if row is None or (run_id is not None and row['run_id'] != run_id):
            raise CoreDataError('rag_evidence_invalid', 'The evidence is unavailable, withdrawn, or outside the authorized project and run.')
        payload = json.loads(row['payload'])
        if payload.get('original_hash') not in (None, row['original_hash']):
            raise CoreDataError('rag_evidence_invalid', 'The evidence original identity changed. Retrieve the source again.')
        payload['original_hash'] = row['original_hash']
        return payload

    def preview_evidence(self, project_id, evidence_id, offset=0, length=8000):
        if not isinstance(offset, int) or offset < 0 or not isinstance(length, int) or not 1 <= length <= 8000:
            raise CoreDataError('rag_invalid_range', 'The evidence text range is invalid.')
        evidence = self.checked_evidence(project_id, evidence_id)
        text = evidence['text'][offset:offset + length]
        return {**evidence, 'text': text, 'content': text, 'offset': offset,
                'total_length': len(evidence['text']), 'external_untrusted': True}

    def read_evidence_context(self, project_id, evidence_id, offset=0, length=8000):
        result = self.preview_evidence(project_id, evidence_id, offset, length)
        locator = result.get('locator', {})
        if not isinstance(locator, dict) or not locator.get('block_id'):
            return result
        parent_id = locator.get('clause_parent_id') or locator['block_id']
        key = 'clause_parent_id'
        scope = 'clause' if result.get('clause_number') else 'block'
        if not result.get('clause_number') and locator.get('table_parent_id'):
            parent_id, key, scope = locator['table_parent_id'], 'table_parent_id', 'table'
        with closing(self.connect()) as connection:
            # Repeat authorization in the same read transaction used for context.
            connection.execute('BEGIN')
            self.checked_evidence(project_id, evidence_id, connection=connection)
            rows = connection.execute('''SELECT text,locator FROM rag_chunks WHERE version_id=?
                AND json_extract(locator,?)=? ORDER BY ordinal''',
                (result['version_id'], '$.' + key, parent_id)).fetchall()
        blocks = {}
        for row in rows:
            source_locator = json.loads(row['locator'])
            blocks.setdefault(source_locator['block_id'], []).append(row['text'])
        parent_text = '\n\n'.join(''.join(fragments) for fragments in blocks.values())
        if not parent_text:
            return result
        context = parent_text[offset:offset + length]
        result['parent_context'] = {
            'text': context, 'text_hash': hashlib.sha256(context.encode('utf-8')).hexdigest(),
            'parent_hash': hashlib.sha256(parent_text.encode('utf-8')).hexdigest(),
            'scope': scope, 'parent_id': parent_id, 'source_block_id': locator['block_id'],
            'offset': offset, 'total_length': len(parent_text), 'has_more': offset + len(context) < len(parent_text),
            'block_count': len(blocks), 'splitting_version': locator.get('splitting_version')}
        return result

    def index_versions(self, library_id, version_ids=None):
        with closing(self.connect()) as connection:
            self.library(connection, library_id)
            rows = connection.execute('''SELECT v.id,v.document_id FROM rag_versions v
                JOIN rag_documents d ON d.id=v.document_id WHERE d.library_id=? AND v.status='confirmed'
                ORDER BY v.document_id,v.id''', (library_id,)).fetchall()
        if version_ids is not None:
            if not isinstance(version_ids, list) or not version_ids or any(not isinstance(value, str) or not value for value in version_ids):
                raise CoreDataError('rag_invalid_request', 'Index versions must be a nonempty identifier list.')
            if len(version_ids) != len(set(version_ids)):
                raise CoreDataError('rag_invalid_request', 'Index version identifiers must be unique.')
            by_id = {row['id']: row for row in rows}
            if not set(version_ids).issubset(by_id):
                raise CoreDataError('rag_version_forbidden', 'Select confirmed versions belonging to this library.')
            rows = [by_id[version_id] for version_id in version_ids]
        document_ids = [row['document_id'] for row in rows]
        if len(document_ids) != len(set(document_ids)):
            code = 'rag_version_selection_required' if version_ids is None else 'rag_version_selection_invalid'
            raise CoreDataError(code, 'Select exactly one confirmed version for each included document.')
        if not rows:
            raise CoreDataError('rag_no_confirmed_documents', 'Confirm a readable document before building its index.')
        return [row['id'] for row in rows]

    def publish_index(self, connection, library_id, index_id):
        connection.execute('UPDATE rag_libraries SET active_index_id=? WHERE id=?', (index_id, library_id))
        connection.execute('UPDATE rag_documents SET adopted_version_id=NULL WHERE library_id=?', (library_id,))
        versions = json.loads(connection.execute('SELECT version_ids FROM rag_indexes WHERE id=?', (index_id,)).fetchone()[0])
        for version_id in versions:
            connection.execute('UPDATE rag_documents SET adopted_version_id=? WHERE id=(SELECT document_id FROM rag_versions WHERE id=?)', (version_id, version_id))
        self.bump_library(connection, library_id, revoke_projects=False)
        connection.execute("UPDATE rag_projects SET generation=generation+1 WHERE project_id IN (SELECT project_id FROM rag_bindings WHERE library_id=? AND enabled=1)", (library_id,))
        connection.execute('UPDATE rag_bindings SET index_id=? WHERE library_id=? AND enabled=1', (index_id, library_id))
