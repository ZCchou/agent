import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from agent.rag.grounding import RagGrounding
from agent.runtime import ChatRunService
from agent.storage import CoreDataError


QUESTION = '请依据当前项目资料库，用不超过100字说明减压试验适用的设备范围，并给出一条来源引用；仅回答这个问题，不审阅或修改文件。'


def candidate(prompt=QUESTION, **changes):
    service = ChatRunService.__new__(ChatRunService)
    service.skills = SimpleNamespace(requires_rag=lambda _: False, references=lambda text: {'skill'} if '$skill' in text else set())
    service.tool_loop = SimpleNamespace(repository=SimpleNamespace(list_for_run=lambda _: []))
    service.rag_grounding = SimpleNamespace(runs={'run': {}}, prepare_direct_answer=lambda _: [{'role': 'user', 'content': 'verified'}])
    run = {'id': 'run', 'web_search_route': 'none'}
    current = {'content': prompt, 'attachments': []}
    decision = {'task_type': 'evidence_answer', 'required': True, 'agent_loop': True,
                'document_workflow': 'none', 'clarification': None}
    run.update(changes.pop('run', {}))
    current.update(changes.pop('current', {}))
    decision.update(changes.pop('decision', {}))
    return service._prepare_direct_rag_answer(run, current, decision,
        application_document=changes.pop('application_document', False),
        specialist_artifacts=changes.pop('specialist_artifacts', []), presearch_message=changes.pop('presearch_message', None))


@pytest.mark.parametrize('prompt', [QUESTION, '依据资料库说明减压试验适用范围，给出引用。',
    'Explain the applicable equipment scope using project-library evidence, with one citation. Do not modify or export files.'])
def test_plain_evidence_question_can_answer_without_task_or_document_planning(prompt):
    assert candidate(prompt)


@pytest.mark.parametrize('prompt,changes', [
    (QUESTION + '另存为报告.docx。', {}),
    ('请修改文件，再说明减压范围。', {}),
    ('读取项目README文件并说明减压范围。', {}),
    ('运行命令并说明减压范围。', {}),
    ('调用$skill说明减压范围。', {}),
    ('请使用审阅技能说明减压范围。', {}),
    ('联网检索最新标准并说明减压范围。', {}),
    (QUESTION, {'current': {'attachments': [{'id': 'attachment'}]}}),
    (QUESTION, {'run': {'web_search_route': 'host_presearch'}}),
    (QUESTION, {'decision': {'operation_hint': 'export'}}),
    (QUESTION, {'application_document': True}),
    (QUESTION, {'specialist_artifacts': [{'id': 'specialist'}]}),
    (QUESTION, {'presearch_message': {'role': 'user', 'content': 'web'}}),
])
def test_composite_and_operational_requests_keep_the_agent_path(prompt, changes):
    assert candidate(prompt, **changes) is None


def grounding_fixture(parent=None):
    service = SimpleNamespace(assert_generation=lambda *args: None,
        validate_evidence=lambda *args, **kwargs: None)
    service.read_evidence = lambda *args, **kwargs: {'parent_context': parent}
    grounding = RagGrounding(service)
    text = '减压试验适用于安装在增压舱内的机载设备。'
    grounding.runs['run'] = {'query': QUESTION, 'project_id': 'project', 'generation': 1,
        'budget': 6000, 'used_tokens': 0, 'admitted': {'id': text}, 'quote_options': {}, 'aliases': {},
        'evidence': {'id': {'id': 'id', 'text': text, 'title': 'DO-160G', 'revision': 'G',
            'clause_number': '4.6.2', 'locator': {'context_required': bool(parent)}}}}
    return grounding


def test_general_direct_evidence_retains_parent_hash_budget_and_generation_gates():
    parent = '父条款保留适用条件与例外。'
    grounding = grounding_fixture({'text': parent, 'text_hash': hashlib.sha256(parent.encode()).hexdigest(), 'has_more': False})
    assert grounding.prepare_exact_clause_answer('run') is None
    assert parent in grounding.prepare_direct_answer('run')[0]['content']
    grounding.service.read_evidence = lambda *args, **kwargs: {'parent_context': {'text': parent, 'text_hash': 'wrong', 'has_more': False}}
    with pytest.raises(CoreDataError) as error:
        grounding.prepare_direct_answer('run')
    assert error.value.code == 'rag_context_integrity_invalid'
    grounding.service.read_evidence = lambda *args, **kwargs: {'parent_context': {'text': parent, 'text_hash': hashlib.sha256(parent.encode()).hexdigest(), 'has_more': True}}
    assert grounding.prepare_direct_answer('run') is None
    grounding = grounding_fixture()
    grounding.runs['run']['budget'] = 1
    assert grounding.prepare_direct_answer('run') is None
    def changed(*args):
        raise CoreDataError('rag_generation_changed', 'Changed')
    grounding.service.assert_generation = changed
    with pytest.raises(CoreDataError) as error:
        grounding.prepare_direct_answer('run')
    assert error.value.code == 'rag_generation_changed'


def test_general_direct_answer_still_rejects_unadmitted_ids_and_unknown_quotes():
    grounding = grounding_fixture()
    grounding.prepare_direct_answer('run')
    for citation, code in [({'id': 'other', 'quote_id': 'unknown'}, 'rag_citation_invalid'),
                            ({'id': 'id', 'quote_id': 'unknown'}, 'rag_quote_invalid')]:
        with pytest.raises(CoreDataError) as error:
            grounding.finalize('run', json.dumps({'claims': [{'text': '减压适用范围', 'evidence': [citation]}]}))
        assert error.value.code == code


@pytest.mark.parametrize('parts', [
    ['公开说明。', '此项尚待核实。'],
    ['{"type":"tool_call",', '"arguments":{"example":"仅示例"}}'],
    ['{"type":"tool_call", "arguments":'],
])
def test_actual_question_streams_plain_answer_in_one_call(tmp_path, monkeypatch, parts):
    import httpx
    from agent.rag.service import RagService
    from test_rag_service import Engines, ready, wait_job
    from test_tool_continuation_retry import configuration, fixture
    requests = []
    original = httpx.AsyncClient
    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        assert not body.get('tools'), 'Plain RAG QA must not expose business/task tools'
        data = json.loads(body['messages'][-1]['content'])
        source = data['verified_answer_evidence'][0]
        assert 'response_format' not in body or body['response_format'] == {'type': 'text'}
        chunks = [{'choices': [{'delta': delta, 'finish_reason': None}]} for delta in
                  [{'reasoning_content': 'private_thought'}, *({'content': part} for part in parts)]]
        chunks.append({'choices': [{'delta': {}, 'finish_reason': 'stop'}]})
        return httpx.Response(200, text=''.join('data: ' + json.dumps(c) + '\n\n' for c in chunks) + 'data: [DONE]\n\n')
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    async def scenario():
        events = []
        async def sink(event):
            events.append(event)
        config = configuration()
        config['base_url'] = 'http://127.0.0.1:19999/v1'
        core, project, work, service = await fixture(tmp_path, sink, config)
        await service.rag.close()
        service.rag = RagService(core, Engines())
        service.rag_grounding = RagGrounding(service.rag)
        service.context.rag_context = service.rag_grounding.evidence
        service.rag.execution_monitor = service.execution_monitor
        async def advisor(**kwargs):
            return {'web': 'off', 'web_evidence': 'optional', 'reasoning': 'standard',
                'task_kinds': ['airworthiness'], 'explanation_code': 'question', 'rag_requirement': 'required',
                'document_workflow': 'none', 'clarification': None}, None
        service._preflight_intent_advisor = advisor
        try:
            library = (await service.rag.handle('library.create', {'name': 'Rules',
                'embedding_model_id': 'embed', 'reranker_model_id': 'rank'}))['library']['id']
            await ready(service.rag, library, content='4.6.2 减压试验适用于安装在增压舱内的机载设备。')
            assert (await wait_job(service.rag, await service.rag.handle('index.build', {'library_id': library})))['status'] == 'completed'
            await service.rag.handle('library.bind', {'library_id': library, 'project_id': project['id'], 'enabled': True})
            result = await service.start({'conversation_id': work['id'], 'content': QUESTION, 'idempotency_key': 'direct-qa'})
            await service.tasks[result['run']['id']]
            run = core.get_model_run(result['run']['id'])
            assert run['status'] == 'completed', (run['error_code'], run['error_message'])
            assert len(requests) == 1
            assert run['assistant_message']['content'] == ''.join(parts)
            deltas = [e['data']['delta'] for e in events if e['event_type'] == 'run.chunk']
            assert ''.join(deltas) == ''.join(parts)
            if parts[0] == '公开说明。':
                assert deltas == parts
            assert not service.tool_loop.repository.list_for_run(run['id'])
            with core._connect() as db:
                assert db.execute('SELECT COUNT(*) FROM task_questions').fetchone()[0] == 0
        finally:
            await service.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize('operation', ['下载标准', '上传报告', '发送邮件', '提交结果', '打包项目',
    '部署服务', '安装软件', '复制文件', '转换文件', '查阅项目README文件', '阅读项目README文件', '运行pytest'])
def test_explicit_chinese_operations_cannot_be_dropped_by_direct_answer(operation):
    assert candidate('请' + operation + '，然后说明减压试验适用范围。') is None


@pytest.mark.parametrize('prompt', ['请依据资料库分析减压试验适用性。',
    '请依据资料库比较减压和过压的适用范围。',
    '请依据资料库说明减压范围，不要下载或修改文件。'])
def test_readonly_analysis_comparison_and_negative_operations_remain_direct(prompt):
    assert candidate(prompt)


@pytest.mark.parametrize('prompt,changes', [
    ('按上面那台设备的安装位置，是否需要减压试验？', {}),
    ('上述设备的减压适用范围是什么？', {}),
    ('Does the previously described device need a decompression test?', {}),
    (QUESTION, {'decision': {'reuse_evidence': True}}),
    (QUESTION, {'current': {'attachment_rejections': [{'id': 'rejected-file'}]}}),
    (QUESTION, {'current': {'input_refs': [{'source': 'prior-input'}]}}),
])
def test_history_dependent_or_rejected_source_questions_keep_authoritative_context(prompt, changes):
    assert candidate(prompt, **changes) is None


@pytest.mark.parametrize('workflow,direct', [(None, True), ('none', True), ('review_nonempty', False), ('fill', False)])
def test_real_schema_none_workflow_is_a_plain_question_not_an_operation(workflow, direct):
    assert bool(candidate(QUESTION, decision={'document_workflow': workflow})) == direct


@pytest.mark.parametrize('prompt,changes', [
    (QUESTION, {'decision': {'clarification': {'question': 'Confirm the equipment'}}}),
    ('它是否需要减压试验？', {}),
    ('这台设备是否需要减压试验？', {}),
    ('Does this equipment need a decompression test?', {}),
    ('Does it need a decompression test?', {}),
])
def test_pending_clarification_and_explicit_pronouns_preserve_agent_context(prompt, changes):
    assert candidate(prompt, **changes) is None


@pytest.mark.parametrize('prose', ['普通回答', '[1,2]', '{"name":"示例","arguments":[]}', '```python\nprint(1)\n```',
                                   '```json\n{"type":"tool_call","name":"example"}\n```'])
def test_public_text_on_structured_transport_includes_user_formats(prose):
    from agent.provider_tool_adapter import parse_structured_output
    assert parse_structured_output(prose, accept_public_text=True) == (prose, None)


def test_malformed_control_envelope_is_not_public_text():
    from agent.provider_tool_adapter import parse_structured_output
    from agent.provider_tool_adapter import ProviderError
    with pytest.raises(ProviderError):
        parse_structured_output('{"type":"tool_call", "arguments":', accept_public_text=True)
