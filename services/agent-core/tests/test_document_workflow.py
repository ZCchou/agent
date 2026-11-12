import hashlib
import io
import pytest
from docx import Document
from agent.storage import CoreDataError, CoreRepository


def template_bytes():
    document = Document()
    document.add_heading('振动试验', 1)
    document.add_paragraph('试验目的')
    document.add_paragraph('待填写')
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = '设备型号'
    table.cell(0, 1).text = ''
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def workflow(tmp_path):
    from agent.documents import DocumentWorkflow
    core = CoreRepository(tmp_path / 'agent.db')
    project = core.create_project('Project', 'work')
    work = core.create_conversation('project_work', 'Work', project_id=project['id'])
    run = core.start_model_run(content='填写模板', idempotency_key='document', provider_profile_id='p', model_id='m', conversation_id=work['id'])
    return core, run, DocumentWorkflow(core)


def test_inspect_and_fill_docx_paragraph_and_table_keep_original(tmp_path):
    core, run, service = workflow(tmp_path)
    payload = template_bytes()
    source = service.register_template(run['id'], payload, '模板.docx')
    inspected = service.inspect(run['id'], source['template_id'])
    assert inspected['sha256'] == hashlib.sha256(payload).hexdigest()
    paragraph = next(item for item in inspected['fields'] if item['text'] == '待填写')
    cell = next(item for item in inspected['fields'] if item['kind'] == 'table_cell' and not item['text'])
    result = service.fill(run['id'], source['template_id'], [
        {'target_id': paragraph['id'], 'text': '验证设备的性能和耐久性。', 'evidence': []},
        {'target_id': cell['id'], 'text': '', 'evidence': [], 'missing': ['设备型号未提供']},
    ])
    generated = Document(service.path_for(run['id'], result['id']))
    assert generated.paragraphs[2].text.startswith('验证设备')
    assert '待补充' in generated.tables[0].cell(0, 1).text
    assert result['manifest']['missing'] == ['设备型号未提供']
    assert source['sha256'] == hashlib.sha256(payload).hexdigest()
    assert result['status'] == 'draft'
    other = core.create_conversation('project_work', 'Other', project_id=core.create_project('Other', 'work')['id'])
    other_run = core.start_model_run(content='Export', idempotency_key='other', provider_profile_id='p', model_id='m', conversation_id=other['id'])
    with pytest.raises(CoreDataError):
        service.path_for(other_run['id'], result['id'])


def test_template_identity_targets_and_artifact_tampering_are_checked(tmp_path):
    core, run, service = workflow(tmp_path)
    source = service.register_template(run['id'], template_bytes(), '模板.docx')
    with pytest.raises(CoreDataError):
        service.fill(run['id'], source['template_id'], [{'target_id': 'forged', 'text': 'x', 'evidence': []}])
    path = service.path_for(run['id'], source['template_id'])
    path.write_bytes(b'changed')
    with pytest.raises(CoreDataError):
        service.inspect(run['id'], source['template_id'])


@pytest.mark.parametrize('format', ['docx', 'markdown', 'txt'])
def test_export_formats_are_file_backed_and_reopenable(tmp_path, format):
    core, run, service = workflow(tmp_path)
    artifact = service.create(run['id'], '试验目的\n验证设备性能。', format)
    path = service.path_for(run['id'], artifact['id'])
    assert artifact['size_bytes'] == path.stat().st_size
    assert artifact['sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    if format == 'docx':
        assert '验证设备性能' in '\n'.join(p.text for p in Document(path).paragraphs)
    else:
        assert '验证设备性能' in path.read_text(encoding='utf-8')


def test_section_selection_is_unique_and_publication_requires_actual_hash(tmp_path):
    core, run, service = workflow(tmp_path)
    source = service.register_template(run['id'], template_bytes(), '模板.docx')
    assert service.select_section(run['id'], source['id'], '填写振动试验的试验目的') == 'p-2'
    artifact = service.create(run['id'], 'Draft', 'txt')
    with pytest.raises(CoreDataError):
        service.record_publication(run['id'], artifact['id'], {'status': 'published', 'path': 'a.txt', 'sha256': 'wrong'})
    published = service.record_publication(run['id'], artifact['id'], {'status': 'published', 'path': 'a.txt', 'sha256': artifact['sha256']})
    assert published['status'] == 'published'


def test_incremental_filling_preserves_prior_fields_and_paragraph_format(tmp_path):
    core, run, service = workflow(tmp_path)
    source = Document(io.BytesIO(template_bytes()))
    source.paragraphs[2].runs[0].bold = True
    stream = io.BytesIO()
    source.save(stream)
    template = service.register_template(run['id'], stream.getvalue(), '模板.docx')
    first = service.fill(run['id'], template['id'], [{'target_id': 'p-2', 'text': '试验目的已填写', 'evidence': []}])
    second = service.fill(run['id'], first['id'], [{'target_id': 't-0-r0-c1', 'text': '型号A', 'evidence': []}])
    document = Document(service.path_for(run['id'], second['id']))
    assert document.paragraphs[2].text == '试验目的已填写'
    assert document.paragraphs[2].runs[0].bold
    assert document.tables[0].cell(0, 1).text == '型号A'
    assert len(second['manifest']['fields']) == 2


def test_project_template_import_requires_a_live_bound_operation(tmp_path):
    import asyncio
    import uuid
    from agent.runtime import ChatRunService
    from agent.tool_contracts import ToolCall
    from agent.tool_repository import ToolRepository
    async def exercise():
        async def sink(event): pass
        core = CoreRepository(tmp_path / 'agent.db')
        application = ChatRunService(core, sink)
        try:
            project = core.create_project('Project', 'work')
            work = core.create_conversation('project_work', 'Work', project_id=project['id'])
            run = core.start_model_run(content='Inspect template', idempotency_key='inspect', provider_profile_id='p', model_id='m', conversation_id=work['id'])
            attempt = core.begin_run_attempt(run['id'])
            tools = ToolRepository(core.database_path)
            operation = tools.create(run['id'], attempt['id'], 1, ToolCall('inspect', 'document.inspect_template', {'path': '模板.docx', 'limit': 1}))
            tools.transition(operation['id'], {'requested'}, 'validating')
            application.tool_loop.waiters[operation['id']] = asyncio.get_running_loop().create_future()
            core.transition_model_run(run['id'], {'queued'}, 'waiting_tool')
            key = f'input-{uuid.uuid4()}.docx'
            payload = template_bytes()
            (application.documents.root / key).write_bytes(payload)
            result = application.import_project_template(operation['id'], key, hashlib.sha256(payload).hexdigest())
            assert result['fields'][0]['id'] == 'p-0'
            assert result['trust'] == 'external_untrusted'
            with pytest.raises(CoreDataError):
                application.import_project_template(operation['id'], '../agent.db', '0'*64)
            with pytest.raises(CoreDataError):
                application.import_project_template(operation['id'], key, '0'*64)
            tools.transition(operation['id'], {'validating'}, 'completed')
            with pytest.raises(CoreDataError):
                application.import_project_template(operation['id'], key, hashlib.sha256(payload).hexdigest())
        finally:
            await application.shutdown()
    asyncio.run(exercise())


@pytest.mark.parametrize('destination', [None, '振动目的.docx'])
def test_text_only_model_uses_application_owned_document_publication(tmp_path, monkeypatch, destination):
    import asyncio
    import json
    import httpx
    from test_tool_continuation_retry import fixture, configuration
    original = httpx.AsyncClient
    payloads = []
    def respond(request):
        payloads.append(json.loads(request.content))
        chunk = {'choices': [{'delta': {'content': '欢迎使用本地智能体。'}, 'finish_reason': 'stop'}]}
        return httpx.Response(200, text='data: '+json.dumps(chunk)+'\n\ndata: [DONE]\n\n')
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    async def exercise():
        service = None
        published = []
        async def sink(event):
            if event['event_type'] == 'run.tool_requested':
                operation = event['data']['operation']
                assert operation['tool_name'] == 'document.export'
                artifact = service.document_artifact(operation['run_id'], operation['arguments']['artifact_id'])
                destination = tmp_path / operation['arguments']['path']
                destination.write_bytes(service.documents.path_for(operation['run_id'], artifact['id']).read_bytes())
                published.append(destination)
                service.resolve_tool(operation['id'], True, json.dumps({'status': 'published', 'path': destination.name, 'sha256': artifact['sha256']}))
        config = configuration()
        config['model_tool_modes']['model'] = 'text_only'
        core, project, work, service = await fixture(tmp_path, sink, config)
        try:
            request = '写一句欢迎语并导出Word文档' + (f'，保存为“{destination}”' if destination else '')
            response = await service.start({'conversation_id': work['id'], 'content': request, 'idempotency_key': 'export'})
            await service.tasks[response['run']['id']]
            run = core.get_model_run(response['run']['id'])
            assert run['status'] == 'completed', run.get('error_message')
            assert len(published) == 1
            if destination:
                assert published[0].name == destination
            assert '欢迎使用' in '\n'.join(p.text for p in Document(published[0]).paragraphs)
            assert not any(payload.get('tools') for payload in payloads)
        finally:
            await service.shutdown()
    asyncio.run(exercise())


def test_application_filling_selects_table_answer_cell(tmp_path):
    _, run, service = workflow(tmp_path)
    document = Document()
    document.add_heading('振动试验', 1)
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = '试验目的'
    table.cell(0, 1).text = '待填写'
    table.cell(1, 0).text = '试验条件'
    table.cell(1, 1).text = '待填写'
    stream = io.BytesIO()
    document.save(stream)
    source = service.register_template(run['id'], stream.getvalue(), '表格模板.docx')
    assert service.select_section(run['id'], source['id'], '填写振动试验的试验目的') == 't-0-r0-c1'
