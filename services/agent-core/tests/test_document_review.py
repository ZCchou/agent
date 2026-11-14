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
            parent_task_id=None, request='审阅全文非空内容'):
    workflow, run, monitor, root, sources = setup
    generate = generate or (lambda batch, evidence: sources.response(batch))
    async def operation(activity):
        return await review_template(workflow, run['id'], artifact['id'], request,
            generate, sources.evidence, sources.resolve, activity=activity,
            identity=identity or {'task_revision': 'intent-1', 'project_generation': sources.generation,
                                 'models': {'generator': 'model'}, 'config': {'route': 'local'}},
            token_allowance=allowance, parent_task_id=parent_task_id)
    return asyncio.run(monitor.execute_stage(root['id'], 'document_review', operation))


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
    workflow, run, _, _, sources = setup
    document = Document()
    document.add_heading('要求', 1)
    for index in range(23):
        document.add_paragraph(f'{index} ' + '要求文本' * 20)
    artifact = register(workflow, run, document)
    sizes, costs = [], []

    def generate(batch, evidence):
        sizes.append(len(batch['fields']))
        costs.append(len(json.dumps({'batch': batch, 'evidence': evidence}, ensure_ascii=False, separators=(',', ':'))))
        return sources.response(batch)

    result = execute(setup, artifact, generate, allowance=1600)
    assert max(sizes) < 10
    assert max(costs) <= 4032
    assert result['report']['coverage']['total'] == 24
    assert result['report']['coverage']['pending'] == 0


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
    pending = execute(setup, artifact, generate, identity=changed)
    assert pending['report']['coverage']['pending'] == 1
    # The initial validated call and its later correction calls share one
    # identity, so the persisted three-call ceiling still applies.
    assert len(calls) == 3


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
    document.add_paragraph('内容' * 3000)
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
    assert sum(map(len, calls)) == 35
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
