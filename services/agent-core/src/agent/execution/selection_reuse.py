"""Rebind only verified selection codes to an independently authorized document."""
import copy
import json

from agent.context_engine.attachment_references import task_attachment_ids
from agent.document_review import _hash
from agent.execution.goal_requirements import GoalRequirements
from agent.storage import CoreDataError


def _constraints(db, task_id, source, request, digest, *, resolved=False):
    """Allow one whole authorized attachment, one pending review, and no clarifications."""
    plan = db.execute('SELECT request_json FROM task_plan_revisions WHERE task_id=? AND revision=0',
                      (task_id,)).fetchone()
    goals = db.execute('SELECT requirements_json,request_digest,provenance FROM task_goal_requirements '
                       'WHERE task_id=? ORDER BY revision', (task_id,)).fetchall()
    if not plan or len(goals) != (2 if resolved else 1) or any(
            row['provenance'] != 'model_inferred' or row['request_digest'] != digest for row in goals):
        return None
    original = json.loads(plan[0])
    if (original.get('content') != request or original.get('attachment_references')
            or task_attachment_ids(db, task_id, original) != {source}):
        return None
    requirements = json.loads(goals[0][0])
    scopes = requirements.get('source_scopes', [])
    if len(scopes) != 1:
        return None
    scope = scopes[0]
    if (scope.get('source_id') != source or scope.get('operation') != 'review'
            or scope.get('scope') != 'selected' or scope.get('target_ids')
            or scope.get('selector') != {'kind': 'selection_pending', 'condition': request}):
        return None
    if resolved:
        committed = json.loads(goals[-1][0])
        final_scopes = committed.get('source_scopes', [])
        if (len(final_scopes) != 1 or final_scopes[0].get('source_id') != source
                or final_scopes[0].get('operation') != 'review'
                or final_scopes[0].get('scope') not in {'selected', 'metadata'}
                or committed.get('deliverables') != requirements.get('deliverables')
                or committed.get('success_criteria') != requirements.get('success_criteria')):
            return None
    scope.pop('source_id')
    return requirements, original.get('rag_mode')


def _checkpoint_matches(db, prior, environment, request):
    # Historical records already captured the pinned engine and index versions.
    # The projection never fetches the model's review body.
    rows = db.execute("""SELECT identity_json FROM execution_wait_checkpoints
        WHERE task_id=? AND stage='document_review' AND checkpoint_key LIKE 'document-public-review-%'
        AND json_extract(identity_json,'$.template_id')=?""", (prior['id'], prior['artifact_id']))
    matched = False
    for row in rows:
        record = json.loads(row[0])
        if (record.get('authorization_source_id') not in {prior['source_id'], prior['artifact_id']}
                or record.get('template_sha256') != prior['source_hash']
                or record.get('request_sha256') != _hash(request)
                or any(key not in record or record[key] != value for key, value in environment.items())):
            return False
        matched = True
    return matched


def verified_seed(repository, task_id, request, identity, fields, template, environment, preexcluded):
    from agent.execution.document_review_selection import CODES, sha
    if not environment or not environment.get('managed_model') or not environment.get('project_id'):
        return None
    goals = GoalRequirements(repository)
    with repository._connect() as db:
        project = db.execute("""SELECT c.project_id FROM execution_wait_tasks t JOIN model_runs r ON r.id=t.run_id
            JOIN conversations c ON c.id=r.conversation_id JOIN rag_projects p ON p.project_id=c.project_id
            WHERE t.id=? AND p.generation=?""", (task_id, environment['project_generation'])).fetchone()
        if (not project or project[0] != environment['project_id'] or goals.answers(task_id, connection=db)
                or goals.request_digest(task_id, connection=db) != identity['request_digest']):
            return None
        constraints = _constraints(db, task_id, identity['source_id'], request, identity['request_digest'])
        if constraints is None:
            return None
        candidates = db.execute("""SELECT t.id,s.source_id,s.source_hash,s.artifact_id,g.request_digest
            FROM execution_wait_tasks t JOIN model_runs r ON r.id=t.run_id JOIN conversations c ON c.id=r.conversation_id
            JOIN task_inventory_sources s ON s.task_id=t.id JOIN task_goal_execution g ON g.task_id=t.id
            WHERE t.id<>? AND t.status='completed' AND c.project_id=? AND s.source_hash=? AND s.complete=1
            AND r.provider_profile_id=? AND r.model_id=? ORDER BY t.created_at DESC LIMIT 16""",
            (task_id, project[0], identity['source_sha256'], environment['provider_id'], environment['model_id']))
        for prior in candidates:
            old_id = prior['id']
            try:
                eligible = (not goals.answers(old_id, connection=db)
                            and goals.request_digest(old_id, connection=db) == prior['request_digest']
                            and _constraints(db, old_id, prior['source_id'], request, prior['request_digest'],
                                             resolved=True) == constraints
                            and _checkpoint_matches(db, prior, environment, request))
            except (CoreDataError, ValueError, TypeError, KeyError):
                # An obsolete authorization or malformed old receipt is a cache miss.
                continue
            if not eligible:
                continue
            row = db.execute("""SELECT json_extract(payload_json,'$._review_selection') FROM task_goal_execution
                WHERE task_id=?""", (old_id,)).fetchone()
            if not row or not row[0]:
                continue
            try:
                saved = json.loads(row[0])
            except (TypeError, ValueError):
                continue
            if not isinstance(saved, dict) or not isinstance(saved.get('identity'), dict):
                continue
            expected = {**identity, 'source_id':prior['source_id'], 'request_digest':prior['request_digest']}
            previous = saved.get('identity', {})
            # Role review always runs under today's policy; reuse only base codes.
            expected['role_review_contract_sha256'] = previous.get('role_review_contract_sha256')
            if saved.get('complete') is not True or saved.get('classification_complete') is not True or previous != expected:
                continue
            seed = _validated_batches(saved, fields, identity, template, preexcluded, CODES, sha)
            if seed:
                seed['reused_selection'] = {
                    'from_task_id':old_id, 'base_count':len(seed['base_decisions']),
                    'environment_sha256':sha(environment)}
                return seed
    return None


def _validated_batches(saved, fields, identity, template, preexcluded, codes, sha):
    previous = saved.get('base_decisions')
    final = saved.get('decisions')
    field_by_id = {f['id']: f for f in fields}
    if not isinstance(previous, dict) or not isinstance(final, dict) or set(previous) != set(final) or set(final) != set(field_by_id):
        return None
    batches, retained, seen = [], {}, set()
    if not isinstance(saved.get('batches'), list):
        return None
    for batch in saved['batches']:
        ids = batch.get('target_ids') if isinstance(batch, dict) else None
        if (not isinstance(ids, list) or not all(isinstance(k,str) and k in previous for k in ids)
                or len(ids) != len(set(ids)) or seen.intersection(ids) or batch.get('source_sha256') != template['sha256']
                or batch.get('decision_sha256') != sha({k:previous[k] for k in ids})):
            return None
        seen.update(ids)
        accepted = []
        for key in ids:
            item = previous[key]
            if not isinstance(item,dict) or not isinstance(final[key],dict):
                return None
            code = item.get('basis_code')
            if (key in preexcluded or item.get('selection_issue') or final[key].get('selection_issue')
                    or final[key].get('decision') not in ('include', 'exclude', 'context') or not isinstance(code,str) or code not in codes or codes[code][0] == 'uncertain'):
                continue
            refs = item.get('context_ids')
            if (item.get('text_sha256') != sha(field_by_id[key].get('text',''))
                    or (item.get('decision'),item.get('reason')) != codes[code]
                    or not isinstance(refs,list) or not all(isinstance(k,str) and k in field_by_id for k in refs)):
                return None
            retained[key] = {k:item[k] for k in ('decision','reason','basis_code','context_ids','text_sha256')}
            accepted.append(key)
        if accepted:
            batches.append({'target_ids':accepted, 'source_sha256':template['sha256'], 'corrections':0,
                            'decision_sha256':sha({k:retained[k] for k in accepted})})
    if retained:
        return {'identity':identity, 'base_decisions':retained, 'decisions':copy.deepcopy(retained),
                'batches':batches, 'source_role_reviews':{}, 'context_notes':'', 'context_ids':[]}
