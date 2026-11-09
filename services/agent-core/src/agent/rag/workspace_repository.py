"""Read models and audit helpers for the document-centered workspace."""
from contextlib import closing
import json

from agent.storage import CoreDataError, _utc_now


class WorkspaceRepository:
    def overview(self, project_id=None):
        with closing(self.connect()) as c:
            if project_id:
                self._project(c, project_id)
                rows = c.execute("""SELECT l.*,b.project_id,b.enabled,b.index_id FROM rag_libraries l
                    JOIN rag_bindings b ON b.library_id=l.id WHERE b.project_id=? AND l.archived_at IS NULL ORDER BY l.created_at,l.id""", (project_id,)).fetchall()
            else:
                rows = c.execute('SELECT * FROM rag_libraries WHERE archived_at IS NULL ORDER BY created_at,id').fetchall()
            libraries = []
            for row in rows:
                library = dict(row)
                library['document_count'] = c.execute("SELECT count(*) FROM rag_documents d WHERE library_id=? AND EXISTS (SELECT 1 FROM rag_versions v WHERE v.document_id=d.id AND v.status!='withdrawn')", (library['id'],)).fetchone()[0]
                index = c.execute('SELECT created_at,version_ids FROM rag_indexes WHERE id=?', (library['active_index_id'],)).fetchone()
                library['ready_count'] = c.execute("SELECT count(DISTINCT document_id) FROM rag_versions WHERE id IN (SELECT value FROM json_each(?)) AND status='confirmed'", (index['version_ids'] if index else '[]',)).fetchone()[0]
                library['project_count'] = c.execute('SELECT count(*) FROM rag_bindings WHERE library_id=? AND enabled=1', (library['id'],)).fetchone()[0]
                library['processing_count'] = c.execute("SELECT count(*) FROM rag_jobs WHERE library_id=? AND parent_id IS NULL AND status IN ('queued','running')", (library['id'],)).fetchone()[0]
                library['attention_count'] = c.execute("SELECT count(*) FROM rag_jobs WHERE library_id=? AND parent_id IS NULL AND status IN ('failed','interrupted') AND resolved_at IS NULL", (library['id'],)).fetchone()[0]
                library['indexed_at'] = index['created_at'] if index else None
                libraries.append(library)
        return {'libraries': libraries}

    def documents(self, library_id, offset=0, limit=50, query='', include_withdrawn=False):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise CoreDataError('rag_invalid_request', 'Invalid document page.')
        with closing(self.connect()) as c:
            self.library(c, library_id)
            rows = c.execute('''SELECT d.* FROM rag_documents d WHERE library_id=? AND EXISTS (
                SELECT 1 FROM rag_versions v WHERE v.document_id=d.id AND (? OR v.status!='withdrawn')
                AND (v.title LIKE ? OR v.original_name LIKE ?)) ORDER BY d.created_at DESC,d.id''',
                (library_id, int(include_withdrawn), '%' + query + '%', '%' + query + '%')).fetchall()
            documents = []
            for document in rows[offset:offset + limit]:
                versions = [dict(row) for row in c.execute('SELECT * FROM rag_versions WHERE document_id=? ORDER BY created_at DESC,rowid DESC', (document['id'],))]
                current = next((v for v in versions if v['id'] == document['adopted_version_id'] and v['status'] != 'withdrawn'), None)
                current = current or next((v for v in versions if v['status'] != 'withdrawn' and v['processing_stage'] == 'final'), None)
                current = current or next((v for v in versions if v['status'] != 'withdrawn'), versions[0])
                current['version_id'] = current['id']
                current['id'] = document['id']
                current['adopted'] = current['version_id'] == document['adopted_version_id']
                current['warnings'] = json.loads(current['warnings'])
                current['ocr_summary'] = json.loads(current['ocr_summary'])
                current['history_count'] = len(versions)
                documents.append(current)
            return {'documents': documents, 'total': len(rows), 'offset': offset}

    def version_history(self, document_id):
        with closing(self.connect()) as c:
            rows = c.execute('''SELECT v.*,d.adopted_version_id FROM rag_versions v JOIN rag_documents d ON d.id=v.document_id
                JOIN rag_libraries l ON l.id=d.library_id WHERE d.id=? AND l.archived_at IS NULL
                ORDER BY v.created_at DESC,v.rowid DESC''', (document_id,)).fetchall()
            return {'versions': [{**dict(row), 'ocr_summary': json.loads(row['ocr_summary']),
                                  'warnings': json.loads(row['warnings']), 'adopted': row['id'] == row['adopted_version_id']} for row in rows]}

    def task_event(self, job_id, action, source='system'):
        with closing(self.connect()) as c, c:
            job = c.execute('SELECT phase FROM rag_jobs WHERE id=?', (job_id,)).fetchone()
            if job:
                c.execute('INSERT INTO rag_task_events(job_id,action,source,phase,created_at) VALUES (?,?,?,?,?)',
                          (job_id, action, source, job[0], _utc_now()))

    def task_progress(self, job_id, phase, completed=0, total=None):
        with closing(self.connect()) as c, c:
            row = c.execute('SELECT phase,status,parent_id FROM rag_jobs WHERE id=?', (job_id,)).fetchone()
            if row is None or row['status'] not in {'queued', 'running'}:
                raise CoreDataError('rag_job_cancelled', 'The task is no longer active.')
            now = _utc_now()
            c.execute('''UPDATE rag_jobs SET phase=?,completed_units=?,total_units=?,updated_at=?,
                stage_started_at=CASE WHEN phase!=? OR stage_started_at IS NULL THEN ? ELSE stage_started_at END WHERE id=?''',
                (phase, completed, total, now, phase, now, job_id))
        if row['phase'] != phase:
            self.task_event(job_id, 'stage_changed')
        if row['parent_id']:
            self.task_progress(row['parent_id'], phase, completed, total)

    def task_detail(self, job_id):
        job = self.job(job_id)
        with closing(self.connect()) as c:
            children = [dict(row) for row in c.execute('SELECT j.*,o.total_pages AS document_pages,o.completed_pages AS ocr_completed_pages FROM rag_jobs j LEFT JOIN rag_ocr_jobs o ON o.job_id=j.id WHERE j.parent_id=? ORDER BY j.created_at,j.id', (job_id,))]
            events = [dict(row) for row in c.execute('''SELECT * FROM rag_task_events WHERE job_id=? OR job_id IN
                (SELECT id FROM rag_jobs WHERE parent_id=?) ORDER BY id''', (job_id, job_id))]
        return {'job': job, 'children': children, 'events': events}

    def terminate_task(self, job_id, source, interrupted=False):
        status = 'interrupted' if interrupted else 'cancelled'
        with closing(self.connect()) as c, c:
            c.execute('''UPDATE rag_jobs SET status=?,termination_source=?,finished_at=?,updated_at=?
                WHERE (id=? OR parent_id=?) AND status IN ('queued','running')''',
                (status, source, _utc_now(), _utc_now(), job_id, job_id))
        self.task_event(job_id, 'interrupt' if interrupted else 'cancel', source)

    def adopted_versions(self, library_id, replacements=None):
        replacements = replacements or {}
        with closing(self.connect()) as c:
            rows = c.execute('''SELECT d.id,d.adopted_version_id FROM rag_documents d JOIN rag_versions v
                ON v.id=d.adopted_version_id WHERE d.library_id=? AND v.status='confirmed' ''', (library_id,)).fetchall()
        selected = {row['id']: row['adopted_version_id'] for row in rows}
        selected.update(replacements)
        return list(selected.values())

    def resolve_previous_tasks(self, job_id):
        current = self.job(job_id)
        entries = current['payload'].get('entries', [])
        current_documents = {entry.get('document_id') for entry in entries if entry.get('document_id')}
        current_sources = {(entry.get('path'),entry.get('sha256')) for entry in entries}
        with closing(self.connect()) as c, c:
            old = c.execute("SELECT * FROM rag_jobs WHERE library_id=? AND parent_id IS NULL AND id!=? AND status IN ('failed','interrupted') AND resolved_at IS NULL", (current['library_id'],job_id)).fetchall()
            for row in old:
                prior = json.loads(row['payload']).get('entries', [])
                if prior and all((entry.get('document_id') in current_documents) or ((entry.get('path'),entry.get('sha256')) in current_sources) for entry in prior):
                    c.execute('UPDATE rag_jobs SET resolved_at=? WHERE id=?', (_utc_now(),row['id']))
                    c.execute("INSERT INTO rag_task_events(job_id,action,source,phase,created_at) VALUES (?,'resolved_by_later_import','system',?,?)", (row['id'],row['phase'],_utc_now()))
