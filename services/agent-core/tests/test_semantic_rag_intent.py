import json
import asyncio

import pytest

from agent.execution.coordinator import ExecutionCoordinator
from agent.rag.routing import decide_evidence, merge_evidence_intent


def test_classifier_carries_attachment_plan_and_clarification():
    intent = ExecutionCoordinator.parse_routing_intent(json.dumps({
        'web': 'off', 'reasoning': 'standard', 'task_kinds': ['attachment_extract'],
        'explanation_code': 'read_attachment', 'rag_requirement': 'not_needed',
        'read_attachment_ids': ['attachment-1'], 'task_scope': 'full',
        'retrieval_scope': 'focused', 'allow_simulated_data': True,
        'clarification': {'summary': '已识别读取任务', 'questions': [
            {'id': 'file', 'question': '读取哪份附件？', 'options': ['第一份', '第二份']}]},
    }))
    assert intent['read_attachment_ids'] == ['attachment-1']
    assert intent['task_scope'] == 'full'
    assert intent['clarification']['questions'][0]['id'] == 'file'
    merged = merge_evidence_intent(decide_evidence('提取文件'), intent)
    assert merged['task_type'] == 'attachment_extract'
    assert merged['retrieval_scope'] == 'focused'


@pytest.mark.parametrize('field,value', [('task_scope', 'everything'),
    ('read_attachment_ids', ['a', 'a']), ('read_attachment_ids', 'all'),
    ('allow_simulated_data', 'yes'), ('clarification', {'summary': 'x', 'questions': []})])
def test_classifier_rejects_invalid_read_plan_fields(field, value):
    payload = {'web': 'off', 'reasoning': 'standard', 'task_kinds': ['general'],
               'explanation_code': 'general', 'rag_requirement': 'not_needed', field: value}
    with pytest.raises(ValueError):
        ExecutionCoordinator.parse_routing_intent(json.dumps(payload))


def test_classifier_prompt_demonstrates_array_and_explanation_contract():
    import ast
    from pathlib import Path

    source = Path(__file__).parents[1] / 'src/agent/runtime.py'
    syntax = ast.parse(source.read_text(encoding='utf8'))
    method = next(node for node in ast.walk(syntax)
                  if isinstance(node, ast.AsyncFunctionDef) and node.name == '_preflight_intent_advisor')
    prompt = next(node.value.value for node in method.body
                  if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == 'prompt' for target in node.targets))
    assert 'JSON array' in prompt
    assert '["general"]' in prompt
    assert '[a-z0-9_.-]{1,80}' in prompt
    assert len(prompt.encode('utf8')) + 64 + 256 <= 2048


def test_semantic_contract_accepts_evidence_and_document_tasks():
    intent = ExecutionCoordinator.parse_routing_intent(json.dumps({
        'web': 'off', 'reasoning': 'standard', 'task_kinds': ['document_export'],
        'explanation_code': 'template_fill', 'rag_requirement': 'required',
    }))
    assert intent['rag_requirement'] == 'required'


@pytest.mark.parametrize('value', ['always', True, None, []])
def test_semantic_contract_rejects_invalid_evidence_requirement(value):
    with pytest.raises(ValueError):
        ExecutionCoordinator.parse_routing_intent(json.dumps({
            'web': 'off', 'reasoning': 'standard', 'task_kinds': ['general'],
            'explanation_code': 'general', 'rag_requirement': value,
        }))


def test_semantic_evidence_overrides_keyword_guess():
    ordinary = decide_evidence('这个设备安装在发动机旁，需要验证什么')
    result = merge_evidence_intent(ordinary, {'rag_requirement': 'required', 'task_kinds': ['airworthiness']})
    assert result['required']
    assert result['source'] == 'advisor'
    assert not merge_evidence_intent(decide_evidence('DO-160资料库怎么用'), {
        'rag_requirement': 'not_needed', 'task_kinds': ['general'],
    })['required']


@pytest.mark.parametrize('mode,skill', [('required', False), ('auto', True)])
def test_semantic_result_cannot_override_user_or_skill(mode, skill):
    result = merge_evidence_intent(decide_evidence('你好', mode=mode, skill_required=skill), {
        'rag_requirement': 'not_needed', 'task_kinds': ['general'],
    })
    assert result['required']


def test_semantic_template_export_preserves_application_owned_route():
    decision = decide_evidence('帮我完成它')
    decision['attachment_roles'] = {'a': 'template'}
    result = merge_evidence_intent(decision, {
        'rag_requirement': 'required', 'task_kinds': ['document_export'],
    })
    assert result['task_type'] == 'document_export'
    assert result['attachment_roles'] == {'a': 'template'}


def test_uncertain_and_failed_classification_are_visible_and_conservative():
    decision = decide_evidence('振动试验的目的')
    result = merge_evidence_intent(decision, {'rag_requirement': 'uncertain', 'task_kinds': ['airworthiness']})
    assert result['required']
    assert result['needs_confirmation']
    failed = merge_evidence_intent(decision, None, error_code='routing_intent_timeout')
    assert failed['required']
    assert failed['source'] == 'fallback'
    assert failed['advisor_error_code'] == 'routing_intent_timeout'


def test_offline_fixed_model_semantic_intent_runs_before_library_check(tmp_path, monkeypatch):
    from test_tool_continuation_retry import configuration, fixture
    from agent.storage import CoreDataError

    async def scenario():
        async def sink(event):
            pass
        config = configuration()
        config['base_url'] = 'http://127.0.0.1:12345/v1'
        core, project, chat, service = await fixture(tmp_path, sink, config)
        service.offline = True
        calls = []

        async def advisor(**kwargs):
            calls.append(kwargs)
            return {'rag_requirement': 'required', 'task_kinds': ['airworthiness'],
                    'files': 'none', 'tools': 'available', 'web': 'not_needed',
                    'reasoning': 'standard'}, None
        monkeypatch.setattr(service, '_preflight_intent_advisor', advisor)
        try:
            with pytest.raises(CoreDataError) as error:
                await service.start({'conversation_id': chat['id'],
                    'content': '这个设备安装在发动机旁，需要验证什么', 'idempotency_key': 'semantic'})
            assert error.value.code == 'rag_library_required'
            assert len(calls) == 1
            assert not any(m['role'] == 'user' for m in core.get_conversation(chat['id'])['messages'])
        finally:
            await service.shutdown()
    asyncio.run(scenario())


def test_classifier_input_has_bounded_request_roles_and_history():
    from agent.rag.routing import intent_payload
    payload = intent_payload('问题' * 8000, [{'name': '模板.docx', 'purpose': 'template'}],
                             [{'role': 'assistant', 'content': '历史' * 8000}], 'project_work', budget_bytes=1600)
    assert len(payload.encode('utf8')) <= 1600
    parsed = json.loads(payload)
    assert parsed['request']
    assert parsed['request_truncated']
    assert parsed['attachments'][0]['purpose'] == 'template'


def test_first_project_message_uses_bound_libraries(tmp_path, monkeypatch):
    from test_tool_continuation_retry import configuration, fixture

    async def scenario():
        async def sink(event):
            pass
        config = configuration()
        config['base_url'] = 'http://127.0.0.1:12345/v1'
        core, project, chat, service = await fixture(tmp_path, sink, config)
        monkeypatch.setattr(service.rag, 'project_state', lambda project_id: {'enabled': True})
        async def advisor(**kwargs):
            return {'rag_requirement': 'required', 'task_kinds': ['airworthiness'],
                    'files': 'none', 'tools': 'available', 'web': 'not_needed',
                    'reasoning': 'standard'}, None
        monkeypatch.setattr(service, '_preflight_intent_advisor', advisor)
        try:
            started = await service.start({'kind': 'project_work', 'project_id': project['id'],
                'provider_profile_id': 'synthetic', 'model_id': 'model',
                'content': '振动试验目的', 'idempotency_key': 'first-message'})
            assert started['run']['rag_decision']['required']
        finally:
            await service.shutdown()
    asyncio.run(scenario())


def test_export_of_evidenced_answer_keeps_source_checks_after_semantic_classification():
    decision = decide_evidence('把刚才的回答保存为Word文档', previous_evidence=True)
    assert decision['reuse_evidence']
    result = merge_evidence_intent(decision, {'rag_requirement': 'not_needed', 'task_kinds': ['document_export']})
    assert result['required']
    assert result['reuse_evidence']


def test_classifier_reports_transport_timeout_distinctly(tmp_path, monkeypatch):
    from test_tool_continuation_retry import configuration, fixture
    async def scenario():
        async def sink(event):
            pass
        core, project, chat, service = await fixture(tmp_path, sink, configuration())
        class UnavailableProvider:
            async def stream_events(self, *args, **kwargs):
                raise TimeoutError('model did not respond')
                yield
        monkeypatch.setattr(service, '_provider', lambda profile: UnavailableProvider())
        try:
            result, code = await service._preflight_intent_advisor(content='普通问题', attachments=[],
                profile=next(iter(service.profiles.values())), model_id='model', conversation_kind='project_work', recent_messages=[])
            assert result is None
            assert code == 'routing_intent_timeout'
        finally:
            await service.shutdown()
    asyncio.run(scenario())


def test_classifier_payload_carries_bounded_pending_task():
    from agent.rag.routing import intent_payload
    value = json.loads(intent_payload('继续', [], [], 'project_work', budget_bytes=900,
        pending_task={'task_type': 'document_export', 'required': True, 'request': '填写模板' * 1000}))
    assert value['pending_task']['task_type'] == 'document_export'
    assert value['pending_task']['required']
    assert value['pending_task']['request'].startswith('填写模板')
    assert len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()) <= 900
    escaped = intent_payload('继续', [], [], 'project_work', budget_bytes=300,
        pending_task={'task_type': 'document_export', 'required': True, 'request': '\x01' * 1000})
    assert len(escaped.encode()) <= 300


def test_pending_task_continuation_keeps_template_selection_and_ranges():
    from agent.rag.routing import continuation_task
    from agent.execution.domain import stable_hash
    from agent.storage import CoreDataError
    selection = [{'current_attachment_id': 'template', 'source_attachment_id': 'source',
                  'source_message_id': 'message', 'source_start': 10, 'source_end': 80}]
    intent = {'effective_requirements': {}, 'attachment_selection': selection, 'effective_attachment_ids': ['template']}
    plan = {'resolved_intent': intent, 'resolution_hash': stable_hash(intent),
            'requirements_hash': stable_hash({'requirements_hash': stable_hash({}), 'attachment_selection': selection})}
    run = {'id': 'previous', 'user_message_id': 'request', 'rag_decision': {'task_type': 'document_export',
           'required': True, 'attachment_roles': {'template': 'template'}}}
    messages = [{'id': 'request', 'content': '填写模板振动试验目的并保存为 Word', 'attachments': [{'id': 'template'}]}]
    result = continuation_task(run, messages, plan)
    assert result['task_type'] == 'document_export'
    assert result['request'] == messages[0]['content']
    assert result['references'] == [{'attachment_id': 'template', 'source_start': 10, 'source_end': 80}]
    plan['resolution_hash'] = 'tampered'
    with pytest.raises(CoreDataError, match='integrity'):
        continuation_task(run, messages, plan)


@pytest.mark.parametrize('mode', ['auto', 'required'])
def test_runtime_continue_preserves_document_task_in_both_modes(tmp_path, monkeypatch, mode):
    from test_tool_continuation_retry import configuration, fixture
    async def scenario():
        async def sink(event):
            pass
        config = configuration()
        config['base_url'] = 'http://127.0.0.1:12345/v1'
        core, project, chat, service = await fixture(tmp_path, sink, config)
        monkeypatch.setattr(service.rag, 'project_state', lambda project_id: {'enabled': True})
        original = core.start_model_run(content='填写项目模板并保存为Word文档', idempotency_key='original',
            conversation_id=chat['id'], provider_profile_id='synthetic', model_id='model',
            rag_decision={'requested_mode': mode, 'required': True, 'reason': 'template_fill',
                          'task_type': 'document_export', 'attachment_roles': {}})
        core.transition_model_run(original['id'], {'queued'}, 'failed')
        observed = []
        async def advisor(**kwargs):
            observed.append(kwargs)
            return {'rag_requirement': 'not_needed', 'task_kinds': ['general'], 'files': 'none',
                    'tools': 'available', 'web': 'not_needed', 'reasoning': 'standard'}, None
        monkeypatch.setattr(service, '_preflight_intent_advisor', advisor)
        try:
            started = await service.start({'conversation_id': chat['id'], 'content': '继续',
                'idempotency_key': 'continue', 'rag_mode': mode})
            decision = started['run']['rag_decision']
            assert decision['task_type'] == 'document_export'
            assert decision['required']
            assert decision['query_context'] == '填写项目模板并保存为Word文档'
            assert observed[0]['pending_task']['task_type'] == 'document_export'
        finally:
            await service.shutdown()
    asyncio.run(scenario())
