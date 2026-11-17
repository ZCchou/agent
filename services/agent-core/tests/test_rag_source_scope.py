import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.rag.routing import project_evidence_policy
from agent.runtime import ChatRunService


@pytest.mark.parametrize(('text', 'decision', 'attachments', 'expected'), [
    ('这个模板里，非空的内容有哪些，提取出来保存为新的文档', {}, True, 'attachment_only'),
    ('Summarize the attached certification report and export it', {'reason': 'model_verified', 'required': True}, True, 'attachment_only'),
    ('校对附件内的错别字', {}, True, 'attachment_only'),
    ('提取附件内容，再按项目资料库核对 DO-160F 条款', {}, True, 'mixed'),
    ('无需查询资料库，只提取附件原文', {}, True, 'attachment_only'),
    ('提取附件原文', {'requested_mode': 'required'}, True, 'required'),
    ('只审阅附件，不要查询资料库', {'reason': 'semantic_evidence'}, True, 'attachment_only'),
    ('仅依据附件审阅这些条款', {'reason': 'semantic_evidence'}, True, 'attachment_only'),
    ('只提取这份附件并导出文档', {'reason': 'evidence_continuation', 'required': True}, True, 'attachment_only'),
    ('不要查询资料库，回答这个问题', {'reason': 'semantic_evidence'}, False, 'none'),
    ('附件中的设备是否符合 DO-160F 第8.2.1.3条？', {}, True, 'required'),
    ('Quote section 8.2.1.3', {}, False, 'required'),
    ('继续', {'reason': 'evidence_continuation', 'required': True}, True, 'required'),
    ('查询项目资料库里的适航条款', {}, False, 'required'),
    ('说明 DO-160F 对振动试验的要求', {'rule_hint': {'required': True, 'reason': 'business_evidence'}}, False, 'optional'),
    ('你好', {'reason': 'model_selected', 'required': True}, False, 'none'),
])
def test_evidence_scope_is_based_on_the_request(text, decision, attachments, expected):
    assert project_evidence_policy(decision, text, has_attachments=attachments) == expected


@pytest.mark.parametrize('stage', ['initial_routing', 'document_planning', 'goal_verification',
                                   'retrieval_tool', 'review_tool'])
@pytest.mark.parametrize('enabled', [False, True])
def test_attachment_scope_rejects_project_evidence_at_every_entry(stage, enabled):
    grounding = SimpleNamespace(runs={}, prepare=AsyncMock())
    service = SimpleNamespace(rag_grounding=grounding, logger=None,
                              rag=SimpleNamespace(project_state=lambda _: {'enabled': enabled}))
    run = {'id': 'run', 'user_message_id': 'message',
           'rag_decision': {'requested_mode': 'auto', 'required': True, 'reason': 'model_goal_evidence'}}
    current = {'id': 'message', 'content': '这个模板里，非空的内容有哪些，提取出来保存为新的文档',
               'attachments': [{'id': 'template'}]}
    context = {'project_id': 'project', 'messages': [current]}
    allowed = asyncio.run(ChatRunService._prepare_requested_project_evidence(service, run, context, stage))
    assert allowed is False
    grounding.prepare.assert_not_awaited()



def test_completed_exact_answer_does_not_reenter_project_task_tools():
    from hashlib import sha256

    content = 'Verified answer with its citations'
    grounding = SimpleNamespace(validate=lambda run_id: None)
    service = SimpleNamespace(rag_grounding=grounding)
    run = {'id': 'run', '_verified_exact_answer_hash': sha256(content.encode()).hexdigest()}
    assert asyncio.run(ChatRunService._finish_task_goal(service, run, 'attempt', content, None,
        None, None, None, [], ('rag.search',), 'native', False)) == content
