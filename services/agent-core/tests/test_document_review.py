import asyncio
import io
import json

import pytest
from docx import Document

from agent.document_review import review_template
from agent.documents import DocumentWorkflow
from agent.execution.execution_wait import ExecutionMonitor
from agent.providers import ProviderError
from agent.rag.grounding import RagGrounding, evidence_for_model, evidence_with_quotes, quote_options
from agent.storage import CoreDataError, CoreRepository


SOURCE = '申请人应提供试验报告。除另有规定外，应保存记录。'


class ReviewSources:
    def __init__(self, run_id, project_id):
        self.run_id, self.project_id = run_id, project_id
        self.generation = 4
        self.valid = True
        self.source = {'id': 'evidence-1', 'title': 'Rules', 'revision': 'A',
                       'text': SOURCE, 'clause_number': '1', 'version_id': 'version-1',
                       'original_hash': 'source-hash', 'locator': {'page': 2}}
        self.grounding = RagGrounding(self)
        self.calls = []
        self.resolved = []

    def assert_generation(self, project_id, generation):
        assert project_id == self.project_id
        if generation != self.generation:
            raise CoreDataError('rag_evidence_stale', '资料已变化')

    def validate_evidence(self, project_id, ids, run_id=None):
        assert project_id == self.project_id and run_id == self.run_id
        if not self.valid or ids != [self.source['id']]:
            raise CoreDataError('rag_evidence_stale', '依据失效')
        return [self.source]

    def evidence(self, batch):
        self.calls.append(batch['target_ids'])
        self.grounding.runs[self.run_id] = {
            'project_id': self.project_id, 'generation': self.generation,
            'query': '', 'subject': None, 'evidence': {self.source['id']: self.source},
            'admitted': {}, 'aliases': {}, 'quote_options': {},
        }
        self.grounding.admit(self.run_id, [self.source['id']])
        return {'evidence': [evidence_for_model(evidence_with_quotes(self.source))],
                'generation': self.generation}

    def resolve(self, text, citations):
        self.resolved.append({'run_id': self.run_id, 'citations': citations})
        return self.grounding.document_claim(self.run_id, text, citations)

    def response(self, batch, *, quote_id=None):
        return {'results': [{'target_id': field['id'], 'status': 'reviewed',
                'summary': self.source['text'], 'problems': [], 'evidence': [{'id': self.source['id'],
                  'quote_id': quote_id or quote_options(self.source['text'])[0]['quote_id']}],
                'version_issues': [], 'gaps': []} for field in batch['fields']]}


@pytest.fixture
def setup(tmp_path):
    core = CoreRepository(tmp_path / 'core.db')
    project = core.create_project('Review project', 'work')
    conversation = core.create_conversation('project_work', 'Review', project_id=project['id'])
    run = core.start_model_run(content='审阅全文非空内容', idempotency_key='review',
           provider_profile_id='provider', model_id='model', conversation_id=conversation['id'])
    workflow = DocumentWorkflow(core)
    monitor = ExecutionMonitor(core)
    root = monitor.start_task(run_id=run['id'], project_id=project['id'])
    return workflow, run, monitor, root, ReviewSources(run['id'], project['id'])


def register(workflow, run, document):
    buffer = io.BytesIO()
    document.save(buffer)
    return workflow.register_template(run['id'], buffer.getvalue(), 'review.docx')


def execute(setup, artifact, generate=None, *, identity=None, allowance=16000,
            parent_task_id=None, request='审阅全文非空内容', target_ids=None):
    workflow, run, monitor, root, sources = setup
    generate = generate or (lambda batch, evidence: sources.response(batch))
    async def operation(activity):
        return await review_template(workflow, run['id'], artifact['id'], request,
            generate, sources.evidence, sources.resolve, activity=activity,
            identity=identity or {'task_revision': 'intent-1', 'project_generation': sources.generation,
                                 'models': {'generator': 'model'}, 'config': {'route': 'local'}},
            token_allowance=allowance, parent_task_id=parent_task_id, target_ids=target_ids)
    return asyncio.run(monitor.execute_stage(root['id'], 'document_review', operation))


def test_selected_target_review_keeps_exact_scope_and_persists_coverage(setup):
    workflow, run, monitor, root, sources = setup
    document = Document()
    document.add_paragraph('第一位置')
    document.add_paragraph('第二位置')
    document.add_paragraph('第三位置')
    artifact = register(workflow, run, document)
    result = execute(setup, artifact, target_ids=['p-1'])
    assert [item['id'] for item in result['report']['targets']] == ['p-1']
    from agent.execution.task_plans import TaskPlans
    assert TaskPlans(workflow.repository).completion(root['id']) == {
        'ready': True, 'total': 1, 'completed': 1, 'gaps': 0, 'remaining': 0}


def test_review_includes_story_text_and_records_unreadable_objects(setup):
    workflow, run, _, root, sources = setup
    from docx.oxml import OxmlElement
    document = Document()
    document.add_heading('振动试验', 1)
    document.add_paragraph('技术正文')
    document.sections[0].header.paragraphs[0].text = '受控型号'
    drawing = OxmlElement('w:drawing')
    drawing.append(OxmlElement('a:blip'))
    document.paragraphs[1]._p.append(drawing)
    artifact = register(workflow, run, document)
    result = execute(setup, artifact)
    assert any(item['text'] == '受控型号' for item in result['report']['targets'])
    assert any(item.get('extraction_status') == 'requires_ocr_or_visual_review'
               and item['status'] == 'pending' for item in result['report']['targets'])
    assert any('p-0' in batch for batch in sources.calls)


def test_cross_chapter_review_reads_conflicting_facts_again_and_preserves_issues(setup):
    workflow, run, _, root, sources = setup
    document = Document()
    document.add_heading('第一章', 1)
    document.add_paragraph('设备型号 A1')
    document.add_heading('第二章', 1)
    document.add_paragraph('设备型号 B2')
    artifact = register(workflow, run, document)
    phases = []
    def generate(batch, context):
        cross_check = batch.get('review_phase') == 'consistency'
        phases.append(cross_check)
        response = sources.response(batch)
        for item, field in zip(response['results'], batch['fields']):
            item['facts'] = ([{'name': '设备型号', 'value': field['text'].split()[-1], 'unit': '', 'condition': ''}]
                             if field['field_type'] != 'heading' else [])
            if cross_check:
                assert {item['text'] for item in batch['fields']} == {'设备型号 A1', '设备型号 B2'}
                item['problems'] = ['设备型号前后不一致。']
                item['gaps'] = ['请核对实际设备型号。']
        return response
    result = execute(setup, artifact, generate)
    assert phases == [False, False, True]
    assert len(result['report']['consistency_checks']) == 2
    assert all('设备型号前后不一致。' in target['problems'] for target in result['report']['targets']
               if target['field_type'] != 'heading')


@pytest.mark.parametrize('locator,label', [
    ({'page': 2}, 'Rules · A · 1 · 物理页 2'),
    ({}, 'Rules · A · 1 · 物理页码未提供'),
])
def test_review_markdown_uses_application_source_labels(setup, locator, label):
    workflow, run, _, _, sources = setup
    sources.source['locator'] = locator
    document = Document()
    document.add_paragraph(SOURCE)
    artifact = register(workflow, run, document)
    result = execute(setup, artifact)
    assert f'[{label}](rag-evidence:evidence-1)' in result['markdown']


def test_model_classifies_authoring_guidance_without_treating_it_as_equipment_facts(setup):
    workflow, run, _, _, _ = setup
    document = Document()
    document.add_paragraph('本章节主要说明大纲编制依据和参考文件。')
    artifact = register(workflow, run, document)

    def generate(batch, evidence):
        return {'results': [{'target_id': item['id'], 'content_kind': 'instruction',
            'status': 'pending', 'summary': '这是供编写者使用的模板说明，正式稿应提供实际文件清单。',
            'problems': [], 'evidence': [], 'version_issues': [], 'gaps': ['实际文件清单待补充。']}
            for item in batch['fields']]}

    result = execute(setup, artifact, generate)
    target = result['report']['targets'][0]
    assert target['review_kind'] == 'instruction'
    assert target['field_type'] == 'content'
    assert target['evidence'] == []
    assert '编写说明' in result['markdown']


def test_every_review_batch_receives_document_wide_standard_references(setup):
    workflow, run, _, _, sources = setup
    document = Document()
    document.add_heading('编制依据', 1)
    document.add_paragraph('本模板拟按照 RTCA DO-160G 编制。')
    document.add_heading('振动试验', 1)
    for index in range(12):
        document.add_paragraph(f'检查位置 {index}')
    artifact = register(workflow, run, document)
    batches = []
    original_evidence = sources.evidence

    def verify_context(batch):
        assert batch['template_context'] == {'name': 'review.docx', 'referenced_standards': ['DO-160G']}

    def evidence(batch):
        verify_context(batch)
        return original_evidence(batch)

    def generate(batch, context):
        verify_context(batch)
        batches.append(batch)
        return sources.response(batch)

    sources.evidence = evidence
    result = execute(setup, artifact, generate)
    assert len(batches) == 3
    assert all('DO-160G' not in field['text'] for batch in batches[1:] for field in batch['fields'])
    assert result['report']['coverage']['total'] == 15


def test_review_schema_selects_only_current_targets_and_quote_identifiers():
    from agent.document_contracts import document_batch_schema
    schema = document_batch_schema({'stage': 'review',
        'fields': [{'id': 'p-2', 'field_type': 'instruction'}, {'id': 'p-3', 'field_type': 'example'}],
        'evidence': [{'id': 'source-1', 'quote_options': [{'quote_id': 'quote-1', 'text': '原文。'}]}]}, [])
    results = schema['properties']['response']['anyOf'][0]['properties']['results']
    assert results['minItems'] == results['maxItems'] == 2
    properties = results['items']['properties']
    assert properties['target_id']['enum'] == ['p-2', 'p-3']
    citation = properties['evidence']['items']['properties']
    assert citation['id']['enum'] == ['source-1']
    assert citation['quote_id']['enum'] == ['quote-1']


def test_review_schema_disallows_citations_when_no_evidence_is_available():
    from agent.document_contracts import document_batch_schema
    schema = document_batch_schema({'stage': 'review', 'fields': [{'id': 'p-2', 'field_type': 'content'}]}, [])
    results = schema['properties']['response']['anyOf'][0]['properties']['results']
    assert results['items']['properties']['evidence']['maxItems'] == 0


def test_review_fit_reserves_instruction_and_current_response_schema():
    from agent.context_engine.policy import estimate_tokens
    from agent.document_review import _fits

    batch = {'stage': 'review', 'fields': [{'id': 'p-1', 'field_type': 'content', 'text': '试验目的'}]}
    context = {'evidence': [{'id': 'evidence-1', 'quote_options': [
        {'quote_id': f'quote-{index}', 'text': '实际条款原文。'} for index in range(40)]}]}
    old_allowance = estimate_tokens({'batch': batch, 'evidence': context}) + 256
    assert not _fits(batch, context, old_allowance)


def test_schema_compatibility_retry_consumes_one_of_the_three_model_calls(setup):
    workflow, run, monitor, root, sources = setup
    document = Document()
    document.add_paragraph(SOURCE)
    artifact = register(workflow, run, document)
    calls = []

    def generate(batch, context):
        calls.append(batch.get('format_error'))
        if len(calls) == 1:
            raise ProviderError('response_format_unsupported', 'Schema unavailable')
        return sources.response(batch)

    result = execute(setup, artifact, generate)
    assert calls == [None, 'response_format_unsupported']
    assert result['report']['coverage']['reviewed'] == 1
    with workflow.repository._connect() as connection:
        count = connection.execute("SELECT COUNT(*) FROM execution_wait_activity WHERE task_id=? AND kind='progress' "
            "AND json_extract(payload_json,'$.review_call_number') IS NOT NULL", (root['id'],)).fetchone()[0]
    assert count == 2


def test_restored_batches_do_not_reset_repeated_failure_recovery_limit(setup):
    workflow, run, monitor, root, sources = setup
    document = Document()
    for index in range(11):
        document.add_paragraph(f'Requirement {index}')
    artifact = register(workflow, run, document)
    calls = []

    def generate(batch, context):
        calls.append(batch['index'])
        if batch['index'] == 0:
            return sources.response(batch)
        if calls.count(1) > 3:
            raise AssertionError('restored work reset the recovery limit')
        raise CoreDataError('runtime_crashed', 'Worker exited')

    async def scenario():
        async def operation(activity):
            return await review_template(workflow, run['id'], artifact['id'], 'review all',
                generate, sources.evidence, sources.resolve, activity=activity,
                identity={'task_revision': 'intent-1', 'project_generation': sources.generation})
        task = asyncio.create_task(monitor.execute_stage(root['id'], 'document_review', operation,
            safe_to_retry=True, recoverable=lambda error: isinstance(error, CoreDataError)
                and error.code == 'runtime_crashed'))
        try:
            with pytest.raises(CoreDataError) as failure:
                await task
            assert failure.value.code == 'runtime_crashed'
            current = monitor.get_task(root['id'])
            assert current['auto_recoveries'] == 2
            assert calls == [0, 1, 1, 1]
            assert current['status'] == 'failed'
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_inventory_covers_nonempty_nested_targets_and_duplicate_chapters_without_source_write(setup):
    workflow, run, _, _, sources = setup
    document = Document()
    document.add_heading('重复章节', 1)
    document.add_paragraph('说明：核对条件。')
    document.add_paragraph('示例：设备A。')
    document.add_paragraph('正文要求。')
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = '外层文字'
    table.cell(0, 0).add_table(rows=1, cols=1).cell(0, 0).text = '内层文字'
    table.cell(0, 1).text = ' \t\u3000\n'
    document.add_heading('重复章节', 1)
    document.add_paragraph('另一章节正文。')
    artifact = register(workflow, run, document)
    path = workflow.path_for(run['id'], artifact['id'])
    original = path.read_bytes()
    calls = []

    def generate(batch, evidence):
        calls.append(batch)
        assert len({field['section_id'] for field in batch['fields']}) == 1
        return sources.response(batch)

    result = execute(setup, artifact, generate)
    targets = result['report']['targets']
    texts = [item['original_content'].strip() for item in targets]
    assert texts.count('外层文字') == 1
    assert texts.count('内层文字') == 1
    assert texts.count('重复章节') == 2
    assert texts.index('外层文字') < texts.index('另一章节正文。')
    assert len(targets) == 8
    assert {item['field_type'] for item in targets} >= {'heading', 'instruction', 'example', 'content'}
    assert len({item['section_id'] for item in targets if item['field_type'] == 'heading'}) == 2
    assert all(item['status'] in {'reviewed', 'pending'} for item in targets)
    assert result['report']['coverage'] == {'total': 8, 'reviewed': 8, 'pending': 0}
    assert '全文非空内容审阅' in result['markdown']
    assert path.read_bytes() == original


def test_batches_have_at_most_ten_targets_and_shrink_for_context_with_evidence(setup):
    from agent.context_engine.policy import estimate_tokens
    from agent.document_review import review_request
    workflow, run, _, _, sources = setup
    sources.source['text'] = SOURCE * 12
    document = Document()
    document.add_heading('要求', 1)
    for index in range(23):
        document.add_paragraph(f'{index} ' + '要求文本' * 20)
    artifact = register(workflow, run, document)
    sizes, costs = [], []

    def generate(batch, evidence):
        sizes.append(len(batch['fields']))
        messages, schema = review_request(batch, evidence)
        costs.append(estimate_tokens({'messages': messages, 'response_schema': schema, 'tools': []}) + 256)
        return sources.response(batch)

    result = execute(setup, artifact, generate, allowance=2000)
    assert max(sizes) < 10
    assert max(costs) <= 2000
    assert result['report']['coverage']['total'] == 24
    assert result['report']['coverage']['pending'] == 0
    assert len(sources.calls) == len(sizes)


def test_two_committed_batches_restore_after_restart_without_regeneration(setup):
    workflow, run, monitor, root, sources = setup
    document = Document()
    for index in range(25):
        document.add_paragraph(f'条目{index}')
    artifact = register(workflow, run, document)
    calls = []

    def interrupted(batch, evidence):
        calls.append(batch['target_ids'])
        if len(calls) == 3:
            raise RuntimeError('process interrupted')
        return sources.response(batch)

    with pytest.raises(RuntimeError, match='process interrupted'):
        execute(setup, artifact, interrupted)
    assert len(calls[0]) == len(calls[1]) == 10
    monitor.interrupt_active()
    current = monitor.get_task(root['id'])
    monitor.control(root['id'], 'retry', revision=current['revision'], idempotency_key='restart')
    resumed = []

    def generate(batch, evidence):
        resumed.append(batch['target_ids'])
        return sources.response(batch)

    result = execute(setup, artifact, generate)
    assert resumed == [calls[2]]
    assert [batch['restored'] for batch in result['report']['batches']] == [True, True, False]
    assert result['report']['coverage'] == {'total': 25, 'reviewed': 25, 'pending': 0}


@pytest.mark.parametrize('failure', ['quote', 'coverage', 'status'])
def test_three_invalid_actual_calls_end_with_explicit_pending_coverage(setup, failure):
    workflow, run, _, _, sources = setup
    document = Document()
    document.add_paragraph('需要审阅的内容')
    artifact = register(workflow, run, document)
    calls = []

    def generate(batch, evidence):
        calls.append(batch)
        if failure == 'coverage':
            return {'results': []}
        response = sources.response(batch, quote_id='forged' if failure == 'quote' else None)
        if failure == 'status':
            response['results'][0]['status'] = []
        return response

    result = execute(setup, artifact, generate)
    assert len(calls) == 3
    assert calls[1]['format_error']
    assert result['report']['coverage'] == {'total': 1, 'reviewed': 0, 'pending': 1}
    assert result['report']['targets'][0]['gaps']
    assert result['report']['targets'][0]['evidence'] == []


def test_checkpoint_identity_changes_recompute_batches_and_invalidated_quotes_are_rechecked(setup):
    workflow, run, _, _, sources = setup
    document = Document()
    document.add_paragraph('正文')
    artifact = register(workflow, run, document)
    execute(setup, artifact)
    calls = []

    def generate(batch, evidence):
        calls.append(batch)
        return sources.response(batch)

    changed = {'task_revision': 'intent-1', 'project_generation': 4,
               'models': {'generator': 'replacement'}, 'config': {'route': 'local'}}
    assert execute(setup, artifact, generate, identity=changed)['report']['batches'][0]['restored'] is False
    assert len(calls) == 1
    sources.valid = False
    with pytest.raises(CoreDataError) as stale:
        execute(setup, artifact, generate, identity=changed)
    assert stale.value.code == 'rag_evidence_stale'


def test_unsupported_summary_stays_pending_human_review(setup):
    workflow, run, _, _, sources = setup
    document = Document()
    document.add_paragraph('未经支持的原文')
    artifact = register(workflow, run, document)

    def generate(batch, evidence):
        response = sources.response(batch)
        response['results'][0]['summary'] = '已证明结构完整性和耐久性满足所有适航要求。'
        return response

    result = execute(setup, artifact, generate)
    assert result['report']['coverage']['pending'] == 1
    assert result['report']['review_status'] == 'pending_human_review'
    assert result['report']['targets'][0]['gaps']


def test_oversized_target_and_nonempty_placeholder_are_explicit_pending_without_model_calls(setup):
    workflow, run, _, _, sources = setup
    document = Document()
    document.add_paragraph('内容' * 10000)
    document.add_paragraph('待填写')
    artifact = register(workflow, run, document)

    def generate(batch, evidence):
        pytest.fail('Oversized input must stay pending before invoking the model')

    result = execute(setup, artifact, generate, allowance=4000)
    assert result['report']['coverage'] == {'total': 2, 'reviewed': 0, 'pending': 2}


def test_source_tampering_during_generation_stops_publication(setup):
    workflow, run, _, _, sources = setup
    document = Document()
    document.add_paragraph('原文')
    artifact = register(workflow, run, document)
    path = workflow.path_for(run['id'], artifact['id'])

    def generate(batch, evidence):
        path.write_bytes(b'changed')
        return sources.response(batch)

    with pytest.raises(CoreDataError) as error:
        execute(setup, artifact, generate)
    assert error.value.code == 'document_artifact_changed'


def test_restored_batch_rebinds_current_evidence_ids_and_filled_content_is_classified(setup):
    workflow, run, _, _, sources = setup
    document = Document()
    document.add_paragraph('待填写')
    template = register(workflow, run, document)
    artifact = workflow.fill(run['id'], template['id'], [{'target_id': 'p-0', 'text': '已填写内容', 'evidence': []}])
    assert execute(setup, artifact)['report']['targets'][0]['field_type'] == 'filled_content'
    sources.source = {**sources.source, 'id': 'evidence-2'}
    original_evidence = sources.evidence

    def current_evidence(batch):
        context = original_evidence(batch)
        state = sources.grounding.runs[run['id']]
        state['evidence'] = {'evidence-2': sources.source}
        sources.grounding.admit(run['id'], ['evidence-2'])
        context['evidence'][0]['id'] = 'evidence-2'
        return context

    sources.evidence = current_evidence
    sources.validate_evidence = lambda project_id, ids, run_id=None: [sources.source]

    def generate(batch, evidence):
        pytest.fail('A re-admitted identical source should reuse the reviewed batch')

    result = execute(setup, artifact, generate)
    assert result['report']['batches'][0]['restored'] is True
    assert result['report']['targets'][0]['evidence'][0]['id'] == 'evidence-2'


def test_format_call_budget_survives_interruption_inside_batch(setup):
    workflow, run, _, _, sources = setup
    document = Document()
    document.add_paragraph('正文')
    artifact = register(workflow, run, document)
    calls = []

    def interrupted(batch, evidence):
        calls.append(batch)
        if len(calls) == 3:
            raise RuntimeError('interrupted during third model call')
        return {'results': []}

    with pytest.raises(RuntimeError):
        execute(setup, artifact, interrupted)

    def generate(batch, evidence):
        pytest.fail('The interrupted batch has already consumed three actual calls')

    result = execute(setup, artifact, generate)
    assert result['report']['coverage'] == {'total': 1, 'reviewed': 0, 'pending': 1}
    assert result['report']['targets'][0]['error_code'] == 'document_review_call_budget_exceeded'


def cancelled_review(setup, *, status='cancelled'):
    workflow, run, monitor, root, sources = setup
    document = Document()
    for index in range(25):
        document.add_paragraph(f'条目{index}')
    artifact = register(workflow, run, document)
    original_evidence = sources.evidence
    generated = []

    async def evidence(batch):
        if batch['index'] == 2:
            if status == 'interrupted':
                monitor.interrupt_active()
            else:
                current = monitor.get_task(root['id'])
                monitor.control(root['id'], 'cancel', revision=current['revision'], idempotency_key='cancel-review')
            await asyncio.Event().wait()
        return original_evidence(batch)

    def generate(batch, context):
        generated.append(batch['target_ids'])
        return sources.response(batch)

    sources.evidence = evidence
    try:
        with pytest.raises(asyncio.CancelledError):
            execute(setup, artifact, generate)
    finally:
        sources.evidence = original_evidence
    assert len(generated) == 2
    assert monitor.get_task(root['id'])['status'] == status
    workflow.repository.transition_model_run(run['id'], {'queued'}, status)
    return artifact


def linked_review(setup, artifact):
    workflow, run, monitor, root, sources = setup
    linked = workflow.repository.create_expanded_retry(run['id'])
    linked_artifact = workflow.register_template(linked['id'],
        workflow.path_for(run['id'], artifact['id']).read_bytes(), 'review.docx')
    linked_root = monitor.start_task(run_id=linked['id'], project_id=root['project_id'])
    linked_sources = ReviewSources(linked['id'], sources.project_id)
    linked_sources.source = {**sources.source, 'id': 'current-evidence'}
    return (workflow, linked, monitor, linked_root, linked_sources), linked_artifact


def parent_audit(setup):
    workflow, _, _, root, _ = setup
    with workflow.repository._connect() as connection:
        audit = {'task': tuple(connection.execute(
            'SELECT * FROM execution_wait_tasks WHERE id=?', (root['id'],)).fetchone())}
        for table in ('stages', 'activity', 'checkpoints', 'controls'):
            audit[table] = [tuple(row) for row in connection.execute(
                f'SELECT * FROM execution_wait_{table} WHERE task_id=? ORDER BY rowid', (root['id'],))]
    return audit


@pytest.mark.parametrize('status', ['cancelled', 'interrupted'])
def test_cancelled_review_reuses_only_validated_batches_on_linked_retry_with_fresh_evidence(setup, status):
    artifact = cancelled_review(setup, status=status)
    before = parent_audit(setup)
    child, child_artifact = linked_review(setup, artifact)
    calls = []

    def generate(batch, context):
        calls.append(batch['target_ids'])
        return child[4].response(batch)

    result = execute(child, child_artifact, generate, parent_task_id=setup[3]['id'])
    assert calls == [['p-20', 'p-21', 'p-22', 'p-23', 'p-24']]
    assert [item['restored'] for item in result['report']['batches']] == [True, True, False]
    assert [item.get('inherited_from') for item in result['report']['batches']] == [setup[3]['id'], setup[3]['id'], None]
    assert result['report']['coverage'] == {'total': 25, 'reviewed': 25, 'pending': 0}
    assert len(child[4].calls) == 3
    assert len(child[4].resolved) == 25
    assert all(item['run_id'] == child[1]['id'] for item in child[4].resolved)
    assert all(source['id'] == 'current-evidence' for target in result['report']['targets'] for source in target['evidence'])
    assert parent_audit(setup) == before


def test_linked_retry_recomputes_technical_failure_and_reuses_successful_batch(setup):
    workflow, run, monitor, root, sources = setup
    document = Document()
    for index in range(11):
        document.add_paragraph(f'条目{index}')
    artifact = register(workflow, run, document)
    def invalid_second(batch, context):
        return sources.response(batch) if batch['index'] == 0 else {'results': []}
    failed = execute(setup, artifact, invalid_second)
    assert failed['report']['coverage']['pending'] == 1
    monitor.finish_task(root['id'], 'failed')
    workflow.repository.transition_model_run(run['id'], {'queued'}, 'failed')
    child, child_artifact = linked_review(setup, artifact)
    calls = []
    def generate(batch, context):
        calls.append(batch['target_ids'])
        return child[4].response(batch)
    result = execute(child, child_artifact, generate, parent_task_id=root['id'])
    assert calls == [['p-10']]
    assert result['report']['coverage'] == {'total': 11, 'reviewed': 11, 'pending': 0}


@pytest.mark.parametrize('changed', ['source', 'template', 'model', 'configuration', 'generation', 'request'])
def test_linked_review_recomputes_when_parent_identity_is_stale(setup, changed):
    artifact = cancelled_review(setup)
    child, child_artifact = linked_review(setup, artifact)
    identity = {'task_revision': 'intent-1', 'project_generation': 4,
                'models': {'generator': 'model'}, 'config': {'route': 'local'}}
    request = '审阅全文非空内容'
    if changed == 'source':
        child[4].source['text'] = '申请人应核对新的试验条件。'
    elif changed == 'template':
        document = Document()
        for index in range(25):
            document.add_paragraph(f'修改条目{index}')
        child_artifact = register(child[0], child[1], document)
    elif changed == 'model':
        identity['models'] = {'generator': 'replacement'}
    elif changed == 'configuration':
        identity['config'] = {'route': 'different'}
    elif changed == 'generation':
        child[4].generation = 5
        identity['project_generation'] = 5
    else:
        request = '审阅全文并核对额外条件'
    calls = []

    def generate(batch, context):
        calls.append(batch['target_ids'])
        return child[4].response(batch)

    result = execute(child, child_artifact, generate, identity=identity,
        parent_task_id=setup[3]['id'], request=request)
    assert len(calls) == 3
    assert all(item['restored'] is False for item in result['report']['batches'])


def test_linked_review_rejects_an_unrelated_parent_root(setup):
    artifact = cancelled_review(setup)
    child, child_artifact = linked_review(setup, artifact)
    workflow, _, monitor, _, _ = setup
    conversation = workflow.repository.create_conversation('project_work', 'Unrelated',
        project_id=setup[3]['project_id'])
    other = workflow.repository.start_model_run(content='Other request', idempotency_key='unrelated',
        provider_profile_id='provider', model_id='model', conversation_id=conversation['id'])
    unrelated = monitor.start_task(run_id=other['id'], project_id=setup[3]['project_id'])
    monitor.finish_task(unrelated['id'], 'cancelled')
    with pytest.raises(CoreDataError) as error:
        execute(child, child_artifact, parent_task_id=unrelated['id'])
    assert error.value.code == 'document_review_lineage_invalid'
    assert child[4].calls == []


def test_default_8k_review_budget_calls_model_for_all_substantive_batches(setup):
    workflow, run, _, _, sources = setup
    document = Document()
    document.add_heading('Review requirements', 1)
    for index in range(35):
        document.add_paragraph(f'Requirement {index} needs review.')
    artifact = register(workflow, run, document)
    sources.source['text'] = 'The applicant shall preserve applicable conditions and provide a controlled report. ' * 18
    calls = []

    def generate(batch, context):
        calls.append(batch['target_ids'])
        return sources.response(batch)

    result = execute(setup, artifact, generate, allowance=2730)
    assert len(calls) >= 4
    assert sum(map(len, calls)) == 36
    assert max(map(len, calls)) <= 10
    assert result['report']['coverage']['total'] == 36
    assert all(target['error_code'] != 'document_context_too_small' for target in result['report']['targets'])


@pytest.mark.parametrize('kind', ['network_unavailable', 'runtime_crashed', 'model_not_loaded'])
def test_confirmed_transport_failures_preserve_format_budget_and_actual_call_audit(setup, kind):
    workflow, run, _, root, sources = setup
    document = Document()
    document.add_paragraph('正文')
    artifact = register(workflow, run, document)
    calls = []

    def generate(batch, context):
        calls.append(batch)
        if len(calls) <= 3:
            error_type = ProviderError if kind == 'network_unavailable' else CoreDataError
            raise error_type(kind, 'confirmed transport failure')
        return sources.response(batch)

    for _ in range(3):
        with pytest.raises((ProviderError, CoreDataError)) as error:
            execute(setup, artifact, generate)
        assert error.value.code == kind
    result = execute(setup, artifact, generate)
    assert len(calls) == 4
    assert result['report']['coverage'] == {'total': 1, 'reviewed': 1, 'pending': 0}
    with workflow.repository._connect() as connection:
        records = [json.loads(row[0]) for row in connection.execute(
            "SELECT payload_json FROM execution_wait_activity WHERE task_id=? AND kind='progress'", (root['id'],))]
    reservations = [record for record in records if 'review_reservation_id' in record]
    failures = [record for record in records if 'review_transport_failure' in record]
    assert len(reservations) == 4
    assert len({record['review_reservation_id'] for record in reservations}) == 4
    assert len(failures) == 3


def test_bad_completed_outputs_stay_counted_when_transport_recovery_renews_attempts(setup):
    workflow, run, _, root, _ = setup
    document = Document()
    document.add_paragraph('正文')
    artifact = register(workflow, run, document)
    calls = []

    def generate(batch, context):
        calls.append(batch)
        if len(calls) in {2, 4}:
            raise ProviderError('provider_response_incomplete', 'transport output incomplete',
                                details={'text_character_count': 42})
        return {'results': []}

    for _ in range(2):
        with pytest.raises(ProviderError):
            execute(setup, artifact, generate)
    result = execute(setup, artifact, generate)
    assert len(calls) == 5
    assert result['report']['coverage']['pending'] == 1
    assert execute(setup, artifact, generate)['report']['batches'][0]['restored'] is True
    assert len(calls) == 5
    with workflow.repository._connect() as connection:
        failures = [json.loads(row[0]) for row in connection.execute(
            "SELECT payload_json FROM execution_wait_activity WHERE task_id=? AND json_extract(payload_json,'$.review_transport_failure') IS NOT NULL",
            (root['id'],))]
    assert len(failures) == 2
    assert all(item['output_character_count'] == 42 for item in failures)


def test_selected_review_report_labels_its_scope():
    from agent.document_review import _markdown

    report = {'coverage_scope': 'selected_targets', 'targets': [],
              'coverage': {'total': 1, 'reviewed': 1, 'pending': 0}}
    body = _markdown(report)
    assert body.startswith('# 选定位置审阅')
    assert '选定位置共 1 项' in body


def test_exact_clause_comparison_renders_verified_page_without_adding_support():
    from agent.document_review import comparison_markdown

    source = {'id': 'rag-1', 'title': 'DO-160F', 'revision': 'F', 'clause_number': '8.2.1.3',
              'locator': {'page': 46}, 'text': '8.2.1.3 Procedures\nUse a qualified laboratory and verified equipment.'}
    body = comparison_markdown('比对 DO-160F 第8.2.1.3条', [source], set())
    assert '已核验的对照引文' in body and '物理页 46' in body and 'rag-evidence:rag-1' in body
    assert '> Use a qualified laboratory and verified equipment' in body
    assert comparison_markdown('比对 DO-160F 第8.2.1.3条', [source], {'rag-1'}) == ''
    assert comparison_markdown('比对 DO-160F 第8.2.1.4条', [source], set()) == ''
    assert comparison_markdown('比对 DO-160F 第8.2.1.3条', [{**source, 'locator': {}}], set()) == ''


def test_selected_review_groups_sibling_sections_and_keeps_each_target_context(setup):
    from agent.document_review import review_request

    workflow, run, _, _, sources = setup
    document = Document()
    document.add_heading('Test procedures', 1)
    document.add_heading('Vibration', 2)
    document.add_heading('Purpose', 3)
    document.add_paragraph('Review the vibration test purpose.')
    document.add_heading('Installation', 3)
    document.add_paragraph('Review the vibration fixture installation.')
    document.add_heading('Temperature', 2)
    document.add_heading('Purpose', 3)
    document.add_paragraph('Review the temperature test purpose.')
    artifact = register(workflow, run, document)
    batches = []
    def generate(batch, context):
        batches.append(batch)
        messages, _ = review_request(batch, context)
        visible_batch = json.loads(messages[1]['content'])['batch']
        assert [field.get('chapter_path', visible_batch['chapter_path']) for field in visible_batch['fields']] == [
            field['chapter_path'] for field in batch['fields']]
        return sources.response(batch)
    result = execute(setup, artifact, generate, target_ids=['p-3', 'p-5', 'p-8'])
    assert [batch['target_ids'] for batch in batches] == [['p-3', 'p-5'], ['p-8']]
    assert [target['id'] for target in result['report']['targets']] == ['p-3', 'p-5', 'p-8']


def test_review_query_uses_actual_topic_and_preserves_explicit_clause_requests():
    from agent.document_review import review_evidence_query

    batch = {'chapter_path': ['通用试验条件'],
             'fields': [{'text': '试验场地的标准环境条件和温湿度要求。'}]}
    query = review_evidence_query('排除空白和案例，不要冒充版本，给出文件名和物理页码。', batch, 500)
    assert '温湿度' in query and '通用试验条件' in query
    assert '排除空白' not in query and '物理页码' not in query
    exact = review_evidence_query('请依据 DO-160F 第8.2.1.3条审阅。', batch, 500)
    assert '8.2.1.3' in exact and 'DO-160F' in exact and len(exact) <= 500


def test_review_correction_receives_and_persists_specific_validation_reason(setup):
    workflow, run, monitor, root, sources = setup
    document = Document()
    document.add_paragraph('设备工作时间10分钟。')
    artifact = register(workflow, run, document)
    calls = []

    def generate(batch, context):
        calls.append(batch)
        response = sources.response(batch)
        corrected = '审阅参数必须来自当前位置的原文。' in batch.get('format_error_message', '')
        response['results'][0]['facts'] = [
            {'name': '工作时间', 'value': '10' if corrected else '20', 'unit': '分钟', 'condition': ''}]
        return response

    result = execute(setup, artifact, generate)
    assert result['report']['coverage']['reviewed'] == 1
    assert result['report']['targets'][0]['facts'][0]['value'] == '10'
    assert len(calls) == 2
    assert calls[1]['format_error'] == 'document_review_invalid'
    with workflow.repository._connect() as connection:
        records = [json.loads(row['payload_json']) for row in connection.execute(
            "SELECT payload_json FROM execution_wait_activity WHERE task_id=? "
            "AND json_extract(payload_json,'$.review_validation_failure') IS NOT NULL", (root['id'],))]
    assert len(records) == 1
    failure = records[0]['review_validation_failure']
    assert failure['code'] == 'document_review_invalid' and failure['target_ids'] == ['p-0']
    assert '审阅参数必须来自当前位置的原文。' in failure['message']


def test_review_feedback_excludes_remote_provider_message(setup):
    workflow, run, monitor, root, sources = setup
    document = Document()
    document.add_paragraph(SOURCE)
    artifact = register(workflow, run, document)
    calls = []
    remote_message = 'remote credential=do-not-retain-this-provider-value'

    def generate(batch, context):
        calls.append(batch)
        if len(calls) == 1:
            raise ProviderError('response_format_unsupported', remote_message)
        assert remote_message not in batch['format_error_message']
        return sources.response(batch)

    result = execute(setup, artifact, generate)
    assert result['report']['coverage']['reviewed'] == 1
    assert len(calls) == 2
    with workflow.repository._connect() as connection:
        events = [row['payload_json'] for row in connection.execute(
            'SELECT payload_json FROM execution_wait_activity WHERE task_id=?', (root['id'],))]
    assert remote_message not in '\n'.join(events)
    assert any('review_validation_failure' in value for value in events)


def test_review_exhausted_validation_keeps_last_reason_without_extra_calls(setup):
    workflow, run, monitor, root, sources = setup
    document = Document()
    document.add_paragraph('设备工作时间10分钟。')
    artifact = register(workflow, run, document)
    calls = []

    def generate(batch, context):
        calls.append(batch)
        response = sources.response(batch)
        response['results'][0]['facts'] = [
            {'name': '工作时间', 'value': '20', 'unit': '分钟', 'condition': ''}]
        return response

    result = execute(setup, artifact, generate)
    assert len(calls) == 3
    assert result['report']['coverage'] == {'total': 1, 'reviewed': 0, 'pending': 1}
    target = result['report']['targets'][0]
    assert target['error_code'] == 'document_review_invalid'
    assert any('审阅参数必须来自当前位置的原文。' in value for value in target['gaps'])
    with workflow.repository._connect() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM execution_wait_activity WHERE task_id=? "
            "AND json_extract(payload_json,'$.review_validation_failure') IS NOT NULL", (root['id'],)).fetchone()[0]
    assert count == 3


@pytest.mark.parametrize('query_request,standards,subject,clauses', [
    ('依据 DO-160F 审阅，并与 DO-160G 比较。排除空白和示例，显示物理页码。',
     {'DO160F', 'DO160G'}, None, ()),
    ('重点检查关于振动的要求，排除空白和示例，显示物理页码。',
     set(), '振动', ()),
    ('排除空白和示例，显示物理页码。' * 40 +
     '依据 MIL-STD-810H 第514.8条，并比较 DO-160G。重点检查关于振动的要求。',
     {'MILSTD810H', 'DO160G'}, '振动', ('514.8',)),
], ids=['revisions', 'subject', 'trailing_constraints'])
def test_review_query_keeps_explicit_search_constraints_without_operation_noise(query_request, standards, subject, clauses):
    from agent.document_review import review_evidence_query
    from agent.rag.retrieval import _standard_ids, explicit_subject, explicit_clause_numbers

    batch = {'chapter_path': ['通用试验条件'], 'fields': [{'text': '原文温湿度要求。'}]}
    query = review_evidence_query(query_request, batch, 500)
    assert _standard_ids(query) == standards
    assert explicit_subject(query) == subject
    assert explicit_clause_numbers(query) == clauses
    assert '原文温湿度' in query
    assert '排除空白' not in query and '物理页码' not in query


def test_review_query_ignores_operational_subject_and_reserves_original_text():
    from agent.document_review import review_evidence_query
    from agent.rag.retrieval import explicit_subject, _standard_ids

    batch = {'chapter_path': ['通用试验条件' * 20], 'fields': [{'text': '原文关键字温湿度要求。' * 30}]}
    query = review_evidence_query('依据DO-160F并比较DO-160G，重点检查关于振动的要求。', batch, 80)
    assert len(query) <= 80 and '原文关键字' in query
    assert _standard_ids(query) == {'DO160F', 'DO160G'}
    assert explicit_subject(query) == '振动'
    noise = review_evidence_query('关于排除示例的要求，显示物理页码。', batch, 500)
    assert explicit_subject(noise) is None
    assert '排除示例' not in noise and '物理页码' not in noise


def test_review_recovers_last_validation_feedback_after_transport_interruption(setup):
    workflow, run, monitor, root, sources = setup
    document = Document()
    document.add_paragraph('设备工作时间10分钟。')
    artifact = register(workflow, run, document)
    calls = []

    def generate(batch, context):
        calls.append(batch)
        if len(calls) == 2:
            raise ProviderError('connection_reset', 'transport interruption')
        response = sources.response(batch)
        if len(calls) == 1:
            value = '20'
        else:
            assert batch['format_error'] == 'document_review_invalid'
            assert '审阅参数必须来自当前位置的原文。' in batch['format_error_message']
            value = '10'
        response['results'][0]['facts'] = [
            {'name': '工作时间', 'value': value, 'unit': '分钟', 'condition': ''}]
        return response

    with pytest.raises(ProviderError) as failure:
        execute(setup, artifact, generate)
    assert failure.value.code == 'connection_reset'
    result = execute(setup, artifact, generate)
    assert len(calls) == 3
    assert result['report']['coverage']['reviewed'] == 1
    assert result['report']['targets'][0]['facts'][0]['value'] == '10'


def test_review_fact_failure_identifies_target_for_specific_correction(setup):
    workflow, run, monitor, root, sources = setup
    document=Document()
    document.add_paragraph('设备工作时间10分钟。')
    document.add_paragraph('设备电压28伏。')
    artifact=register(workflow,run,document)
    calls=[]

    def generate(batch,context):
        calls.append(batch)
        message=batch.get('format_error_message','')
        corrected='p-1' in message and 'facts' in message and '[]' in message
        response=sources.response(batch)
        for row in response['results']:
            value = '10' if row['target_id'] == 'p-0' else ('28' if corrected else '30')
            row['facts'] = [{'name': '时间' if row['target_id'] == 'p-0' else '电压', 'value': value, 'unit': '', 'condition': ''}]
        return response

    result=execute(setup,artifact,generate)
    assert result['report']['coverage']['reviewed']==2
    assert len(calls)==2
    assert result['report']['targets'][1]['facts'][0]['value']=='28'


def test_review_fact_feedback_identifies_all_invalid_targets_in_one_retry(setup):
    workflow, run, monitor, root, sources = setup
    document = Document()
    for text in ('设备工作时间10分钟。', '设备电压28伏。', '设备频率50赫兹。'):
        document.add_paragraph(text)
    artifact = register(workflow, run, document)
    calls = []

    def generate(batch, context):
        calls.append(batch)
        message = batch.get('format_error_message', '')
        corrected = 'p-1' in message and 'p-2' in message and 'facts' in message and '[]' in message
        response = sources.response(batch)
        values = ['10', '28' if corrected else '30', '50' if corrected else '60']
        for result in response['results']:
            index = int(result['target_id'].split('-')[1])
            name, value = ['工作时间', '电压', '频率'][index], values[index]
            result['facts'] = [{'name': name, 'value': value, 'unit': '', 'condition': ''}]
        return response

    result = execute(setup, artifact, generate)
    assert result['report']['coverage']['reviewed'] == 3
    assert len(calls) == 2
    assert [target['facts'][0]['value'] for target in result['report']['targets']] == ['10', '28', '50']


@pytest.mark.parametrize('shared_quote', [False, True])
def test_review_schema_binds_each_quote_to_its_own_evidence(shared_quote):
    from agent.document_contracts import document_batch_schema
    sources = [
        {'id': 'source-a', 'quote_options': [{'quote_id': 'q-a'}, {'quote_id': 'q-shared'}]},
        {'id': 'source-b', 'quote_options': [{'quote_id': 'q-b'}] +
         ([{'quote_id': 'q-shared'}] if shared_quote else [])},
        {'id': 'source-without-quotes', 'quote_options': []},
    ]
    schema = document_batch_schema({'stage': 'review', 'fields': [{'id': 'p-0', 'field_type': 'content'}],
                                    'evidence': sources}, [])
    result = schema['properties']['response']['anyOf'][0]['properties']['results']['items']
    citation = result['properties']['evidence']['items']
    # Each object alternative constrains both properties together. A single
    # object with global enums instead permits their incorrect cross product.
    alternatives = citation.get('anyOf', [citation])
    allowed = {(source_id, quote_id) for option in alternatives
               for source_id in option['properties']['id']['enum']
               for quote_id in option['properties']['quote_id']['enum']}
    expected = {(source['id'], quote['quote_id']) for source in sources for quote in source['quote_options']}
    assert allowed == expected
    assert ('source-a', 'q-b') not in allowed
    assert not any(source_id == 'source-without-quotes' for source_id, _ in allowed)


def test_review_quote_feedback_identifies_all_targets_and_pairing_rule(setup):
    workflow, run, monitor, root, sources = setup
    document = Document()
    for text in ('试验要求一', '试验要求二', '试验要求三'):
        document.add_paragraph(text)
    artifact = register(workflow, run, document)
    calls = []

    def generate(batch, context):
        calls.append(batch)
        message = batch.get('format_error_message', '')
        corrected = all(word in message for word in ('p-1', 'p-2', 'quote_options', 'quote_id'))
        response = sources.response(batch)
        if not corrected:
            for result in response['results'][1:]:
                result['evidence'][0]['quote_id'] = 'other-source-quote'
        return response

    result = execute(setup, artifact, generate)
    assert result['report']['coverage']['reviewed'] == 3
    assert len(calls) == 2
    assert all(not target.get('error_code') for target in result['report']['targets'])



@pytest.mark.parametrize('invalid_quote', [False, True])
def test_review_report_distinguishes_processed_pending_from_technical_failure(setup, invalid_quote):
    workflow, run, monitor, root, sources = setup
    document = Document()
    document.add_paragraph('本节描述试验要求。')
    artifact = register(workflow, run, document)

    def generate(batch, context):
        response = sources.response(batch)
        if invalid_quote:
            response['results'][0]['evidence'][0]['quote_id'] = 'invalid'
        else:
            response['results'][0]['evidence'] = []
            response['results'][0]['gaps'] = ['未找到直接支持的摘录，专业结论待核实。']
        return response

    result = execute(setup, artifact, generate)
    assert result['report']['coverage'] == {'total': 1, 'reviewed': 0, 'pending': 1}
    if invalid_quote:
        assert '已处理 0 项' in result['markdown']
        assert '技术待处理 1 项' in result['markdown']
    else:
        assert '已处理 1 项' in result['markdown']
        assert '专业待复核 1 项' in result['markdown']
        assert '技术待处理 0 项' in result['markdown']



def test_consistency_technical_failure_remains_technical_pending(setup):
    workflow, run, monitor, root, sources = setup
    document = Document()
    document.add_heading('第一章', 1)
    document.add_paragraph('设备型号 A1')
    document.add_heading('第二章', 1)
    document.add_paragraph('设备型号 B2')
    artifact = register(workflow, run, document)

    def generate(batch, context):
        response = sources.response(batch)
        for result, field in zip(response['results'], batch['fields']):
            result['facts'] = [{'name': '设备型号', 'value': field['text'].split()[-1], 'unit': '', 'condition': ''}]
            if batch.get('review_phase') == 'consistency':
                result['evidence'][0]['quote_id'] = 'invalid'
        return response

    result = execute(setup, artifact, generate, target_ids=['p-1', 'p-3'])
    assert all(target['error_code'] == 'rag_quote_invalid' for target in result['report']['targets'])
    assert '已处理 0 项' in result['markdown']
    assert '技术待处理 2 项' in result['markdown']



def test_review_query_includes_only_recorded_local_anchor_context():
    from agent.document_review import review_evidence_query
    batch = {'chapter_path': ['附录C 故障记录表'], 'fields': [{
        'id': 't-7-r5-c5', 'text': '签名', 'table_context': {
            'table_id': 't-7',
            'nearest_nonempty_anchor_ids': {'left': 't-7-r5-c4'},
            'cells': [
                {'id': 't-7-r5-c0', 'text': '故障分析人员'},
                {'id': 't-7-r5-c4', 'text': '试验件供应商技术负责人'},
                {'id': 't-7-r5-c8', 'text': '试验件供应商质量负责人'}]}}]}
    query = review_evidence_query('依据DO-160F比较DO-160G。', batch, 500)
    assert '签名' in query and '试验件供应商技术负责人' in query
    assert '故障分析人员' not in query and '试验件供应商质量负责人' not in query
    assert 't-7-r5-c4' in query and len(query) <= 500


def test_review_query_anchor_context_is_bounded_and_keeps_original_topic():
    from agent.document_review import review_evidence_query
    batch = {'chapter_path': ['试验要求'], 'fields': [{
        'id': 't-2-r4-c2', 'text': '目标原文关键字' * 100, 'table_context': {
            'table_id': 't-2',
            'nearest_nonempty_anchor_ids': {'left': 't-2-r4-c1', 'above': 'missing', 'right': 't-9-r4-c3'},
            'cells': [{'id': 't-2-r4-c1', 'text': '局部标签' * 1000},
                      {'id': 't-2-r0-c0', 'text': '遥远的表头'},
                      {'id': 't-9-r4-c3', 'text': '另一个表内容'}]}}]}
    query = review_evidence_query('依据DO-160F第4.6.2条审阅。', batch, 160)
    assert len(query) <= 160 and '目标原文关键字' in query and '局部标签' in query
    assert 'DO-160F' in query and '4.6.2' in query
    assert '遥远的表头' not in query and '另一个表内容' not in query


def test_table_review_instruction_covers_local_relations_and_all_output_sections():
    from agent.document_review import review_request
    batch = {'fields': [{'id': 't-0-r0-c1', 'text': '签名', 'table_context': {
        'nearest_nonempty_anchor_ids': {'left': 't-0-r0-c0'},
        'cells': [{'id': 't-0-r0-c0', 'text': '指定签署岗位'}]}}], 'target_ids': ['t-0-r0-c1']}
    messages, _ = review_request(batch, {'evidence': []}, structured=False)
    instruction = messages[0]['content']
    assert 'nearest_nonempty_anchor_ids' in instruction
    assert 'summary, problems and gaps' in instruction
    assert 'mandatory' in instruction and 'labels' in instruction


def test_one_unsupported_quantity_keeps_eight_verified_steps(setup):
    workflow, run, _, root, sources = setup
    document = Document()
    for index in range(9):
        document.add_paragraph(f'低温试验步骤{index}，原文温度为-15℃。')
    artifact = register(workflow, run, document)
    calls = []

    def generate(batch, context):
        calls.append(batch['target_ids'])
        response = sources.response(batch)
        for row in response['results']:
            if row['target_id'] == 'p-7':
                row['summary'] = '根据标准要求，温度应为-15℃。'
        return response

    result = execute(setup, artifact, generate)
    targets = result['report']['targets']
    assert calls[0] == [f'p-{n}' for n in range(9)]
    assert len(calls) == 1
    assert sum(t['status'] == 'reviewed' for t in targets) == 8
    assert targets[7]['status'] == 'pending'
    assert targets[7]['evidence'] == [] and not targets[7].get('error_code')
    assert '量值' in ''.join(targets[7]['gaps'])
    from agent.execution.task_plans import TaskPlans
    assert TaskPlans(workflow.repository).completion(root['id'])['ready']


def test_partial_review_survives_interrupted_correction_and_rebinds_quotes(setup):
    workflow, run, _, root, sources = setup
    document = Document()
    for index in range(9):
        document.add_paragraph(f'步骤{index}。')
    artifact = register(workflow, run, document)
    calls = []

    def generate(batch, context):
        ids = batch['target_ids']
        calls.append(ids)
        if len(calls) == 2:
            assert ids == ['p-7'] and 'quote_id' in batch['format_error_message']
            from agent.execution.task_plans import TaskPlans
            assert TaskPlans(workflow.repository).completion(root['id'])['completed'] == 8
            raise ProviderError('connection_reset', 'interrupted')
        response = sources.response(batch)
        if len(calls) == 1:
            response['results'][7]['evidence'][0]['quote_id'] = 'unrelated-quote'
        return response

    with pytest.raises(ProviderError):
        execute(setup, artifact, generate)
    sources.source = {**sources.source, 'id': 'rebound-source'}
    result = execute(setup, artifact, generate)
    assert calls == [[f'p-{i}' for i in range(9)], ['p-7'], ['p-7']]
    assert result['report']['coverage']['reviewed'] == 9
    assert all(target['evidence'][0]['id'] == 'rebound-source' for target in result['report']['targets'])
    with workflow.repository._connect() as db:
        events = [json.loads(row[0]) for row in db.execute(
            "SELECT payload_json FROM execution_wait_activity WHERE task_id=? AND json_extract(payload_json,'$.public_document_event') IS NOT NULL",
            (root['id'],))]
    corrections = [e['public_document_event'] for e in events if e['public_document_event']['event'] == 'correcting']
    assert corrections and all([f['target_id'] for f in e['current']] == ['p-7'] for e in corrections)
    assert all(e['completed'] == 8 and e['correction']['attempt'] >= 2 for e in corrections)
