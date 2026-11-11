import json
import asyncio

import pytest

from agent.execution.coordinator import ExecutionCoordinator
from agent.rag.routing import decide_evidence, merge_evidence_intent


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
