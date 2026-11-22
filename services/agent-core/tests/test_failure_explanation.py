import asyncio
import json

import httpx
import pytest

from agent.storage import CoreDataError


@pytest.mark.parametrize('code', ['document_template_changed', 'rag_resource_exhausted',
                                 'provider_context_limit_exceeded', 'rag_generation_changed'])
def test_local_model_explains_blocked_task_without_completing_it(tmp_path, monkeypatch, code):
    from test_tool_continuation_retry import configuration, fixture, install_http

    explanation = '模型说明：这项任务尚未完成，请先补齐所需条件，再从已有结果继续。'
    requests = install_http(monkeypatch, [({'content': explanation}, 'stop')])

    async def scenario():
        events = []
        async def sink(event):
            events.append(event)
        config = configuration()
        config['base_url'] = 'http://127.0.0.1:19999/v1'
        config['model_capabilities']['model']['max_output_tokens'] = 512
        core, project, work, service = await fixture(tmp_path, sink, config)
        async def blocked(*args):
            if code in {'rag_resource_exhausted', 'rag_generation_changed'}:
                service.rag_grounding.runs[args[0]['id']] = {'project_id': project['id'],
                    'generation': -1 if code == 'rag_generation_changed' else service.rag.project_state(project['id'])['generation'],
                    'admitted': {}, 'quote_options': {}}
            raise CoreDataError(code, 'RAW-EXCEPTION-SENTINEL')
        service._prepare_document_attachments = blocked
        try:
            started = await service.start({'conversation_id': work['id'], 'content': '解释普通文本',
                                           'idempotency_key': 'blocked-task'})
            await service.tasks[started['run']['id']]
            run = core.get_model_run(started['run']['id'])
            assert run['status'] == 'failed' and run['error_code'] == code
            plan = service.execution_repository.get_for_run(run['id'])
            assert next(step for step in plan['steps'] if step['step_type'] == 'final_response')['status'] == 'failed'
            assert explanation in run['draft']['content']
            assert len(requests) == 1
            payload = requests[0][1]
            assert not payload.get('tools') and not payload.get('response_format')
            assert payload['max_tokens'] <= 512
            assert code in json.dumps(payload, ensure_ascii=False)
            assert 'RAW-EXCEPTION-SENTINEL' not in json.dumps(payload)
            assert '解释普通文本' in json.dumps(payload, ensure_ascii=False)
            assert any(event['event_type'] == 'run.response_recovering' and
                       event['data'].get('explanation') is True for event in events)
            failed = [event for event in events if event['event_type'] == 'run.failed']
            assert len(failed) == 1 and explanation in failed[0]['data']['fallback_content']
        finally:
            await service.shutdown()
    asyncio.run(scenario())


def test_missing_rag_binding_is_saved_and_explained_by_the_selected_local_model(tmp_path, monkeypatch):
    from test_tool_continuation_retry import configuration, fixture, install_http

    explanation = '当前项目尚未启用资料库。请先启用资料库，原请求已保留。'
    requests = install_http(monkeypatch, [({'content': explanation}, 'stop')])
    async def scenario():
        async def sink(event):
            pass
        config = configuration()
        config['base_url'] = 'http://127.0.0.1:19999/v1'
        core, _, work, service = await fixture(tmp_path, sink, config)
        try:
            started = await service.start({'conversation_id': work['id'], 'content': '请依据项目资料回答',
                                           'rag_mode': 'required', 'idempotency_key': 'missing-library'})
            await service.tasks[started['run']['id']]
            run = core.get_model_run(started['run']['id'])
            assert run['status'] == 'failed' and run['error_code'] == 'rag_library_required'
            assert explanation in run['draft']['content']
            assert len(requests) == 1 and not requests[0][1].get('tools')
            assert len(core.get_conversation(work['id'])['messages']) >= 1
        finally:
            await service.shutdown()
    asyncio.run(scenario())


def test_failure_after_completed_tool_uses_current_step_state_for_explanation(tmp_path, monkeypatch):
    from test_tool_continuation_retry import configuration, fixture, list_reply, start

    requests = []
    explanation = '目录读取已完成，后续回复的上下文不足，请缩小范围后继续。'
    original = httpx.AsyncClient
    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 2:
            return httpx.Response(400, json={'error': {'message': 'maximum context length exceeded'}})
        assert len(requests) <= 3
        delta = list_reply('read-once') if len(requests) == 1 else {'content': explanation}
        finish = 'tool_calls' if len(requests) == 1 else 'stop'
        return httpx.Response(200, text='data: ' + json.dumps({'choices': [{'delta': delta, 'finish_reason': finish}]}) + '\n\ndata: [DONE]\n\n')
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))

    async def scenario():
        async def sink(event):
            if event['event_type'] == 'run.tool_requested':
                operation = event['data']['operation']
                service.tool_status(operation['id'], 'executing')
                service.resolve_tool(operation['id'], True, '{"entries":[]}')
        config = configuration()
        config['base_url'] = 'http://127.0.0.1:19999/v1'
        core, _, work, service = await fixture(tmp_path, sink, config)
        try:
            identity = await start(service, work)
            run = core.get_model_run(identity)
            assert run['status'] == 'failed' and run['error_code'] == 'provider_context_limit_exceeded'
            assert explanation in run['draft']['content']
            assert len(requests) == 3 and not requests[-1].get('tools')
            operations = service.list_tool_operations(identity)
            assert len(operations) == 1 and operations[0]['status'] == 'completed'
            invocations = service.execution_repository.get_for_run(identity)['invocations']
            assert len({item['round_no'] for item in invocations}) == len(invocations)
        finally:
            await service.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize('cancel', [False, True])
def test_diagnostic_failure_and_cancellation_do_not_restart_the_business_task(tmp_path, monkeypatch, cancel):
    from test_tool_continuation_retry import configuration, fixture

    async def scenario():
        entered, hold = asyncio.Event(), asyncio.Event()
        requests, events = [], []
        async def respond(request):
            requests.append(json.loads(request.content))
            entered.set()
            if cancel:
                await hold.wait()
            return httpx.Response(400, json={'error': {'message': 'Synthetic diagnostic rejection'}})
        original = httpx.AsyncClient
        monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs:
                            original(transport=httpx.MockTransport(respond), **kwargs))
        async def sink(event):
            events.append(event)
        config = configuration()
        config['base_url'] = 'http://127.0.0.1:19999/v1'
        core, _, work, service = await fixture(tmp_path, sink, config)
        calls = []
        async def blocked(*args):
            calls.append('business')
            raise CoreDataError('document_template_changed', 'Changed source')
        service._prepare_document_attachments = blocked
        try:
            started = await service.start({'conversation_id': work['id'], 'content': '解释普通文本',
                                           'idempotency_key': 'failed-explanation'})
            identity = started['run']['id']
            if cancel:
                await asyncio.wait_for(entered.wait(), timeout=10)
                await service.cancel(identity)
            else:
                await service.tasks[identity]
            run = core.get_model_run(identity)
            assert run['status'] == ('cancelled' if cancel else 'failed')
            assert service.execution_repository.get_for_run(identity)['status'] == ('cancelled' if cancel else 'failed')
            with core._connect() as connection:
                assert connection.execute('SELECT COUNT(*) FROM run_attempts WHERE run_id=?', (identity,)).fetchone()[0] == 1
            if not cancel:
                assert run['error_code'] == 'document_template_changed'
                assert '收尾回复也未完成' in run['draft']['content']
            assert len(requests) == 1 and calls == ['business']
            assert len([event for event in events if event['event_type'] == 'run.failed']) == (0 if cancel else 1)
        finally:
            await service.shutdown()
    asyncio.run(scenario())
