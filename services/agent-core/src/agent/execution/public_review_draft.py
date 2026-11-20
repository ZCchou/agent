"""Recover public, source-checked review output without running tools or publishing."""
import json
import sqlite3

from agent.execution.document_goal_runtime import validate_sources
from agent.execution.document_progress import DocumentProgress
from agent.rag.grounding import plain_markdown
from agent.storage import CoreDataError


def public_review_draft(service, run):
    state = service.rag_grounding.runs.get(run['id'])
    if not state:
        return ''
    try:
        service.rag_grounding.validate(run['id'])
        with service.repository._connect() as db:
            row = db.execute('SELECT t.id,g.payload_json,g.goal_revision,g.request_digest FROM execution_wait_tasks t '
                             'JOIN task_goal_execution g ON t.id=g.task_id WHERE t.run_id=?', (run['id'],)).fetchone()
        if row is None:
            return ''
        goals = service.task_plans.goals.get(row['id'])
        scopes = [s for s in (goals or {}).get('source_scopes', []) if s['scope'] != 'excluded']
        if (not scopes or any(s['operation'] != 'review' for s in scopes)
                or goals['revision'] != row['goal_revision'] or goals['request_digest'] != row['request_digest']):
            return ''
        validate_sources(service, run, row['id'], goals)
        payload = json.loads(row['payload_json'])
        receipt = run.get('_public_review_artifact') or payload.get('_prepared_review', {})
        snapshot = service.execution_monitor.get_task(row['id'])
        note = '本地文件交付尚未核验。' if goals.get('deliverables') else ''
        body = ''
        if (receipt and receipt.get('goal_revision') == goals['revision']
                and receipt.get('request_digest') == goals['request_digest']
                and receipt.get('evidence_generation') == state['generation']):
            artifact = service.documents.get(run['id'], receipt['artifact_id'])
            review = artifact['manifest'].get('review_report', {})
            source = service.documents.get(run['id'], review['template_id'])
            service.documents.path_for(run['id'], source['id'])
            if (artifact['kind'] == 'markdown' and artifact['sha256'] == receipt['artifact_sha256']
                    and artifact['manifest'].get('rag_generation') == state['generation']
                    and source['sha256'] == review.get('template_sha256') == receipt.get('source_sha256')):
                body = service.documents.path_for(run['id'], artifact['id']).read_bytes().decode('utf-8')
                # A later failure need not invalidate a previously verified publication.
                if artifact['status'] == 'published':
                    try:
                        service.documents.verify_publication(run['id'], artifact['id'])
                    except CoreDataError:
                        note = '本地文件交付尚未核验。'
                    else:
                        note = '本地文件已核验：' + plain_markdown(artifact['output_path'])
        if not body:
            with service.repository._connect() as db:
                saved = db.execute("SELECT identity_json,payload_json FROM execution_wait_checkpoints "
                    "WHERE task_id=? AND checkpoint_key LIKE 'document-public-review-%' ORDER BY committed_at,rowid",
                    (row['id'],)).fetchall()
            chunks, seen = [], set()
            for record in saved:
                identity, section = map(json.loads, record)
                if (section.get('goal_revision') != goals['revision'] or section.get('request_digest') != goals['request_digest']
                        or identity.get('project_generation') != state['generation'] or not section.get('body')):
                    continue
                template = service.documents.get(run['id'], identity['template_id'])
                service.documents.path_for(run['id'], template['id'])
                if identity['template_sha256'] != template['sha256'] or identity.get('project_id') != template['project_id']:
                    continue
                ids = section['target_ids']
                if identity['targets'] != ids or seen.intersection(ids):
                    continue
                seen.update(ids)
                chunks.append(section['body'])
            if chunks:
                body = '\n\n'.join(chunks) + '\n\n尚未完成整份审阅。'
        if not body:
            progress = DocumentProgress(service.repository).snapshot(snapshot, refresh=True) or {}
            finding = progress.get('latest_review') or {}
            if not finding.get('summary'):
                return ''
            body = '最近已保存的审阅结果\n\n' + plain_markdown(finding['target_id']) + ' · ' + plain_markdown(finding['summary'])
            if finding.get('citations'):
                body += '\n\n依据：' + '、'.join(map(plain_markdown, finding['citations']))
            body += '\n\n尚未完成整份审阅。'
        service.rag_grounding.validate(run['id'])
        return '## 已保存的审阅内容\n\n' + body + ('\n\n' + note if note else '')
    except (CoreDataError, OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        return ''
