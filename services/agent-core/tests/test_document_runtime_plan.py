import asyncio
import io
from types import SimpleNamespace

from docx import Document
import pytest

from agent.runtime import ChatRunService
from agent.storage import CoreDataError, CoreRepository
from agent.documents import DocumentWorkflow


def runtime_attachment(root, identity, text, name='reference.docx'):
    from test_document_attachments import source_attachment
    document = Document()
    document.add_paragraph(text)
    stream = io.BytesIO()
    document.save(stream)
    attachment = source_attachment(root, stream.getvalue(), name)
    path = root / 'message-attachments' / f'{identity}.docx'
    (root / attachment['storage_key']).rename(path)
    attachment.update(id=identity, storage_key='message-attachments/' + path.name)
    attachment['artifacts'][0].update(artifact_id=identity + '-source', storage_key=attachment['storage_key'])
    return attachment


@pytest.mark.parametrize('mode', ['selected_one', 'native_deferred', 'text_continuation', 'json_string', 'json_object'])
def test_normal_document_ranges_leave_unselected_originals_metadata_only(tmp_path, monkeypatch, mode):
    import json
    import httpx
    from test_tool_continuation_retry import fixture, configuration
    monkeypatch.setenv('AGENT_DATA_DIR', str(tmp_path))
    original = httpx.AsyncClient
    payloads = []
    def respond(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        if mode == 'native_deferred' and len(payloads) == 1:
            delta = {'tool_calls': [{'index': 0, 'id': 'read', 'type': 'function', 'function': {
                'name': 'document_read_attachment', 'arguments': json.dumps({'attachment_id': 'selected', 'physical_page': 2, 'limit': 1})}}]}
            finish = 'tool_calls'
        elif mode == 'text_continuation' and len(payloads) == 1:
            delta = {'content': json.dumps({'read_requests': [{'attachment_id': 'selected', 'physical_page': 2, 'offset': 1, 'limit': 1}]})}
            finish = 'stop'
        elif mode in {'json_string', 'json_object'}:
            result = {'response': 'Equipment summary' if mode == 'json_string' else {'model': 'EQ-REAL'}}
            delta, finish = {'content': json.dumps(result)}, 'stop'
        else:
            delta, finish = {'content': 'Completed with LATE-PAGE-SECOND.'}, 'stop'
        chunk = {'choices': [{'delta': delta, 'finish_reason': finish}]}
        return httpx.Response(200, text='data: '+json.dumps(chunk)+'\n\ndata: [DONE]\n\n')
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    class Parser:
        async def parse(self, path):
            return {'chunks': [{'text': 'EARLY ' * 1000, 'locator': {'page': 1}},
                               {'text': 'LATE-PAGE-FIRST', 'locator': {'page': 2}},
                               {'text': 'LATE-PAGE-SECOND', 'locator': {'page': 2}}], 'warnings': []}
    async def scenario():
        async def sink(event): pass
        config = configuration()
        config['model_tool_modes']['model'] = 'native' if mode == 'native_deferred' else 'text_only'
        core, _, work, service = await fixture(tmp_path, sink, config)
        service.document_attachments.parser = Parser()
        selected = runtime_attachment(tmp_path, 'selected', 'selected source')
        unselected = runtime_attachment(tmp_path, 'unselected', 'unselected binary body')
        async def intent(**kwargs):
            return {'files': 'available', 'tools': 'available', 'web': 'off', 'web_evidence': 'optional',
                'reasoning': 'standard', 'task_kinds': ['attachment_extract'], 'explanation_code': 'read',
                'rag_requirement': 'not_needed', 'read_attachment_ids': [] if mode == 'native_deferred' else ['selected'],
                'read_requests': ([{'attachment_id': 'selected', 'physical_page': 2, 'limit': 1}]
                                  if mode == 'text_continuation' else [])}, None
        service._preflight_intent_advisor = intent
        try:
            response = await service.start({'conversation_id': work['id'], 'content': 'Read selected document page 2',
                'attachments': [selected, unselected], 'idempotency_key': mode})
            await service.tasks[response['run']['id']]
            run = core.get_model_run(response['run']['id'])
            assert run['status'] == 'completed', run.get('error_message')
            first = json.dumps(payloads[0], ensure_ascii=False)
            assert 'unselected binary body' not in first
            if mode == 'native_deferred':
                assert 'LATE-PAGE-FIRST' not in first
                assert 'document_read_attachment' in first
                assert 'LATE-PAGE-FIRST' in json.dumps(payloads[1])
            if mode == 'text_continuation':
                assert 'LATE-PAGE-FIRST' in first
                assert len(payloads) == 2
                assert 'LATE-PAGE-SECOND' in json.dumps(payloads[1])
                reads = run['rag_decision']['document_reads']
                assert reads[-1]['request']['offset'] == 1
                assert reads[-1]['result']['blocks'][0]['locator']['page'] == 2
            if mode in {'json_string', 'json_object'}:
                expected = {'response': 'Equipment summary' if mode == 'json_string' else {'model': 'EQ-REAL'}}
                answer = next(item['content'] for item in reversed(core.get_conversation(run['conversation_id'])['messages']) if item['role'] == 'assistant')
                assert answer == json.dumps(expected)
                assert len(payloads) == 1
        finally:
            await service.shutdown()
    asyncio.run(scenario())


def test_template_registration_reuses_same_run_identity_after_verifying_bytes(tmp_path):
    from test_document_workflow import workflow, template_bytes
    _, run, service = workflow(tmp_path)
    first = service.register_template(run['id'], template_bytes(), 'template.docx')
    second = service.register_template(run['id'], service.path_for(run['id'], first['id']).read_bytes(), 'template.docx')
    assert first['id'] == second['id']


def test_application_full_template_uses_batched_plan_for_repeated_headings(tmp_path):
    async def scenario():
        core = CoreRepository(tmp_path / 'agent.db')
        project = core.create_project('P', 'work')
        conversation = core.create_conversation('project_work', 'C', project_id=project['id'])
        run = core.start_model_run(content='填写整份模板，允许模拟数据', idempotency_key='full',
            provider_profile_id='p', model_id='m', conversation_id=conversation['id'])
        run['rag_decision'] = {'task_scope': 'full', 'allow_simulated_data': True}
        document = Document()
        for name in ['振动试验', '温度试验']:
            document.add_heading(name, 1)
            document.add_heading('试验目的', 2)
            document.add_paragraph('请填写试验目的')
        payload = io.BytesIO()
        document.save(payload)
        service = ChatRunService.__new__(ChatRunService)
        service.repository = core
        service.documents = DocumentWorkflow(core)
        template = service.documents.register_template(run['id'], payload.getvalue(), '模板.docx')
        service.rag_grounding = SimpleNamespace(runs={}, validate=lambda _: None,
            document_claim=lambda _, text, citations: (text, []))
        service._profile = lambda _: SimpleNamespace(model_capabilities={'m': {'context_length': 8192}})
        async def emit(*args):
            pass
        service._emit = emit
        calls = []
        async def generate(run, attempt_id, task):
            calls.append(task)
            if task['stage'] == 'plan':
                return {'targets': [{'target_id': f['id'], 'field_type': 'instruction', 'parameter_keys': []}
                    for f in task['fields'] if f['original_content'] == '请填写试验目的']}
            return {'fields': [{'target_id': f['target_id'], 'text': f['chapter_path'][0] + '完成稿',
                'evidence': [], 'missing': ['实际设备资料待补充']} for f in task['fields']]}
        service._generate_document_batch = generate
        result = await service._fill_template_planned(run, 'attempt', template['id'], '填写整份模板，允许模拟数据')
        generated = Document(service.documents.path_for(run['id'], result['id']))
        completed = [p.text for p in generated.paragraphs if '完成稿' in p.text]
        assert len(completed) == 2
        assert completed[0].startswith('振动试验完成稿')
        assert completed[1].startswith('温度试验完成稿')
        assert all('实际设备资料待补充' in text for text in completed)
        assert result['manifest']['coverage']['completed'] == 2
        assert all(task['allow_simulated'] for task in calls)
        assert all(len(task['fields']) <= 10 for task in calls if task['stage'] == 'fill')
    asyncio.run(scenario())


@pytest.mark.parametrize(('equipment_value', 'retry_mode'), [('EQ-ALPHA', None), ('EQ-BETA', 'same'), ('EQ-GAMMA', 'linked'),
    ('USER-ACTUAL-123', 'real_only'), ('USER-ACTUAL-456', 'mixed'), ('LONG-ACTUAL', 'long'),
    ('CHANGED-ACTUAL', 'changed_source'), ('EQ-FALLBACK', 'schema_unsupported'),
    ('EQ-ARRAY', 'strict_arrays'), ('EQ-DUPLICATE', 'strict_duplicate')])
def test_normal_template_run_uses_audited_batches_and_publishes_docx(tmp_path, monkeypatch, equipment_value, retry_mode):
    import json
    import httpx
    from test_tool_continuation_retry import fixture, configuration
    from test_document_attachments import source_attachment
    from test_rag_service import Engines, ready, wait_job
    from agent.rag.service import RagService
    from agent.rag.grounding import RagGrounding
    original = httpx.AsyncClient
    payloads = []
    retry = retry_mode in {'same', 'linked'}
    simulated = retry_mode == 'mixed'
    parameters = {'model': equipment_value, **({'serial': 'SIM-999'} if simulated else {})}
    def respond(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        if retry_mode == 'schema_unsupported' and len(payloads) == 1:
            return httpx.Response(400, json={'error': {'message': 'response_format unsupported'}})
        task = json.loads(payload['messages'][-1]['content'])['task']
        supplied = json.loads(payload['messages'][-1]['content'])['source_context']
        if equipment_value not in json.dumps(supplied, ensure_ascii=False):
            result = {'read_requests': [{'attachment_id': 'equipment', 'offset': 0, 'limit': 1},
                                        {'attachment_id': 'doc', 'offset': 0, 'limit': 1}]}
        elif task['stage'] == 'plan':
            if not retry:
                assert any(item['attachment_id'] == 'doc' for item in supplied['read_results'])
            result = {'equipment_parameters': parameters,
                'targets': [{'target_id': field['id'], 'field_type': 'placeholder',
                             'parameter_keys': list(parameters) if field['id'] == 'p-5' else ['model']}
                for field in task['fields'] if field['original_content'] == '待填写']}
        else:
            supplied = json.loads(payload['messages'][-1]['content'])['source_context']
            assert equipment_value in json.dumps(supplied, ensure_ascii=False)
            sources = json.loads(payload['messages'][-1]['content'])['evidence']
            citations = ([{'id': sources[0]['id'], 'quote_id': sources[0]['quote_options'][0]['quote_id']}]
                         if retry and sources else [])
            result = {'fields': [{'target_id': field['target_id'],
                'text': ' '.join('{{equipment:' + key + '}}' for key in field['parameter_keys']) + ' 设备性能验证草稿。',
                'parameter_values': {key: parameters[key] for key in field['parameter_keys']},
                'evidence': citations, 'missing': ['设备安装位置和适用依据待补充']} for field in task['fields']]}
        if retry_mode in {'strict_arrays', 'strict_duplicate'} and not result.get('read_requests'):
            containers = [result] if task['stage'] == 'plan' else result['fields']
            key = 'equipment_parameters' if task['stage'] == 'plan' else 'parameter_values'
            for container in containers:
                container[key] = [{'name': name, 'value': value} for name, value in container[key].items()]
                if retry_mode == 'strict_duplicate':
                    container[key] += container[key]
            result = {'response': result}
        chunk = {'choices': [{'delta': {'content': json.dumps(result, ensure_ascii=False)}, 'finish_reason': 'stop'}]}
        return httpx.Response(200, text='data: '+json.dumps(chunk)+'\n\ndata: [DONE]\n\n')
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    async def scenario():
        application = None
        published = []
        async def sink(event):
            if event['event_type'] == 'run.tool_requested':
                operation = event['data']['operation']
                artifact = application.document_artifact(operation['run_id'], operation['arguments']['artifact_id'])
                path = tmp_path / operation['arguments']['path']
                path.write_bytes(application.documents.path_for(operation['run_id'], artifact['id']).read_bytes())
                published.append(path)
                application.resolve_tool(operation['id'], True, json.dumps({'status': 'published', 'path': path.name, 'sha256': artifact['sha256']}))
        config = configuration()
        config['base_url'] = 'http://127.0.0.1:19999/v1'
        config['model_tool_modes']['model'] = 'text_only'
        core, project, work, application = await fixture(tmp_path, sink, config)
        await application.rag.close()
        application.rag = RagService(core, Engines())
        application.rag_grounding = RagGrounding(application.rag)
        async def intent(**kwargs):
            return {'web': 'off', 'web_evidence': 'optional', 'reasoning': 'standard',
                'task_kinds': ['document_export'], 'explanation_code': 'template_fill',
                'rag_requirement': 'required', 'read_attachment_ids': ['equipment'] if retry else [], 'task_scope': 'full',
                'allow_simulated_data': retry_mode != 'real_only'}, None
        application._preflight_intent_advisor = intent
        generated = application._generate_document_batch
        seen = []
        failed = False
        async def interrupt_batch(run, attempt_id, task):
            nonlocal failed
            seen.append((task['stage'], [field.get('target_id', field.get('id')) for field in task['fields']]))
            if retry and task['stage'] == 'fill' and task['fields'][0]['target_id'] == 'p-5' and not failed:
                failed = True
                raise CoreDataError('document_test_interrupted', 'Interrupted later chapter')
            result = await generated(run, attempt_id, task)
            if retry_mode == 'changed_source' and task['stage'] == 'plan':
                (tmp_path / 'message-attachments/equipment.docx').write_bytes(b'changed source')
            return result
        application._generate_document_batch = interrupt_batch
        class Parser:
            async def parse(self, path):
                parsed = Document(path)
                return {'chunks': [{'text': '\n'.join(p.text for p in parsed.paragraphs),
                                    'locator': {'kind': 'docx', 'paragraph': 1}}], 'warnings': []}
        application.document_attachments.parser = Parser()
        try:
            library = (await application.rag.handle('library.create', {'name': 'Rules', 'embedding_model_id': 'embed', 'reranker_model_id': 'rank'}))['library']['id']
            await ready(application.rag, library)
            assert (await wait_job(application.rag, await application.rag.handle('index.build', {'library_id': library})))['status'] == 'completed'
            await application.rag.handle('library.bind', {'library_id': library, 'project_id': project['id'], 'enabled': True})
            document = Document()
            for chapter in ['振动试验', '温度试验']:
                document.add_heading(chapter, 1)
                document.add_heading('试验目的', 2)
                document.add_paragraph('待填写')
            payload = io.BytesIO()
            document.save(payload)
            attachment = source_attachment(tmp_path, payload.getvalue(), '测试模板.docx')
            source = tmp_path / attachment['storage_key']
            source.rename(source.with_name('template.docx'))
            attachment['storage_key'] = 'message-attachments/template.docx'
            attachment['artifacts'][0]['storage_key'] = attachment['storage_key']
            equipment = runtime_attachment(tmp_path, 'equipment', equipment_value, 'equipment.docx')
            request_text = '填写整份模板，' + ('只使用实际数据，不允许模拟' if retry_mode == 'real_only' else '允许模拟数据')
            if retry_mode == 'long':
                request_text += ' 请保留各章节专业任务与设备验证要求。' * 140
            response = await application.start({'conversation_id': work['id'], 'content': request_text,
                'attachments': [attachment, equipment], 'idempotency_key': 'template-normal'})
            await application.tasks[response['run']['id']]
            run = core.get_model_run(response['run']['id'])
            if retry_mode == 'strict_duplicate':
                assert run['status'] == 'failed' and run['error_code'] == 'document_plan_invalid'
                assert not published
                return
            if retry_mode == 'changed_source':
                assert run['status'] == 'failed'
                assert run['error_code'] == 'attachment_changed'
                assert published == []
                return
            if retry:
                assert run['status'] == 'failed'
                resumed = await application.retry(run['id'], context_strategy='expanded_retry' if retry_mode == 'linked' else 'default')
                await application.tasks[resumed['id']]
                run = core.get_model_run(resumed['id'])
                assert sum(stage == 'plan' for stage, fields in seen) == 1
                assert sum(stage == 'fill' and fields == ['p-2'] for stage, fields in seen) == 1
            assert run['status'] == 'completed', run.get('error_message')
            assert len(published) == 1
            completed = Document(published[0])
            assert sum('设备性能验证草稿' in p.text for p in completed.paragraphs) == 2
            assert sum(equipment_value in p.text for p in completed.paragraphs) == 2
            with core._connect() as connection:
                published_id = connection.execute("SELECT id FROM document_artifacts WHERE run_id=? AND status='published'", (run['id'],)).fetchone()['id']
            manifest = application.documents.get(run['id'], published_id)['manifest']
            assert manifest['equipment_parameters'] == parameters
            assert manifest['equipment_parameter_provenance']['model']['kind'] == 'attachment'
            assert manifest['equipment_parameter_provenance']['model']['source_sha256'] == equipment['sha256']
            assert manifest['simulated'] is simulated
            assert sum('【模拟数据示例' in p.text for p in completed.paragraphs) == int(simulated)
            if simulated:
                assert manifest['equipment_parameter_provenance']['serial']['kind'] == 'simulated'
            if retry:
                with core._connect() as connection:
                    published_id = connection.execute("SELECT id FROM document_artifacts WHERE run_id=? AND status='published'", (run['id'],)).fetchone()['id']
                fields = application.documents.get(run['id'], published_id)['manifest']['fields']
                assert all(field['evidence'] for field in fields)
                application.rag.validate_evidence(project['id'], [source['id'] for field in fields for source in field['evidence']], run_id=run['id'])
            fallback = retry_mode == 'schema_unsupported'
            assert all(payload.get('response_format', {}).get('type') == (
                'json_object' if fallback and index == 1 else 'json_schema') for index, payload in enumerate(payloads))
            assert len(payloads) == (3 if retry else 4) + int(fallback)
            plan = application.execution_coordinator.get_for_run(run['id'])
            assert len(plan['invocations']) == (1 if retry_mode == 'linked' else 3 if retry else 4) + int(fallback)
            assert sum(item['status'] == 'failed' for item in plan['invocations']) == int(fallback)
            assert all(item['status'] in {'completed', 'failed'} for item in plan['invocations'])
        finally:
            await application.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize('name', ['document.read_attachment', 'document.inspect_template', 'rag.search', 'rag.read'])
def test_native_document_and_rag_tool_names_roundtrip(name):
    from agent.providers import _external_tool_name, _internal_tool_name
    assert _internal_tool_name(_external_tool_name(name)) == name


def test_text_read_plan_preserves_validated_range_and_rejects_unknown_fields():
    import json
    from agent.execution.coordinator import ExecutionCoordinator
    payload = {'web': 'off', 'reasoning': 'standard', 'task_kinds': ['attachment_extract'],
               'explanation_code': 'read', 'read_requests': [{'attachment_id': 'doc', 'offset': 3,
                   'limit': 1, 'physical_page': 7, 'section': 'Late section'}]}
    parsed = ExecutionCoordinator.parse_routing_intent(json.dumps(payload))
    assert parsed['read_requests'] == payload['read_requests']
    assert parsed['read_attachment_ids'] == ['doc']
    payload['read_requests'][0]['path'] = 'forged.docx'
    with pytest.raises(ValueError):
        ExecutionCoordinator.parse_routing_intent(json.dumps(payload))


def test_document_retry_reauthorizes_actual_project_evidence_into_child_run(tmp_path):
    from agent.rag.service import RagService
    from agent.rag.grounding import RagGrounding, quote_options
    from test_rag_service import Engines, ready, wait_job
    async def scenario():
        core = CoreRepository(tmp_path / 'evidence.db')
        project = core.create_project('P', 'work')
        rag = RagService(core, Engines())
        service = ChatRunService.__new__(ChatRunService)
        service.rag, service.rag_grounding = rag, RagGrounding(rag)
        try:
            library = (await rag.handle('library.create', {'name': 'Rules', 'embedding_model_id': 'embed', 'reranker_model_id': 'rank'}))['library']['id']
            await ready(rag, library)
            assert (await wait_job(rag, await rag.handle('index.build', {'library_id': library})))['status'] == 'completed'
            await rag.handle('library.bind', {'library_id': library, 'project_id': project['id'], 'enabled': True})
            prior = await rag.search(project['id'], '失效', run_id='parent')
            source = prior['evidence'][0]
            quote = quote_options(source['text'])[0]
            state = {'project_id': project['id'], 'generation': prior['generation'], 'aliases': {}, 'evidence': {},
                     'admitted': {}, 'quote_options': {}}
            service.rag_grounding.runs['child'] = state
            field = service._reauthorize_document_field('child', {'text': 'Content',
                'evidence': [{'id': source['id'], 'quote_id': quote['quote_id']}]})
            current_id = field['evidence'][0]['id']
            assert current_id != source['id']
            assert field['evidence'][0]['quote_id'] == quote['quote_id']
            assert state['evidence'][current_id]['run_id'] == 'child'
            assert state['aliases'][source['id']] == current_id
            assert state['quote_options'][current_id][quote['quote_id']] == quote['text']
            rag.validate_evidence(project['id'], [current_id], run_id='child')
        finally:
            await rag.close()
    asyncio.run(scenario())


def test_batch_schema_protects_headings_spans_and_attachment_identity():
    from agent.document_contracts import document_batch_schema
    task = {'stage': 'plan', 'fields': [{'id': 'p-610', 'field_type': 'heading'},
                                       {'id': 'p-611', 'field_type': 'content'}]}
    schema = document_batch_schema(task, ['authorized'])
    target = {'target_id': 'p-611', 'field_type': 'content', 'parameter_keys': [], 'span': None}
    response = {'response': {'targets': [target], 'equipment_parameters': [], 'gaps': []}}
    assert schema_accepts(response, schema)
    target['target_id'] = 'p-610'
    assert not schema_accepts(response, schema)
    target.update(target_id='p-611', span=False)
    assert not schema_accepts(response, schema)
    reading = {'attachment_id': 'authorized', 'offset': 0, 'limit': 1, 'physical_page': None, 'section': None}
    assert schema_accepts({'response': {'read_requests': [reading]}}, schema)
    reading['attachment_id'] = 'forged'
    assert not schema_accepts({'response': {'read_requests': [reading]}}, schema)
    def check_closed_objects(node):
        if isinstance(node, dict):
            if node.get('type') == 'object':
                assert node.get('additionalProperties') is False
                assert set(node['required']) == set(node['properties'])
            for value in node.values():
                check_closed_objects(value)
        elif isinstance(node, list):
            for value in node:
                check_closed_objects(value)
    check_closed_objects(schema)


def schema_accepts(value, schema):
    """Validate the JSON Schema keywords emitted by the captured application contract."""
    if 'anyOf' in schema:
        return any(schema_accepts(value, alternative) for alternative in schema['anyOf'])
    types = schema.get('type', [])
    types = [types] if isinstance(types, str) else types
    actual = ('null' if value is None else 'boolean' if type(value) is bool else 'object' if isinstance(value, dict) else 'array' if isinstance(value, list)
              else 'string' if isinstance(value, str) else 'integer' if type(value) is int else 'unknown')
    if actual not in types or 'enum' in schema and value not in schema['enum']:
        return False
    if actual == 'object':
        properties = schema.get('properties', {})
        return (set(schema.get('required', [])) <= set(value)
                and (schema.get('additionalProperties', True) or set(value) <= set(properties))
                and all(schema_accepts(item, properties[key]) for key, item in value.items() if key in properties))
    if actual == 'array':
        return (schema.get('minItems', 0) <= len(value) <= schema.get('maxItems', float('inf'))
                and all(schema_accepts(item, schema['items']) for item in value))
    if actual == 'string':
        return schema.get('minLength', 0) <= len(value) <= schema.get('maxLength', float('inf'))
    if actual == 'integer':
        return schema.get('minimum', float('-inf')) <= value <= schema.get('maximum', float('inf'))
    return True


def test_strict_schema_rag_text_only_reads_late_page_before_grounded_answer(tmp_path, monkeypatch):
    import json
    import httpx
    from test_tool_continuation_retry import fixture, configuration
    from test_rag_service import Engines, ready, wait_job
    from agent.rag.service import RagService
    from agent.rag.grounding import RagGrounding
    monkeypatch.setenv('AGENT_DATA_DIR', str(tmp_path))
    original = httpx.AsyncClient
    captured = []
    holder = {}
    def respond(request):
        payload = json.loads(request.content)
        captured.append(payload)
        assert payload['response_format']['type'] == 'json_schema'
        assert payload['response_format']['json_schema']['strict'] is True
        schema = payload['response_format']['json_schema']['schema']
        if len(captured) == 1:
            result = {'response': {'read_requests': [{'attachment_id': 'late', 'offset': 1, 'limit': 1,
                                                     'physical_page': 2, 'section': None}]}}
            assert not schema_accepts({'response': {'read_requests': [{'attachment_id': 'forbidden',
                'offset': 1, 'limit': 1, 'physical_page': 2, 'section': None}]}}, schema)
        else:
            assert 'LATE-PAGE-SECOND' in json.dumps(payload['messages'])
            state = next(iter(holder['service'].rag_grounding.runs.values()))
            identity = next(iter(state['admitted']))
            quote_id, quote = next(iter(state['quote_options'][identity].items()))
            result = {'response': {'claims': [{'text': quote, 'evidence': [{'id': identity, 'quote_id': quote_id}]}]}}
        assert schema_accepts(result, schema), 'Strict provider schema rejects the advertised attachment read/answer contract'
        chunk = {'choices': [{'delta': {'content': json.dumps(result, ensure_ascii=False)}, 'finish_reason': 'stop'}]}
        return httpx.Response(200, text='data: '+json.dumps(chunk)+'\n\ndata: [DONE]\n\n')
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    class Parser:
        async def parse(self, path):
            return {'chunks': [{'text': 'LATE-PAGE-FIRST', 'locator': {'page': 2}},
                               {'text': 'LATE-PAGE-SECOND', 'locator': {'page': 2}}], 'warnings': []}
    async def scenario():
        async def sink(event): pass
        config = configuration()
        config['base_url'] = 'http://127.0.0.1:19999/v1'
        config['model_tool_modes']['model'] = 'text_only'
        core, project, work, service = await fixture(tmp_path, sink, config)
        holder['service'] = service
        await service.rag.close()
        service.rag = RagService(core, Engines())
        service.rag_grounding = RagGrounding(service.rag)
        service.context.rag_context = service.rag_grounding.evidence
        service.document_attachments.parser = Parser()
        async def intent(**kwargs):
            return {'web': 'off', 'web_evidence': 'optional', 'reasoning': 'standard',
                'task_kinds': ['attachment_summary'], 'explanation_code': 'grounded_attachment',
                'rag_requirement': 'required', 'read_attachment_ids': []}, None
        service._preflight_intent_advisor = intent
        try:
            library = (await service.rag.handle('library.create', {'name': 'Rules', 'embedding_model_id': 'embed', 'reranker_model_id': 'rank'}))['library']['id']
            await ready(service.rag, library)
            assert (await wait_job(service.rag, await service.rag.handle('index.build', {'library_id': library})))['status'] == 'completed'
            await service.rag.handle('library.bind', {'library_id': library, 'project_id': project['id'], 'enabled': True})
            attachment = runtime_attachment(tmp_path, 'late', 'Late document')
            response = await service.start({'conversation_id': work['id'], 'content': 'Read the late equipment page and explain safety requirements using project evidence',
                'attachments': [attachment], 'idempotency_key': 'strict-read'})
            await service.tasks[response['run']['id']]
            run = core.get_model_run(response['run']['id'])
            assert run['status'] == 'completed', run.get('error_message')
            assert len(captured) == 2
            assert run['rag_decision']['document_reads'][-1]['result']['blocks'][0]['text'].strip() == 'LATE-PAGE-SECOND'
            plan = service.execution_coordinator.get_for_run(run['id'])
            step = next(item for item in plan['steps'] if item['step_type'] == 'final_response')
            spec = service._create_model_invocation_spec(run, plan, step, round_no=99, attempt_no=1,
                allowed_tools=(), max_output_tokens=512, response_only=True, read_attachment_ids=('late',))
            final_schema = json.loads(spec.response_schema_json)
            assert set(final_schema['properties']) == {'claims'}
            assert not schema_accepts({'read_requests': [{'attachment_id': 'late'}]}, final_schema)
            plain_spec = service._create_model_invocation_spec(run, plan, step, round_no=100, attempt_no=1,
                allowed_tools=(), max_output_tokens=512)
            assert set(json.loads(plain_spec.response_schema_json)['properties']) == {'claims'}
        finally:
            await service.shutdown()
    asyncio.run(scenario())
