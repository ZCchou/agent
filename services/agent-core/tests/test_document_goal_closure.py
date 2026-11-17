import asyncio
import json
from types import SimpleNamespace

import pytest

from agent.execution.document_goal import normalize_goal, verified_extraction, resolve_extraction, example_goal
from agent.execution.document_goal_runtime import finalize
from agent.execution.compiler import InvocationCompiler
from agent.execution.source_inventory import SourceInventory
from agent.storage import CoreDataError
from test_task_plans import service
from test_unified_invocation_v3 import model_spec


def closure_fixture(tmp_path, monkeypatch):
    core, monitor, root, plans = service(tmp_path)
    fields = [{'id': f'p-{i}', 'text': text, 'original_content': text}
              for i, text in enumerate(['标题', '第一项', '第二项', '未请求项'])]
    store = SourceInventory(core)
    store.register(root['id'], 'source', 'template', 'hash', fields, complete=True)
    store.record_read(root['id'], 'source', 'hash', fields[:3])
    goals = plans.capture_requirements(root['id'], {'source_scopes': [{'source_id': 'source',
        'operation': 'read', 'scope': 'selected', 'target_ids': [],
        'selector': {'kind': 'prefix_nonempty', 'count': 3}, 'metadata_keys': ['total'], 'reason': '仅前三项'}],
        'success_criteria': ['原文提取，不审阅、不导出'], 'deliverables': []}, expected_revision=0)
    with core._connect() as db:
        db.execute('INSERT INTO task_goal_execution VALUES (?,?,?,?)', (root['id'], goals['revision'],
            goals['request_digest'], json.dumps({'answer_kind': 'extract'})))
    monkeypatch.setattr(core, 'get_run_conversation', lambda _: {'messages': [{'id': 'message',
        'content': '只提取前三项和总数，不导出', 'attachments': [{'id': 'source', 'sha256': 'hash'}]}]})
    monkeypatch.setattr(core, 'get_model_run', lambda _: {'status': 'generating'})
    calls, checks, events = [], [], []
    expected = {'template_id': 'template', 'total': 4,
                'fields': [{'id': f'p-{i}', 'text': f['text']} for i, f in enumerate(fields[:3])]}
    async def invoke(*args, **kwargs):
        calls.append(kwargs)
        return json.dumps(expected, ensure_ascii=False), None, {}
    async def emit(*args):
        events.append(args)
    svc = SimpleNamespace(repository=core, task_plans=plans,
        execution_monitor=SimpleNamespace(start_task=lambda **_: root, get_task=monitor.get_task),
        tool_loop=SimpleNamespace(repository=SimpleNamespace(list_for_run=lambda _: [])),
        rag_grounding=SimpleNamespace(runs={}),
        document_attachments=SimpleNamespace(_original=lambda a: checks.append(a['id'])),
        invocation_compiler=InvocationCompiler(),
        _create_model_invocation_spec=lambda *args, **kwargs: model_spec(max_output_tokens=kwargs['max_output_tokens']),
        _invoke_planned_step=invoke, _emit=emit)
    return svc, root, calls, checks, expected


def test_verified_scope_automatically_finalizes_without_business_tools(tmp_path, monkeypatch):
    svc, root, calls, checks, expected = closure_fixture(tmp_path, monkeypatch)
    text = asyncio.run(finalize(svc, {'id': 'run', 'user_message_id': 'message'}, 'attempt', 1, object(), {}, {'id': 'step'}))
    assert json.loads(text) == expected
    assert len(calls) == 1 and 'allowed_tools' not in calls[0]
    assert checks == ['source', 'source']  # before and after the model response
    with svc.repository._connect() as db:
        receipt = json.loads(db.execute('SELECT payload_json FROM task_goal_execution').fetchone()[0])['_verified_final']
    assert receipt['goal_revision'] == 1


def test_source_change_during_final_answer_cannot_publish(tmp_path, monkeypatch):
    svc, root, calls, checks, expected = closure_fixture(tmp_path, monkeypatch)
    async def changed(*args, **kwargs):
        svc.document_attachments._original = lambda _: (_ for _ in ()).throw(CoreDataError('attachment_changed', 'changed'))
        return json.dumps(expected), None, {}
    svc._invoke_planned_step = changed
    with pytest.raises(CoreDataError) as error:
        asyncio.run(finalize(svc, {'id': 'run', 'user_message_id': 'message'}, 'attempt', 1, object(), {}, {'id': 'step'}))
    assert error.value.code == 'attachment_changed'


def test_missing_proof_does_not_start_final_answer(tmp_path, monkeypatch):
    svc, root, calls, checks, _ = closure_fixture(tmp_path, monkeypatch)
    with svc.repository._connect() as db:
        db.execute('DELETE FROM task_read_proofs WHERE target_id=?', ('p-1',))
    assert asyncio.run(finalize(svc, {'id': 'run', 'user_message_id': 'message'}, 'attempt', 1, object(), {}, {'id': 'step'})) is None
    assert calls == []


def test_model_declared_scope_also_finalizes_without_initial_planner_row(tmp_path, monkeypatch):
    svc, root, calls, _, expected = closure_fixture(tmp_path, monkeypatch)
    with svc.repository._connect() as db:
        db.execute('DELETE FROM task_goal_execution WHERE task_id=?', (root['id'],))
    answer = asyncio.run(finalize(svc, {'id':'run','user_message_id':'message'},'attempt',1,object(),{}, {'id':'step'}))
    assert json.loads(answer) == expected
    assert len(calls) == 1


def test_project_template_fallback_does_not_treat_import_as_forbidden_attachment(tmp_path, monkeypatch):
    svc, root, calls, _, _ = closure_fixture(tmp_path, monkeypatch)
    with svc.repository._connect() as db:
        db.execute('DELETE FROM task_goal_execution WHERE task_id=?', (root['id'],))
    monkeypatch.setattr(svc.repository,'get_run_conversation',lambda _: {'messages':[{'id':'message','content':'处理项目模板','attachments':[]}]})
    assert asyncio.run(finalize(svc, {'id':'run','user_message_id':'message'},'attempt',1,object(),{}, {'id':'step'})) is None
    assert not calls


def test_revised_synthesis_goal_does_not_keep_extraction_answer_contract(tmp_path, monkeypatch):
    svc, root, calls, _, _ = closure_fixture(tmp_path, monkeypatch)
    before = svc.task_plans.goals.get(root['id'])
    revised = {key:before[key] for key in ('source_scopes','deliverables','success_criteria')}
    revised['success_criteria'] = ['分析这些已读取内容']
    svc.task_plans.capture_requirements(root['id'], revised, expected_revision=before['revision'], reconciled=True)
    async def invoke(*args, **kwargs):
        calls.append(kwargs)
        return '已分析读取结果。', None, {}
    svc._invoke_planned_step = invoke
    assert asyncio.run(finalize(svc, {'id':'run','user_message_id':'message'},'attempt',1,object(),{}, {'id':'step'})) == '已分析读取结果。'


def test_exact_single_source_wrapper_is_a_valid_extraction():
    source = {'template_id': 'template', 'total': 1, 'fields': [{'id': 'p-0', 'text': '原文。'}]}
    packet = {'sources': [source]}
    assert verified_extraction(json.dumps({'sources': [source]}), packet)
    with pytest.raises(CoreDataError):
        verified_extraction(json.dumps({**source, 'fields': [{'id': 'p-0', 'text': '原文'}]}), packet)


def test_position_references_fill_exact_original_but_never_repair_wrong_quotes():
    packet = {'sources': [{'template_id': 't', 'total': 1, 'fields': [{'id': 'p-0', 'text': ' 原文。\n'}]}]}
    answer = {'template_id': 't', 'total': 1, 'fields': [{'id': 'p-0'}]}
    assert json.loads(resolve_extraction(json.dumps(answer),packet))['fields'][0]['text'] == ' 原文。\n'
    assert json.loads(resolve_extraction(json.dumps({**answer,'fields':['p-0']}),packet))['fields'][0]['text'] == ' 原文。\n'
    for fields in [[{'id':'p-99'}], [{'id':'p-0'}, {'id':'p-0'}], [{'id':'p-0','text':'原文'}]]:
        with pytest.raises(CoreDataError):
            resolve_extraction(json.dumps({**answer,'fields':fields}),packet)


def test_goal_rejects_selector_that_does_not_match_declared_user_cardinality():
    value = example_goal('source')
    value['answer_kind'] = 'extract'
    scope = value['source_scopes'][0]
    scope.update(scope='selected',selector={'kind':'prefix_nonempty','count':2},requested_count=5)
    with pytest.raises(CoreDataError):
        normalize_goal(value,[{'id':'source'}])
    scope['selector']['count'] = 5
    assert normalize_goal(value,[{'id':'source'}])['source_scopes'][0]['selector']['count'] == 5
    scope.update(scope='full',selector=None)
    with pytest.raises(CoreDataError):
        normalize_goal(value,[{'id':'source'}])


def test_incomplete_document_failure_keeps_original_code_and_one_explanation(tmp_path, monkeypatch):
    from agent.runtime import ChatRunService
    from agent.execution.run_recovery import failure_notice
    svc, root, calls, _, _ = closure_fixture(tmp_path, monkeypatch)
    with svc.repository._connect() as db:
        db.execute("DELETE FROM task_read_proofs WHERE target_id='p-1'")
    monkeypatch.setattr(svc.repository,'get_run_conversation',lambda _: {'messages':[{'id':'message',
        'content':'读取附件','attachments':[{'id':'source','artifacts':[{'kind':'document.source'}]}]}]})
    saved = []
    monkeypatch.setattr(svc.repository,'checkpoint_draft',lambda *args:saved.append(args[-1]))
    body = '模型说明：规划未能完成，附件及请求已保留。'
    with pytest.raises(CoreDataError) as error:
        asyncio.run(ChatRunService._finish_task_goal(svc,{'id':'run','user_message_id':'message',
            '_response_failure_code':'task_goal_invalid'},'attempt',body+failure_notice('task_goal_invalid',[]),
            None,None,None,None,[],(),'native',False))
    assert error.value.code == 'task_goal_invalid' and error.value.explanation_saved
    assert saved == [body] and not calls


def test_unknown_chapter_selection_keeps_the_user_cardinality_for_resolution():
    goal = example_goal('source')
    goal['source_scopes'][0].update(scope='selected',selector={'kind':'selection_pending','condition':'第二章的前三项'},requested_count=3)
    requirement = normalize_goal(goal,[{'id':'source'}])['source_scopes'][0]
    assert requirement['selector'] == {'kind':'selection_pending','condition':'第二章的前三项','count':3}


def test_review_answer_cannot_declare_only_read_obligations():
    value = example_goal('source')
    value.update(answer_kind='review', evidence_required=True)
    with pytest.raises(CoreDataError) as error:
        normalize_goal(value, [{'id':'source'}])
    assert error.value.code == 'task_goal_invalid'



def test_exact_extraction_survives_bad_model_position_reference(tmp_path, monkeypatch):
    svc, root, calls, _, expected = closure_fixture(tmp_path, monkeypatch)

    async def bad_reference(*args, **kwargs):
        calls.append(kwargs)
        return '{"template_id":"wrong","total":0,"fields":[]}', None, {}

    svc._invoke_planned_step = bad_reference
    actual = asyncio.run(finalize(svc, {'id': 'run', 'user_message_id': 'message'},
        'attempt', 1, object(), {}, {'id': 'step'}))
    assert json.loads(actual) == expected



def test_full_nonempty_extract_exports_verified_source_without_model(tmp_path, monkeypatch):
    svc, root, calls, _, _ = closure_fixture(tmp_path, monkeypatch)
    from agent.execution.document_goal_runtime import export_complete_extraction

    goals = svc.task_plans.goals.get(root['id'])
    scope = {**goals['source_scopes'][0], 'scope': 'full', 'target_ids': []}
    scope.pop('selector', None)
    goals = {**goals, 'source_scopes': [scope], 'deliverables': [{'format': 'docx', 'path': None}]}
    store = SourceInventory(svc.repository)
    store.record_read(root['id'], 'source', 'hash',
        [{'id': 'p-3', 'text': '未请求项', 'original_content': '未请求项'}])
    current = {'id': 'message', 'content': '提取这个模板的非空内容并保存为新的 DOCX 文档。',
               'attachments': [{'id': 'source', 'name': 'fixture.docx', 'sha256': 'hash'}]}
    monkeypatch.setattr(svc.repository, 'get_run_conversation', lambda _: {'messages': [current]})
    exported = []
    async def publish(run, attempt, text, **kwargs):
        exported.append((text, kwargs))
        return '[打开文件](<提取.docx>)'
    svc._application_document_export = publish
    run = {'id': 'run', 'user_message_id': 'message'}
    out = asyncio.run(export_complete_extraction(svc, run, 'attempt', root['id'], goals, current))
    assert '4 个非空文字位置' in out and '[打开文件](<提取.docx>)' in out
    assert exported[0][0].splitlines() == ['标题', '第一项', '第二项', '未请求项']
    assert not calls
    assert asyncio.run(export_complete_extraction(svc, run, 'attempt', root['id'], goals,
        {**current, 'content': '提取非空内容并按照 DO-160F 核对条款，再保存为 DOCX。'})) is None



def test_builds_full_extraction_plan_from_explicit_attachment_request():
    from agent.execution.document_goal_runtime import full_extraction_plan
    current = {'content': '这个模板里，非空的内容有哪些，提取出来保存为新的 DOCX 文档。',
               'attachments': [{'id': 'source', 'name': '试验模板.docx'}]}
    draft = full_extraction_plan({}, current, current['attachments'])
    goal = normalize_goal(draft, current['attachments'])
    assert goal['source_scopes'][0]['scope'] == 'full'
    assert goal['deliverables'] == [{'format': 'docx', 'path': None}]
    for request in ['提取前三项非空原文，保存为新的 DOCX。',
                    '提取附件第三章的全部非空内容并保存为 DOCX。',
                    '提取全部非空内容并保存为 PDF 文件。',
                    '提取附件里的全部非空内容并保存为 Excel 文件。',
                    '提取非空内容，并按项目资料库核对，保存为新的 DOCX。',
                    '提取设备编号这一非空字段并保存为新的 DOCX。',
                    '想了解如何提取非空内容并导出为新的 DOCX。']:
        assert full_extraction_plan({}, {**current, 'content': request}, current['attachments']) is None
    assert full_extraction_plan({'rag_decision': {'requested_mode': 'required'}},
                                current, current['attachments']) is None



def test_extraction_exports_verified_text_with_only_known_visual_gaps(tmp_path, monkeypatch):
    svc, root, _, _, _ = closure_fixture(tmp_path, monkeypatch)
    from agent.execution.document_goal_runtime import export_complete_extraction

    current = {'id': 'message', 'content': '提取这个模板的全部非空内容并保存为新的 DOCX 文档。',
               'attachments': [{'id': 'source', 'name': 'fixture.docx', 'sha256': 'hash'}]}
    monkeypatch.setattr(svc.repository, 'get_run_conversation', lambda _: {'messages': [current]})
    store = SourceInventory(svc.repository)
    fields = [{'id': f'p-{i}', 'text': text, 'original_content': text}
              for i, text in enumerate(['标题', '第一项', '第二项', '未请求项'])]
    visual = {'id': 'word/document.xml#object-0', 'text': '', 'field_type': 'unreadable',
              'extraction_status': 'requires_ocr_or_visual_review', 'position': {'xml_position': 'object-0'}}
    store.register(root['id'], 'source', 'template', 'hash', [*fields, visual], complete=True)
    goals = svc.task_plans.goals.get(root['id'])
    scope = {**goals['source_scopes'][0], 'scope': 'full', 'target_ids': []}
    scope.pop('selector', None)
    goals = {**goals, 'source_scopes': [scope], 'deliverables': [{'format': 'docx', 'path': None}]}
    exported = []
    async def publish(run, attempt, text, **kwargs):
        exported.append(text)
        return text + '\n\n[打开文件](<提取.docx>)'
    svc._application_document_export = publish
    run = {'id': 'run', 'user_message_id': 'message'}
    assert asyncio.run(export_complete_extraction(svc, run, 'attempt', root['id'], goals, current)) is None
    assert not exported
    store.record_read(root['id'], 'source', 'hash', fields[-1:])
    reply = asyncio.run(export_complete_extraction(svc, run, 'attempt', root['id'], goals, current))
    assert '1 个图形' in reply and '[打开文件]' in reply
    assert exported[0].splitlines()[:4] == ['标题', '第一项', '第二项', '未请求项']
    assert 'word/document.xml#object-0' in exported[0]
