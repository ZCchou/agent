from copy import deepcopy

import pytest

from agent.execution.document_goal import example_goal, normalize_goal
from agent.execution.document_goal_runtime import normalize_document_answer_draft, requested_document_answer_kind


COVER = '请只依据本次引用附件封面的原文，用一句话说明文档标题和文档类型；没有的信息标记未提供。不要使用项目资料库，不要导出文件。'


@pytest.mark.parametrize('prompt', [COVER,
    '请仅根据引用附件原文，简要介绍文档作者和文号。',
    '请只依据附件扉页说明标题及作者。不要导出。',
    'Only use the attachment to describe the document title and author.',
    '请仅根据附件第2节原文，简要回答哪些适用条件。不要导出。',
    '请只依据附件指定章节的原文，简短说明适用范围。',
    'Only use the attachment cover page to describe its title and document type in one sentence. Do not export.'])
def test_explicit_local_questions_replace_invented_full_obligation_with_pending_selection(prompt):
    draft = example_goal('source')
    draft['answer_kind'] = 'extract'
    draft['first_action']['limit'] = 1
    normalize_document_answer_draft(prompt, draft)
    assert draft['answer_kind'] == 'synthesis'
    scope = draft['source_scopes'][0]
    assert scope['scope'] == 'selected' and scope['target_ids'] == []
    assert scope['selector']['kind'] == 'selection_pending'
    assert scope['selector']['condition'] in prompt
    assert scope['requested_count'] is None
    assert draft['first_action']['limit'] == 1
    assert not draft['evidence_required']
    assert requested_document_answer_kind(prompt, 'extract') == 'synthesis'


@pytest.mark.parametrize('prompt', [
    '请阅读全文，再只依据封面，用一句话说明标题和类型。',
    '请只依据附件封面逐字提取标题，不要总结。',
    '请只依据附件封面，用一句话说明标题并导出文件。',
    '请只依据附件封面审阅合规性，用一句话说明结论。',
    '请只依据附件封面修改标题，用一句话说明改动。',
    '不要只依据附件封面，用一句话说明标题和类型。',
    '不只依据附件封面，用一句话说明标题和类型。',
    '并非只依据附件封面，用一句话说明标题和类型。',
    'Do not only use the cover page to describe the title in one sentence.',
])
def test_protected_or_negated_local_request_does_not_shrink_scope(prompt):
    draft = example_goal('source')
    draft['answer_kind'] = 'extract'
    original = deepcopy(draft)
    normalize_document_answer_draft(prompt, draft)
    assert draft == original


def test_actual_selected_positions_and_confirmed_scope_are_preserved():
    draft = example_goal('source')
    draft['answer_kind'] = 'extract'
    draft['source_scopes'][0].update(scope='selected', target_ids=['real-cover-heading'], selector=None)
    normalize_document_answer_draft(COVER, draft)
    assert draft['answer_kind'] == 'synthesis'
    assert draft['source_scopes'][0]['target_ids'] == ['real-cover-heading']
    confirmed = example_goal('source')
    confirmed['answer_kind'] = 'extract'
    original = deepcopy(confirmed)
    normalize_document_answer_draft(COVER, confirmed, confirmed_requirements={'provenance': 'user_clarification'})
    assert confirmed == original


def test_pending_local_scope_requires_real_selection_and_complete_read_proof(tmp_path):
    from agent.execution.source_inventory import SourceInventory
    from test_task_plans import service
    core, _, root, plans = service(tmp_path)
    draft = example_goal('source')
    draft['answer_kind'] = 'extract'
    normalize_document_answer_draft(COVER, draft)
    requirements = normalize_goal(draft, [{'id': 'source'}])
    fields = [{'id': 'real-cover-heading', 'text': 'Original test programme title',
               'original_content': 'Original test programme title'},
              {'id': 'unrequested-body', 'text': 'Unrequested body', 'original_content': 'Unrequested body'}]
    store = SourceInventory(core)
    store.register(root['id'], 'source', 'template', 'hash', fields, complete=True)
    plans.capture_requirements(root['id'], requirements, expected_revision=0)
    # Even a read of the actual title cannot silently resolve pending selection.
    store.record_read(root['id'], 'source', 'hash', fields[:1])
    assert not plans.completion(root['id'])['ready']
    selected = deepcopy(requirements)
    selected['source_scopes'][0].update(target_ids=['real-cover-heading'])
    selected['source_scopes'][0].pop('selector')
    plans.capture_requirements(root['id'], selected, expected_revision=1)
    assert store.coverage(root['id'], selected['source_scopes'][0])['ready']
    assert store.coverage(root['id'], {**selected['source_scopes'][0], 'scope': 'full', 'target_ids': []})['remaining'] == 1



def test_first_read_limit_and_invented_prefix_do_not_define_cover_scope():
    draft = example_goal('source')
    draft['answer_kind'] = 'extract'
    draft['first_action']['limit'] = 3
    draft['source_scopes'][0].update(scope='selected', requested_count=3,
        selector={'kind': 'prefix_nonempty', 'count': 3})
    normalize_document_answer_draft(COVER, draft)
    scope = draft['source_scopes'][0]
    assert scope['selector'] == {'kind': 'selection_pending', 'condition': COVER}
    assert scope['requested_count'] is None and scope['target_ids'] == []
    assert draft['first_action']['limit'] == 3


@pytest.mark.parametrize('ready', [False, True])
def test_cover_explanation_finalization_requires_coverage_before_one_sentence_path(tmp_path, monkeypatch, ready):
    import asyncio
    from hashlib import sha256
    from agent.execution.document_goal_runtime import finalize
    from test_document_goal_closure import closure_fixture
    svc, root, calls, checks, _ = closure_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(svc.repository, 'get_run_conversation', lambda _: {'messages': [{'id': 'message',
        'content': COVER, 'attachments': [{'id': 'source', 'sha256': 'hash'}]}]})
    with svc.repository._connect() as db:
        db.execute("UPDATE task_goal_execution SET payload_json=?", ('{"answer_kind":"synthesis"}',))
        if not ready:
            db.execute("DELETE FROM task_read_proofs WHERE target_id='p-2'")
    answer = '文档标题为试验大纲，文档类型为模板。'
    async def invoke(*args, **kwargs):
        calls.append(kwargs)
        return answer, None, []
    svc._invoke_planned_step = invoke
    run = {'id': 'run', 'user_message_id': 'message'}
    result = asyncio.run(finalize(svc, run, 'attempt', 1, object(), {}, {'id': 'step'}))
    if not ready:
        assert result is None and not calls
        assert '_verified_local_summary_hash' not in run
    else:
        assert result == answer and len(calls) == 1
        assert run['_verified_local_summary_hash'] == sha256(answer.encode()).hexdigest()
        assert checks == ['source', 'source']


@pytest.mark.parametrize('planned', ['extract', 'synthesis'])
def test_local_read_question_cannot_keep_invented_review_operation(planned):
    draft = example_goal('source')
    draft.update(answer_kind=planned, evidence_required=True)
    draft['source_scopes'][0]['operation'] = 'review'
    normalize_document_answer_draft(COVER, draft)
    assert draft['source_scopes'][0]['operation'] == 'read'
    assert not draft['evidence_required']


@pytest.mark.parametrize('guard', ['migrate', 'confirmed_history', 'digest', 'authority', 'fence', 'cache_failure'])
def test_cached_model_scope_migration_preserves_proofs_cache_and_protected_history(tmp_path, monkeypatch, guard):
    import json
    import sqlite3
    from agent.execution import document_goal_runtime as runtime
    from agent.storage import CoreDataError
    from test_document_goal_closure import closure_fixture
    svc, root, _, checks, _ = closure_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(svc.repository, 'get_run_conversation', lambda _: {'messages': [{'id': 'message',
        'content': COVER, 'attachments': [{'id': 'source', 'sha256': 'hash'}]}]})
    prior = svc.task_plans.goals.get(root['id'])
    draft = example_goal('source')
    draft['answer_kind'] = 'extract'
    requirements = normalize_goal(draft, [{'id': 'source'}])
    draft['_first_result_id'] = 'saved-original-read'
    prior = svc.task_plans.capture_requirements(root['id'], requirements, expected_revision=prior['revision'], reconciled=True)
    with svc.repository._connect() as db:
        db.execute('UPDATE task_goal_execution SET goal_revision=?,payload_json=?',
            (prior['revision'], json.dumps(draft)))
        if guard == 'confirmed_history':
            db.execute("UPDATE task_goal_requirements SET provenance='user_clarification' WHERE task_id=? AND revision=1", (root['id'],))
        if guard == 'cache_failure':
            db.execute("CREATE TRIGGER fail_cache BEFORE UPDATE ON task_goal_execution BEGIN SELECT RAISE(ABORT, 'simulated cache update interruption'); END")
        before_proofs = [tuple(row) for row in db.execute('SELECT * FROM task_read_proofs')]
        before_cache = dict(db.execute('SELECT * FROM task_goal_execution').fetchone())
    digest = svc.task_plans.goals.request_digest(root['id'])
    if guard == 'digest':
        digest = 'changed-request'
    if guard == 'authority':
        def forbidden(_):
            raise CoreDataError('attachment_reference_forbidden', 'Permission revoked')
        svc.document_attachments._original = forbidden
    if guard == 'fence':
        root = {**root, 'revision': root['revision'] + 1}
    if guard == 'cache_failure':
        with pytest.raises(sqlite3.IntegrityError, match='simulated cache update'):
            runtime.reconcile_cached_local_document_goal(svc, {'id': 'run', 'user_message_id': 'message'}, root,
                {'content': COVER, 'attachments': [{'id': 'source'}]}, prior, digest)
    elif guard in {'authority', 'fence'}:
        with pytest.raises(CoreDataError) as caught:
            runtime.reconcile_cached_local_document_goal(svc, {'id': 'run', 'user_message_id': 'message'}, root,
                {'content': COVER, 'attachments': [{'id': 'source'}]}, prior, digest)
        assert caught.value.code == ('attachment_reference_forbidden' if guard == 'authority' else 'task_plan_stale')
    else:
        changed = runtime.reconcile_cached_local_document_goal(svc, {'id': 'run', 'user_message_id': 'message'}, root,
            {'content': COVER, 'attachments': [{'id': 'source'}]}, prior, digest)
        assert changed == (guard == 'migrate')
    after = svc.task_plans.goals.get(root['id'])
    with svc.repository._connect() as db:
        cache = dict(db.execute('SELECT * FROM task_goal_execution').fetchone())
        assert [tuple(row) for row in db.execute('SELECT * FROM task_read_proofs')] == before_proofs
    if guard == 'migrate':
        assert after['revision'] == prior['revision'] + 1
        assert after['source_scopes'][0]['selector']['kind'] == 'selection_pending'
        assert not svc.task_plans.completion(root['id'])['ready']
        assert cache['goal_revision'] == after['revision']
        cached = json.loads(cache['payload_json'])
        assert cached['_first_result_id'] == 'saved-original-read'
        assert cached['answer_kind'] == 'synthesis'
        assert checks == ['source']
    else:
        assert after['revision'] == prior['revision']
        assert cache == before_cache


@pytest.mark.parametrize('prompt', [
    '请只依据附件封面说明标题，不要通读全文；但请阅读全文。',
    'Only use the attachment cover page to describe the title. Do not read the whole document. Read the entire document.',
    '请总结整篇文档，并只依据附件封面说明标题。',
    '请完整阅读附件，再只依据附件封面说明文档类型。'])
def test_explicit_whole_document_work_remains_full_even_with_a_local_subquestion(prompt):
    draft = example_goal('source')
    draft['answer_kind'] = 'extract'
    normalize_document_answer_draft(prompt, draft)
    assert draft['source_scopes'][0]['scope'] == 'full'


@pytest.mark.parametrize('prompt', [
    '请只依据附件封面用一句话说明标题和类型，不要通读全文。',
    '不要审阅全文，只依据附件封面用一句话说明标题和类型。',
    'Only use the attachment cover page to describe the title. Do not read the whole document.',
])
def test_negated_whole_document_read_does_not_hide_positive_local_scope(prompt):
    from agent.execution.document_goal_runtime import local_document_question_scope
    assert local_document_question_scope(prompt) == prompt
    draft = example_goal('source')
    draft['answer_kind'] = 'extract'
    normalize_document_answer_draft(prompt, draft)
    assert draft['answer_kind'] == 'synthesis'
    assert draft['source_scopes'][0]['selector']['kind'] == 'selection_pending'
