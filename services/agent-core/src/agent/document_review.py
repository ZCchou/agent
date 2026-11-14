"""Read-only, whole-template review with run-authorized evidence checkpoints."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import inspect
import json
import re
import uuid

from agent.context_engine.policy import estimate_tokens
from agent.document_plan import document_structure, field_type, validate_source_locators
from agent.documents import load_docx, targets
from agent.execution.execution_wait import current_activity
from agent.providers import ProviderError
from agent.rag.grounding import plain_markdown
from agent.rag.support import review_issues
from agent.storage import CoreDataError


TRANSPORT_FAILURE_CODES = frozenset({
    'runtime_crashed', 'network_unavailable', 'connection_reset', 'request_timeout',
    'stream_stalled', 'managed_session_changed', 'model_not_loaded',
    'provider_response_incomplete', 'provider_empty_output',
})


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


async def _invoke(callback, *args):
    result = callback(*args)
    return await result if inspect.isawaitable(result) else result


def _invalid(message='审阅批次结构或目标覆盖无效。'):
    return CoreDataError('document_review_invalid', message)


def _strings(value):
    if not isinstance(value, list) or len(value) > 200 or any(
            not isinstance(item, str) or not item.strip() or len(item) > 1000 for item in value):
        raise _invalid('审阅问题、版本差异和缺项必须是有效文字清单。')
    return list(dict.fromkeys(value))


def _pending(fields, reason, *, code=None):
    return {'results': [{'target_id': field['id'], 'status': 'pending',
             'summary': '该位置需要补充依据或人工复核。', 'problems': [], 'evidence': [],
             'version_issues': [], 'gaps': [reason], 'error_code': code} for field in fields]}


def _source_identity(source):
    # Evidence IDs belong to a retrieval admission. Full visible source content,
    # quote IDs and the project generation establish reusable review identity.
    return {key: value for key, value in source.items() if key != 'id'}


def _rebind(response, previous, current):
    by_identity = {}
    for source in current:
        by_identity.setdefault(_hash(_source_identity(source)), []).append(source['id'])
    aliases = {}
    for source in previous:
        matches = by_identity.get(_hash(_source_identity(source)), [])
        if len(matches) == 1:
            aliases[source['id']] = matches[0]
    response = deepcopy(response)
    for result in response.get('results', []):
        for citation in result.get('evidence', []):
            if citation.get('id') not in aliases:
                raise CoreDataError('rag_evidence_stale', '已保存审阅的证据身份无法重新核验。')
            citation['id'] = aliases[citation['id']]
    return response


def _version_issues(template, metadata):
    expected = template['manifest'].get('referenced_standards', [])
    cited = re.findall(r'DO[-－ ]?160[FG]', ' '.join(str(item.get('title') or '') for item in metadata), re.I)
    canonical = lambda values: {re.sub(r'[-－ ]', '', value).upper() for value in values}
    if expected and cited and not canonical(expected) <= canonical(cited):
        return ['模板所引用标准与本批依据版本存在差异，请核对适用的受控版本。']
    return []


async def _validate(response, fields, template, evidence_resolver):
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except ValueError as error:
            raise _invalid('审阅回答必须是 JSON 对象。') from error
    results = response.get('results') if isinstance(response, dict) else None
    if not isinstance(results, list) or len(results) != len(fields):
        raise _invalid('审阅回答需要逐一覆盖当前批次的所有目标。')
    available, validated = {field['id']: field for field in fields}, {}
    for result in results:
        key = result.get('target_id') if isinstance(result, dict) else None
        if not isinstance(key, str) or key not in available or key in validated:
            raise _invalid('审阅目标重复或超出当前批次。')
        summary, status = result.get('summary'), result.get('status')
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 8000 \
                or not isinstance(status, str) or status not in {'reviewed', 'pending'}:
            raise _invalid()
        problems = _strings(result.get('problems', []))
        versions = _strings(result.get('version_issues', []))
        gaps = _strings(result.get('gaps', []))
        citations = result.get('evidence', [])
        if not isinstance(citations, list) or len(citations) > 8 or any(
                not isinstance(item, dict) or set(item) != {'id', 'quote_id'}
                or any(not isinstance(item[name], str) or not item[name] for name in ('id', 'quote_id'))
                for item in citations):
            raise CoreDataError('rag_quote_invalid', '审阅引用必须选择当前证据的摘录编号。')
        metadata = []
        if citations:
            if evidence_resolver is None:
                raise CoreDataError('rag_citation_invalid', '审阅引用需要本轮证据核验。')
            claim = '\n'.join([summary, *problems])
            _, metadata = await _invoke(evidence_resolver, claim, citations)
            validate_source_locators(summary, metadata)
            if not isinstance(metadata, list) or not metadata:
                raise CoreDataError('rag_evidence_missing', '审阅引用缺少已核验的证据。')
            gaps.extend(review_issues(metadata))
            versions.extend(issue for item in metadata for issue in item.get('version_issues', []))
            versions.extend(_version_issues(template, metadata))
        else:
            # Unsupported model prose remains an explicitly pending assessment.
            gaps.append('该项尚未关联可核验的资料库证据，结论待人工复核。')
        if gaps or versions or not citations:
            status = 'pending'
        validated[key] = {**available[key], 'target_id': key, 'status': status,
            'summary': summary, 'problems': problems, 'citations': citations, 'evidence': metadata,
            'version_issues': list(dict.fromkeys(versions)), 'gaps': list(dict.fromkeys(gaps)),
            'error_code': result.get('error_code')}
    return [validated[field['id']] for field in fields]


def _batch(base, fields, index):
    return {**base, 'index': index, 'chapter_path': fields[0]['chapter_path'],
            'section_id': fields[0]['section_id'], 'fields': fields,
            'target_ids': [field['id'] for field in fields]}


def _fits(batch, evidence, token_allowance):
    # Runtime already separates the model window into input/output allowances.
    # Apply the normal mixed-language estimator once, with instruction and
    # format-correction space inside the supplied input allowance.
    return estimate_tokens({'batch': batch, 'evidence': evidence}) + 256 <= token_allowance


def _call_count(activity, identity):
    # Progress is an append-only, fenced record and does not clear consecutive
    # recovery failures. Confirmed transport outcomes remain audited separately
    # from the three completed-output format trials. Unknown interruptions
    # conservatively consume a trial until an outcome can be established.
    with activity.monitor.repository._connect() as connection:
        return connection.execute(
            '''SELECT COUNT(*) FROM execution_wait_activity a
               WHERE a.task_id=? AND a.kind='progress'
                 AND json_extract(a.payload_json,'$.review_call_identity')=?
                 AND json_extract(a.payload_json,'$.review_call_number') IS NOT NULL
                 AND NOT EXISTS (
                     SELECT 1 FROM execution_wait_activity outcome
                     WHERE outcome.task_id=a.task_id AND outcome.kind='progress'
                       AND json_extract(outcome.payload_json,'$.review_transport_failure')=
                           json_extract(a.payload_json,'$.review_reservation_id'))''',
            (activity.task_id, identity)).fetchone()[0]


def _review_parent(workflow, run_id, template, activity, parent_task_id):
    if parent_task_id is None:
        return None
    if not isinstance(parent_task_id, str) or not parent_task_id or parent_task_id == activity.task_id:
        raise CoreDataError('document_review_lineage_invalid', '审阅重试来源身份无效。')
    current = workflow.repository.get_model_run(run_id)
    root = activity.monitor.get_task(activity.task_id)
    parent = activity.monitor.get_task(parent_task_id)
    if root['run_id'] != run_id or parent['status'] not in {'completed', 'failed', 'cancelled', 'interrupted'}:
        raise CoreDataError('document_review_lineage_invalid', '审阅重试需要已结束的关联运行。')
    ancestor_id, seen = current.get('parent_run_id'), {run_id}
    while ancestor_id and ancestor_id not in seen:
        ancestor = workflow.repository.get_model_run(ancestor_id)
        if any(ancestor[key] != current[key] for key in ('conversation_id', 'user_message_id')):
            break
        if ancestor_id == parent['run_id']:
            project = workflow.repository.get_run_conversation(ancestor_id).get('project_id')
            if project == template['project_id']:
                return {'task_id': parent_task_id, 'run_id': ancestor_id}
            break
        seen.add(ancestor_id)
        ancestor_id = ancestor.get('parent_run_id')
    raise CoreDataError('document_review_lineage_invalid', '审阅检查点不属于当前请求的关联重试链。')


def _parent_checkpoint(workflow, activity, parent, key, identity, fields):
    if parent is None:
        return None
    with activity.monitor.repository._connect() as connection:
        row = connection.execute(
            '''SELECT c.identity_json,c.payload_json FROM execution_wait_checkpoints c
               JOIN execution_wait_tasks t ON t.id=c.task_id
               WHERE c.task_id=? AND c.stage=? AND c.checkpoint_key=?
                 AND t.status IN ('completed','failed','cancelled','interrupted')''',
            (parent['task_id'], activity.stage, key)).fetchone()
    if row is None:
        return None
    try:
        saved_identity, payload = json.loads(row['identity_json']), json.loads(row['payload_json'])
        comparable = {key: value for key, value in identity.items() if key != 'template_id'}
        previous = {key: value for key, value in saved_identity.items() if key != 'template_id'}
        if previous != comparable or payload.get('format_version') != 1:
            return None
        prior_fields = payload.get('results', [])
        if len(prior_fields) != len(fields) or any(
                any(prior_field.get(key) != value for key, value in field.items() if key != 'status')
                for field, prior_field in zip(fields, prior_fields)):
            return None
        template = workflow.get(parent['run_id'], saved_identity['template_id'])
        if template['sha256'] != identity['template_sha256'] or template['project_id'] != identity['project_id']:
            return None
        workflow.path_for(parent['run_id'], template['id'])
        return payload
    except (CoreDataError, KeyError, TypeError, ValueError):
        return None


def _markdown(report):
    coverage = report['coverage']
    lines = ['# 全文非空内容审阅', '', '适航资料分析草案，待人工复核。', '',
             f"非空位置共 {coverage['total']} 项，已审阅 {coverage['reviewed']} 项，待处理 {coverage['pending']} 项。"]
    for target in report['targets']:
        title = ' / '.join(target['chapter_path']) or '文档正文'
        lines.extend(['', '## ' + plain_markdown(title) + ' · ' + plain_markdown(target['id']), '',
                      '已审阅' if target['status'] == 'reviewed' else '待人工复核', '',
                      plain_markdown(target['summary'])])
        for label, key in [('问题', 'problems'), ('版本差异', 'version_issues'), ('缺项', 'gaps')]:
            for item in target[key]:
                lines.extend(['', label + '。' + plain_markdown(item)])
        for source in target['evidence']:
            source_id = source.get('id')
            title = plain_markdown(str(source.get('title') or '资料依据'))
            if isinstance(source_id, str) and re.fullmatch(r'[A-Za-z0-9_-]+', source_id):
                lines.extend(['', f'[{title}](rag-evidence:{source_id})'])
            if source.get('quote'):
                lines.extend(['', '> ' + plain_markdown(source['quote']).replace('\n', '\n> ')])
    return '\n'.join(lines)


async def review_template(workflow, run_id, template_id, request, generate, evidence,
                          evidence_resolver, *, activity=None, identity=None, token_allowance=12000,
                          parent_task_id=None):
    """Review every nonempty target of an authorized managed DOCX artifact.

    ``generate(batch, evidence)`` performs exactly one actual model invocation.
    This function owns the per-batch three-output correction budget. ``evidence``
    re-admits project RAG sources for every batch, including restored batches.
    Caller identity includes semantic task revision, models and configuration.
    Optional ``parent_task_id`` comes from the runtime-owned linked-run lineage.
    Parent checkpoints are read-only and revalidated against current admission.
    """
    if not isinstance(request, str) or not request.strip() or len(request) > 32000:
        raise _invalid('请提供有效的全文审阅请求。')
    if type(token_allowance) is not int or token_allowance < 512:
        raise CoreDataError('document_context_too_small', '当前上下文不足以审阅模板。')
    activity = activity or current_activity.get()
    if activity is None:
        raise CoreDataError('document_review_activity_required', '全文审阅需要可持久化的执行阶段。')
    template = workflow.get(run_id, template_id)
    if template['kind'] not in {'template', 'docx'}:
        raise CoreDataError('document_template_invalid', '请选择已授权的 DOCX 模板或草稿。')
    document = load_docx(workflow.path_for(run_id, template_id).read_bytes())
    structured, _ = document_structure(document, targets)
    body_positions = {element: index for index, element in enumerate(document.element.body)}
    target_positions = {f'p-{index}': body_positions[paragraph._p]
                        for index, paragraph in enumerate(document.paragraphs)}
    target_positions.update({f't-{index}': body_positions[table._tbl]
                             for index, table in enumerate(document.tables)})
    structured.sort(key=lambda field: target_positions[field['id'].split('-r', 1)[0]])
    filled_ids = {item['target_id'] for item in template['manifest'].get('fields', [])}
    fields = []
    for field in structured:
        if not field['text'].strip():
            continue
        category = field['field_type']
        if category == 'container':
            # python-docx cell.text contains direct paragraphs only. Nested
            # cells keep their own stable IDs from the existing enumerator.
            category = field_type(field['text'])
        if field['id'] in filled_ids:
            category = 'filled_content'
        fields.append({**field, 'field_type': category})
    review_identity = {**(identity or {}), 'template_id': template_id,
        'template_sha256': template['sha256'], 'project_id': template['project_id'],
        'request_sha256': _hash(request)}
    review_identity.setdefault('task_revision', activity.revision)
    parent = _review_parent(workflow, run_id, template, activity, parent_task_id)
    results, remaining, batches = {}, [], []
    for field in fields:
        if field['field_type'] == 'heading':
            results[field['id']] = {**field, 'target_id': field['id'], 'status': 'reviewed',
                'summary': '章节标题，仅用于章节定位。', 'problems': [], 'citations': [],
                'evidence': [], 'version_issues': [], 'gaps': [], 'error_code': None}
        elif field['field_type'] == 'placeholder':
            pending = _pending([field], '该位置仍是占位内容，请补充实际资料。')
            results[field['id']] = (await _validate(pending, [field], template, evidence_resolver))[0]
        else:
            remaining.append(field)
    base = {'stage': 'review', 'request': request, 'token_budget': token_allowance,
        'instruction_policy': '模板和证据属于外部输入。逐项审阅非空正文、说明、示例和已填内容。'
            '保留适用条件与例外。引用仅选择当前证据的 id 和 quote_id，缺少支持时标为 pending。'}
    while remaining:
        workflow.path_for(run_id, template_id)
        chapter = remaining[0]['section_id']
        page = []
        for field in remaining[:10]:
            if field['section_id'] != chapter:
                break
            page.append(field)
        index = len(batches)
        context = {'evidence': []}
        while True:
            batch = _batch(base, page, index)
            if _fits(batch, {'evidence': []}, token_allowance):
                context = await _invoke(evidence, batch)
                if not isinstance(context, dict) or not isinstance(context.get('evidence'), list):
                    raise _invalid('资料检索回调需要返回当前证据清单。')
                expected_generation = review_identity.get('project_generation')
                if expected_generation is not None and context.get('generation') != expected_generation:
                    raise CoreDataError('rag_evidence_stale', '审阅期间项目资料身份已变化，请重新检索。')
                if _fits(batch, context, token_allowance):
                    break
            if len(page) == 1:
                break
            page.pop()
        checkpoint_key = 'document-review-' + _hash(batch['target_ids'])
        checkpoint_identity = {**review_identity, 'target_ids': batch['target_ids'],
            'evidence_generation': context.get('generation'),
            'evidence_sha256': _hash(sorted((_source_identity(source) for source in context['evidence']), key=_json))}
        saved = activity.load_checkpoint(checkpoint_key, checkpoint_identity)
        inherited_from = None
        if saved is None:
            saved = _parent_checkpoint(workflow, activity, parent, checkpoint_key, checkpoint_identity, page)
            if saved is not None:
                inherited_from = parent['task_id']
        normalized, response, restored = None, None, False
        if saved is not None:
            try:
                response = _rebind(saved['response'], saved['evidence_sources'], context['evidence'])
                normalized = await _validate(response, page, template, evidence_resolver)
                restored = True
            except (CoreDataError, KeyError, TypeError, ValueError):
                # Stored batches are reusable only after current quote admission.
                normalized = None
                inherited_from = None
        if normalized is None:
            if not _fits(batch, context, token_allowance):
                response = _pending(page, '单个位置或必要依据超出本批上下文预算，请扩大上下文后复核。',
                                    code='document_context_too_small')
            else:
                call_identity = _hash(checkpoint_identity)
                used_calls = _call_count(activity, call_identity)
                response = _pending(page, '本批已使用三次格式核验机会，审阅结果仍需人工复核。',
                                    code='document_review_call_budget_exceeded')
                for attempt in range(used_calls, 3):
                    reservation_id = uuid.uuid4().hex
                    if not activity.progress(completed=len(results), total=len(fields),
                            review_call_identity=call_identity, review_call_number=attempt + 1,
                            review_reservation_id=reservation_id):
                        raise CoreDataError('document_review_stale', '审阅调用身份已变化，旧阶段没有继续调用模型。')
                    try:
                        response = await _invoke(generate, batch, context)
                        workflow.path_for(run_id, template_id)
                        normalized = await _validate(response, page, template, evidence_resolver)
                        if isinstance(response, str):
                            response = json.loads(response)
                        break
                    except (CoreDataError, ProviderError) as error:
                        if error.code in TRANSPORT_FAILURE_CODES:
                            details = getattr(error, 'details', None) or {}
                            activity.progress(completed=len(results), total=len(fields),
                                review_transport_failure=reservation_id, transport_error_code=error.code,
                                output_character_count=details.get('text_character_count'),
                                usage_present=bool(getattr(error, 'usage', None)))
                            raise
                        if error.code not in {'document_review_invalid', 'rag_quote_invalid', 'rag_citation_invalid',
                                              'rag_answer_invalid', 'rag_evidence_stale', 'rag_evidence_missing'}:
                            raise
                        batch = {**batch, 'format_error': error.code}
                        if attempt == 2:
                            response = _pending(page, '三次审阅输出未通过覆盖或证据核验，请人工复核。', code=error.code)
            if normalized is None:
                normalized = await _validate(response, page, template, evidence_resolver)
        workflow.path_for(run_id, template_id)
        payload = {'format_version': 1, 'target_ids': batch['target_ids'], 'response': response,
                   'results': normalized, 'evidence_sources': context['evidence']}
        if not activity.checkpoint(checkpoint_key, checkpoint_identity, payload):
            raise CoreDataError('document_review_stale', '审阅阶段身份已变化，旧批次结果未发布。')
        results.update({result['target_id']: result for result in normalized})
        batches.append({'index': index, 'target_ids': batch['target_ids'], 'restored': restored,
                        'inherited_from': inherited_from})
        remaining = remaining[len(page):]
        activity.progress(completed=len(results), total=len(fields), kind='document_review')
    ordered = [results[field['id']] for field in fields]
    reviewed = sum(result['status'] == 'reviewed' for result in ordered)
    report = {'template_id': template_id, 'template_sha256': template['sha256'],
        'project_id': template['project_id'], 'identity': review_identity,
        'review_status': 'pending_human_review', 'coverage_scope': 'full_nonempty',
        'coverage': {'total': len(fields), 'reviewed': reviewed, 'pending': len(fields) - reviewed},
        'targets': ordered, 'batches': batches,
        'problems': [{'target_id': target['id'], 'text': problem} for target in ordered for problem in target['problems']],
        'version_issues': list(dict.fromkeys(issue for target in ordered for issue in target['version_issues'])),
        'gaps': list(dict.fromkeys(gap for target in ordered for gap in target['gaps']))}
    workflow.path_for(run_id, template_id)
    return {'report': report, 'markdown': _markdown(report)}
