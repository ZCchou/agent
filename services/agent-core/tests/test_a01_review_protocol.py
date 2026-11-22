import asyncio

from agent.execution.document_goal_runtime import plan_conditional_evidence_review
from test_tool_continuation_retry import fixture, install_http, start


def test_named_section_review_with_advice_and_unquoted_filename():
    attachment = {'id': 'source', 'name': '大纲.docx'}
    request = ('结合项目资料库审阅第 10.1 节温度变化和第 10.2 节湿热试验计划。'
               '沿附件中的交叉引用核对功能检查。输出修改建议。'
               '将报告保存到已授权目录，文件名为 示例_温变与湿热专项审阅.md。')
    current = {'content': request, 'attachments': [attachment]}
    run = {'rag_decision': {'requested_mode': 'required'}}
    draft = plan_conditional_evidence_review(run, current, [attachment])
    assert draft['source_scopes'][0]['selector']['condition'] == request
    assert draft['deliverables'] == [{'format': 'markdown', 'path': '示例_温变与湿热专项审阅.md'}]
    current['content'] = request + '同时修改原文件。'
    assert plan_conditional_evidence_review(run, current, [attachment]) is None


def test_repeated_text_tool_calls_cannot_complete_or_execute(tmp_path, monkeypatch):
    markup = '<tool_call> {"args":{"query":"rag search"},"name":"rag","kind":"builtin"}'
    requests = install_http(monkeypatch, [({'content': markup}, 'stop')] * 2, goal_checks=True)

    async def exercise():
        events = []
        async def sink(event):
            events.append(event)
        repository, _, work, service = await fixture(tmp_path, sink)
        try:
            run_id = await start(service, work)
            run = repository.get_model_run(run_id)
            assert run['status'] == 'failed'
            assert run['error_code'] == 'model_tool_protocol_failed'
            assert len(requests) == 2
            assert not service.list_tool_operations(run_id)
            assert not any(e['event_type'] == 'run.completed' for e in events)
            assert markup not in run['draft']['content']
            assert not any('<tool_call>' in e['data'].get('delta', '') for e in events)
        finally:
            await service.shutdown()
    asyncio.run(exercise())
