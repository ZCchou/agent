import asyncio
import hashlib
import io
import json
import shutil

import httpx
import pytest
from docx import Document

from agent.rag.grounding import RagGrounding
from agent.rag.service import RagService
from test_document_attachments import source_attachment
from test_rag_service import Engines, ready, wait_job
from test_tool_continuation_retry import configuration, fixture


@pytest.mark.parametrize('purpose', ['template', 'reference'])
def test_local_agent_reviews_revises_and_exports_in_one_task(tmp_path, monkeypatch, purpose):
    original_client = httpx.AsyncClient
    main_turns, citations = [], []

    def respond(request):
        payload = json.loads(request.content)
        messages = payload['messages']
        last_user = next(message['content'] for message in reversed(messages) if message['role'] == 'user')
        try:
            batch_request = json.loads(last_user)
        except (TypeError, ValueError):
            batch_request = {}
        if isinstance(batch_request, dict) and 'batch' in batch_request:
            sources = batch_request['sources']['evidence']
            source = sources[0]
            citation = {'id': source['id'], 'quote_id': source['quote_options'][0]['quote_id']}
            citations[:] = [citation]
            answer = {'results': [{'target_id': field['id'], 'status': 'reviewed',
                'summary': source['quote_options'][0]['text'], 'content_kind': field['field_type'],
                'problems': [], 'evidence': [citation], 'version_issues': [], 'gaps': []}
                for field in batch_request['batch']['fields']]}
            delta, finish = {'content': json.dumps({'response': answer})}, 'stop'
        else:
            main_turns.append(payload)
            results = {message['name'].replace('.', '_'): json.loads(message['content']) for message in messages
                       if message['role'] == 'tool' and message['content'].startswith('{')}
            index = len(main_turns)
            if index == 1:
                name, arguments = 'task.update_plan', {'expected_revision': 0, 'steps': [
                    {'id': 'review', 'description': '审阅全部正文并修订保存', 'depends_on': []}],
                    'success_criteria': ['每个非空位置有审阅结果，修订文件已保存'],
                    'source_scopes': [{'source_id': 'doc', 'operation': 'review', 'scope': 'full', 'target_ids': [], 'reason': ''}],
                    'deliverables': [{'path': 'reviewed.docx'}]}
            elif index == 2:
                name, arguments = 'document.inspect_template', {'attachment_id': 'doc'}
            elif index == 3:
                name, arguments = 'document.review_targets', {
                    'template_id': results['document_inspect_template']['template_id']}
            elif index == 4:
                name, arguments = 'document.fill_template', {
                    'template_id': results['document_inspect_template']['template_id'],
                    'fields': [{'target_id': 'p-1', 'text': '修订后的试验说明', 'evidence': citations}]}
            elif index == 5:
                name, arguments = 'document.export', {
                    'artifact_id': results['document_fill_template']['id'], 'path': 'reviewed.docx'}
            else:
                delta, finish = {'content': '审阅及修订已完成，文件已保存。'}, 'stop'
                return response(delta, finish)
            delta = {'tool_calls': [{'index': 0, 'id': 'tool-' + str(index), 'type': 'function',
                'function': {'name': name.replace('.', '_'), 'arguments': json.dumps(arguments)}}]}
            finish = 'tool_calls'
        return response(delta, finish)

    def response(delta, finish):
        return httpx.Response(200, text='data: ' + json.dumps({
            'choices': [{'delta': delta, 'finish_reason': finish}]}) + '\n\ndata: [DONE]\n\n')

    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original_client(
        transport=httpx.MockTransport(respond), **kwargs))

    async def scenario():
        async def sink(event):
            if event['event_type'] == 'run.tool_requested':
                operation = event['data']['operation']
                assert operation['tool_name'] == 'document.export'
                artifact = service.documents.get(operation['run_id'], operation['arguments']['artifact_id'])
                target = tmp_path / operation['arguments']['path']
                shutil.copy2(service.documents.path_for(operation['run_id'], artifact['id']), target)
                service.tool_status(operation['id'], 'executing')
                service.resolve_tool(operation['id'], True, json.dumps({
                    'status': 'published', 'sha256': hashlib.sha256(target.read_bytes()).hexdigest(), 'path': str(target)}))

        config = configuration()
        config['base_url'] = 'http://127.0.0.1:19999/v1'
        core, project, work, service = await fixture(tmp_path, sink, config)
        await service.rag.close()
        service.rag = RagService(core, Engines())
        service.rag_grounding = RagGrounding(service.rag)
        service.rag.execution_monitor = service.execution_monitor
        service.document_attachments.parser = service.rag.parser
        document = Document()
        document.add_heading('试验要求', 1)
        document.add_paragraph('需要核对的正文')
        payload = io.BytesIO()
        document.save(payload)
        attachment = source_attachment(tmp_path, payload.getvalue(), '模板.docx')
        attachment['purpose'] = purpose
        try:
            library = (await service.rag.handle('library.create', {'name': 'Rules',
                'embedding_model_id': 'embed', 'reranker_model_id': 'rank'}))['library']['id']
            await ready(service.rag, library)
            await wait_job(service.rag, await service.rag.handle('index.build', {'library_id': library}))
            await service.rag.handle('library.bind', {'library_id': library, 'project_id': project['id'], 'enabled': True})
            result = await service.start({'conversation_id': work['id'],
                'content': '依据项目资料审阅附件的非空内容，然后修订正文并保存 DOCX。',
                'attachments': [attachment], 'rag_mode': 'required', 'idempotency_key': 'composite'})
            await service.tasks[result['run']['id']]
            run = core.get_model_run(result['run']['id'])
            assert run['status'] == 'completed', (run['error_code'], run['error_message'])
            assert any(item.text.startswith('修订后的试验说明') for item in Document(tmp_path / 'reviewed.docx').paragraphs)
            assert (tmp_path / attachment['storage_key']).read_bytes() == payload.getvalue()
            assert [item['tool_name'] for item in service.tool_loop.repository.list_for_run(run['id'])] == [
                'task.update_plan', 'document.inspect_template', 'document.review_targets', 'document.fill_template', 'document.export']
            assert service.execution_status({'owner_kind': 'run', 'owner_id': run['id']})['coverage']['ready']
        finally:
            await service.shutdown()
    asyncio.run(scenario())
