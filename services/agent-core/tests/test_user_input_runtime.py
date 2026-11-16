import asyncio
import hashlib
import json

import pytest
from types import SimpleNamespace

from agent.runtime import ChatRunService
from agent.execution.input_sources import UserInputSources
from agent.execution.execution_wait import ExecutionMonitor
from agent.execution.task_plans import TaskPlans
from agent.tool_contracts import ToolCall
from agent.storage import CoreDataError
from test_input_sources import source_run


def working_summary_reply(payload, notes):
    supplied = json.loads(payload['messages'][-1]['content'])
    return json.dumps({'notes': notes, **supplied['protected_record_ids']})


def runtime(core):
    service = ChatRunService.__new__(ChatRunService)
    service.repository = core
    service.user_input_sources = UserInputSources(core.database_path)
    service.execution_monitor = ExecutionMonitor(core)
    service.task_plans = TaskPlans(core)
    return service


def test_saved_input_tool_initializes_search_and_bounds_escaped_json(tmp_path):
    core, run = source_run(tmp_path, 'prefix' + '\x01' * 3000 + 'unique requirement')
    service = runtime(core)
    found = json.loads(service._saved_result_tool(run['id'], ToolCall('search', 'tools.search_result',
        {'result_id': 'user-input', 'query': 'unique requirement'}), 1200))
    assert found['matches'][0]['offset'] == 3006
    page = service._saved_result_tool(run['id'], ToolCall('read', 'tools.read_result',
        {'result_id': 'user-input', 'length': 1000}), 1200)
    assert len(page) <= 1200
    assert json.loads(page)['next_cursor']


def test_grounded_input_schema_preserves_claims_and_allows_application_read(tmp_path):
    from agent.rag.grounding import RagGrounding
    core, run = source_run(tmp_path, 'Read my original input')
    service = runtime(core)
    service.rag_grounding = RagGrounding(None)
    service.rag_grounding.runs[run['id']] = {'admitted': {'e': {}}, 'quote_options': {'e': {'q': {}}}}
    service._managed_runtime_identity = lambda _profile: None
    service.tool_loop = SimpleNamespace(repository=SimpleNamespace(list_for_run=lambda _run: []))
    step = {'id': 'step', 'provider_profile_id': 'p', 'model_id': 'm', 'protocol_id': 'openai_chat',
            'capability_signature': ['text']}
    plan = {'id': 'plan'}
    kwargs = dict(round_no=1, attempt_no=1, allowed_tools=(), max_output_tokens=512)
    original = service._create_model_invocation_spec(run, plan, step, **kwargs)
    expanded = service._create_model_invocation_spec(run, plan, step, allow_user_input_read=True, **kwargs)
    schema = json.loads(expanded.response_schema_json)
    alternatives = schema['properties']['response']['anyOf']
    assert json.loads(original.response_schema_json) in alternatives
    assert any('user_input_read' in item.get('properties', {}) for item in alternatives)
    final = service._create_model_invocation_spec(run, plan, step, allow_user_input_read=True, response_only=True, **kwargs)
    assert json.loads(final.response_schema_json) == json.loads(original.response_schema_json)


def test_large_multimodal_input_projects_text_and_keeps_image_budget(tmp_path, monkeypatch):
    from agent.context_engine.service import ContextEngineService
    from agent.storage import CoreRepository
    core = CoreRepository(tmp_path / 'image.db')
    monkeypatch.setenv('AGENT_DATA_DIR', str(tmp_path))
    content = 'read every requirement ' + '原始要求🌟 ' * 2400 + 'Final requirement'
    image = b'png-data'
    path = tmp_path / 'message-attachments' / 'image.png'
    path.parent.mkdir()
    path.write_bytes(image)
    run = core.start_model_run(content=content, idempotency_key='image-input', provider_profile_id='p',
        model_id='m', kind='standard_chat', category='casual', attachments=[{'id': 'image', 'kind': 'image',
        'name': 'image.png', 'media_type': 'image/png', 'storage_key': 'message-attachments/image.png',
        'byte_size': len(image), 'sha256': hashlib.sha256(image).hexdigest(), 'width': 1, 'height': 1}])
    packed = ContextEngineService(core, source_reads_enabled=True).assemble(run, context_window=8192, max_output_tokens=1024,
        serialization_identity={'protocol': 'openai_chat', 'capability_signature': ['text', 'vision']})
    current = next(message for message in reversed(packed.messages) if message['role'] == 'user')
    assert isinstance(current['content'], list)
    assert json.loads(current['content'][0]['text'])['result_id'] == 'user-input'
    assert current['content'][1]['type'] == 'image'
    assert UserInputSources(core.database_path).read(run['id'], length=16000)['content'] == content


def test_deferred_input_advisor_uses_read_pending_without_provider_call():
    from agent.providers import ProviderProfile
    service = ChatRunService.__new__(ChatRunService)
    profile = ProviderProfile('p', 'Local', 'openai_compatible', 'http://127.0.0.1:1',
        model_capabilities={'m': {'context_length': 8192}})
    def fail_provider(_profile):
        raise AssertionError('Projected input must defer semantic classification')
    service._provider = fail_provider
    content = 'read original requirements ' + '原始要求🌟 ' * 4000
    intent, error = asyncio.run(service._preflight_intent_advisor(content=content, attachments=[],
        profile=profile, model_id='m', conversation_kind='standard_chat', recent_messages=[]))
    assert error is None
    assert intent['defer_interpretation']
    assert intent['tools'] == 'available'
    assert intent['rag_requirement'] == 'uncertain'


@pytest.mark.parametrize('repeated', [False, True])
def test_text_only_input_read_recovers_invalid_request_and_detects_repetition(tmp_path, monkeypatch, repeated):
    import httpx
    from goal_check_support import goal_check_content
    from test_tool_continuation_retry import fixture, configuration
    original = httpx.AsyncClient
    dispatched = []
    def respond(request):
        payload = json.loads(request.content)
        checked = goal_check_content(payload)
        if checked is not None:
            content = checked
        elif any(message['role'] == 'system' and 'Summarize completed work, decisions' in str(message['content'])
                 for message in payload['messages']):
            content = working_summary_reply(payload, 'Original requirements remain available as user-input. Continue its latest cursor.')
        else:
            dispatched.append(payload)
            reads = []
            for message in payload['messages']:
                if message['role'] != 'user' or not isinstance(message['content'], str):
                    continue
                try:
                    value = json.loads(message['content'])
                except ValueError:
                    continue
                if isinstance(value, dict) and value.get('result_id') == 'user-input' and 'offset' in value:
                    reads.append(value)
            if len(dispatched) == 1:
                content = json.dumps({'user_input_read': []})
            elif repeated or not reads:
                content = json.dumps({'response': {'user_input_read': {'length': 512, 'cursor': None}}})
            elif reads[-1]['next_cursor']:
                content = json.dumps({'user_input_read': {'length': 512, 'cursor': reads[-1]['next_cursor']}})
            else:
                content = 'Completed all original requirements.'
        chunk = {'choices': [{'delta': {'content': content}, 'finish_reason': 'stop'}]}
        return httpx.Response(200, text='data: ' + json.dumps(chunk) + '\n\ndata: [DONE]\n\n')
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    async def scenario():
        config = configuration()
        config['model_tool_modes']['model'] = 'text_only'
        config['model_capabilities']['model']['context_length'] = 8192
        async def sink(event):
            pass
        core, _, work, service = await fixture(tmp_path, sink, config)
        try:
            response = await service.start({'conversation_id': work['id'],
                'content': 'Process every original instruction. ' + '原始要求🌟 ' * 1000 + 'Final requirement.',
                'idempotency_key': 'input-read'})
            await service.tasks[response['run']['id']]
            run = core.get_model_run(response['run']['id'])
            if repeated:
                assert run['status'] == 'failed'
                assert run['error_code'] == 'task_no_progress'
                assert any('same input page was read three times' in str(payload['messages']) for payload in dispatched)
            else:
                assert run['status'] == 'completed', run.get('error_message')
                assert run['assistant_message']['content'] == 'Completed all original requirements.'
                assert any('input read request was rejected' in str(payload['messages']) for payload in dispatched)
        finally:
            await service.shutdown()
    asyncio.run(scenario())


def test_native_two_page_compaction_summarizes_original_requirements_before_discard(tmp_path, monkeypatch):
    import httpx
    import agent.runtime as runtime_module
    from goal_check_support import goal_check_content
    from test_tool_continuation_retry import fixture, configuration
    marker = 'KEEP-ORIGINAL-REQUIREMENT-ALPHA'
    original_client = httpx.AsyncClient
    original_estimate = runtime_module.estimate_tokens
    summaries = []
    business = []

    def under_pressure(value):
        if isinstance(value, dict) and set(value) == {'model', 'messages', 'tools', 'response_schema'}:
            pages = [message for message in value['messages'] if message.get('role') == 'tool']
            if len(pages) == 2:
                return 100000
        return original_estimate(value)

    monkeypatch.setattr(runtime_module, 'estimate_tokens', under_pressure)

    def respond(request):
        payload = json.loads(request.content)
        checked = goal_check_content(payload)
        finish = 'stop'
        if checked is not None:
            delta = {'content': checked}
        elif any('Summarize completed work, decisions' in str(message.get('content')) for message in payload['messages']):
            supplied = json.loads(payload['messages'][-1]['content'])
            summaries.append(supplied)
            prior = supplied['previous_notes']
            if marker in prior:
                note = prior
            elif marker in supplied['next_history_piece']['content']:
                note = marker
            else:
                note = 'Continue reading original source.'
            delta = {'content': working_summary_reply(payload, note)}
        else:
            business.append(payload)
            pages = []
            for message in payload['messages']:
                if message.get('role') != 'tool':
                    continue
                try:
                    value = json.loads(message['content'])
                except (ValueError, TypeError):
                    continue
                if isinstance(value, dict) and value.get('source') == 'original_user_message':
                    pages.append(value)
            if pages and pages[-1]['next_cursor'] is None:
                delta = {'content': 'Completed all original requirements.'}
            else:
                arguments = {'result_id': 'user-input', 'length': 1000}
                if pages:
                    arguments['cursor'] = pages[-1]['next_cursor']
                delta = {'tool_calls': [{'index': 0, 'id': 'original-' + str(len(business)), 'type': 'function',
                    'function': {'name': 'tools_read_result', 'arguments': json.dumps(arguments)}}]}
                finish = 'tool_calls'
        return httpx.Response(200, text='data: ' + json.dumps({'choices': [{'delta': delta, 'finish_reason': finish}]}) + '\n\ndata: [DONE]\n\n')

    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original_client(transport=httpx.MockTransport(respond), **kwargs))

    async def scenario():
        config = configuration()
        config['model_capabilities']['model']['context_length'] = 8192
        async def sink(event):
            pass
        core, _, _, service = await fixture(tmp_path, sink, config)
        try:
            chat = core.create_conversation('standard_chat', 'Original input', category='casual')
            core.update_conversation_routing(chat['id'], 'fixed', 'balanced', 'synthetic', 'model')
            content = 'Read all requirements. ' + '原始要求🌟 ' * 100 + marker + '背景要求🌟 ' * 900 + 'Final requirement.'
            response = await service.start({'conversation_id': chat['id'], 'content': content, 'idempotency_key': 'native-preserve-input'})
            await service.tasks[response['run']['id']]
            run = core.get_model_run(response['run']['id'])
            assert run['status'] == 'completed', run.get('error_message')
            assert marker not in next(message['content'] for message in business[0]['messages'] if message['role'] == 'user')
            assert any(marker in source['next_history_piece']['content'] for source in summaries)
            assert any(marker in str(message.get('content')) and 'Derived working notes' in str(message.get('content'))
                       for message in business[-1]['messages'])
            with core._connect() as connection:
                outcome = connection.execute("SELECT outcome FROM task_work_items WHERE source_id='file:user-input'").fetchone()[0]
            assert outcome == 'done'
        finally:
            await service.shutdown()
    asyncio.run(scenario())


def test_forced_evidence_long_input_reads_original_before_model_selected_search(tmp_path, monkeypatch):
    import httpx
    from goal_check_support import goal_check_content
    from test_tool_continuation_retry import fixture, configuration
    from agent.rag.grounding import quote_options
    from agent.rag.repository import required_text
    original_client = httpx.AsyncClient
    evidence_text = 'Equipment safety requirements must be verified before operation.'
    quote_id = quote_options(evidence_text)[0]['quote_id']
    queries = []
    active_service = None

    def respond(request):
        payload = json.loads(request.content)
        checked = goal_check_content(payload)
        if checked is not None:
            content = checked
        elif any('Summarize completed work, decisions' in str(message.get('content')) for message in payload['messages']):
            content = working_summary_reply(payload, 'Read the complete original input and then search project evidence for safety requirements.')
        else:
            pages = []
            for message in payload['messages']:
                if message.get('role') != 'user':
                    continue
                try:
                    value = json.loads(message['content'])
                except (ValueError, TypeError):
                    continue
                if isinstance(value, dict) and value.get('source') == 'original_user_message':
                    pages.append(value)
            if not pages or pages[-1]['next_cursor']:
                content = json.dumps({'response': {'user_input_read': {'length': 1000,
                    'cursor': pages[-1]['next_cursor'] if pages else None}}})
            elif not next(iter(active_service.rag_grounding.runs.values()))['admitted']:
                content = json.dumps({'response': {'evidence_query': {'query': 'equipment safety requirements'}}})
            else:
                content = json.dumps({'response': {'claims': [{'text': evidence_text,
                    'evidence': [{'id': 'evidence-long-input', 'quote_id': quote_id}]}]}})
        return httpx.Response(200, text='data: ' + json.dumps({'choices': [{'delta': {'content': content}, 'finish_reason': 'stop'}]}) + '\n\ndata: [DONE]\n\n')

    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original_client(transport=httpx.MockTransport(respond), **kwargs))

    async def scenario():
        nonlocal active_service
        config = configuration()
        config['base_url'] = 'http://127.0.0.1:18765/v1'
        config['model_tool_modes']['model'] = 'text_only'
        config['model_capabilities']['model']['context_length'] = 8192
        async def sink(event):
            pass
        core, project, work, service = await fixture(tmp_path, sink, config)
        active_service = service
        with core._connect() as connection:
            connection.execute('INSERT INTO rag_projects(project_id) VALUES (?)', (project['id'],))
        service.rag.project_state = lambda _project: {'enabled': True, 'generation': 0}
        service.rag.assert_generation = lambda *_args: None
        service.rag.validate_evidence = lambda *_args, **_kwargs: None
        async def search(project_id, query, **kwargs):
            required_text(query, 'Search query', 2000)
            with core._connect() as connection:
                outcome = connection.execute("SELECT outcome FROM task_work_items WHERE source_id='file:user-input'").fetchone()
            assert outcome and outcome[0] == 'done'
            queries.append(query)
            return {'generation': 0, 'evidence': [{'id': 'evidence-long-input', 'text': evidence_text,
                'title': 'Equipment requirements', 'revision': 'A', 'clause_number': '1', 'version_id': 'version',
                'locator': {'page': 1}, 'text_hash': hashlib.sha256(evidence_text.encode()).hexdigest()}]}
        service.rag.search = search
        try:
            response = await service.start({'conversation_id': work['id'], 'rag_mode': 'required',
                'content': 'Use project evidence for every requirement. ' + '原始要求🌟 ' * 1000 + 'Preserve final requirement.',
                'idempotency_key': 'forced-long-input'})
            await service.tasks[response['run']['id']]
            run = core.get_model_run(response['run']['id'])
            assert run['status'] == 'completed', (run.get('error_code'), run.get('error_message'))
            assert queries == ['equipment safety requirements']
            assert run['rag_decision']['required']
            assert 'rag-evidence:evidence-long-input' in run['assistant_message']['content']
            assert any(operation['tool_name'] == 'rag.search' for operation in service.list_tool_operations(run['id']))
        finally:
            await service.shutdown()
    asyncio.run(scenario())


def test_native_evidence_search_requires_complete_original_input_coverage(tmp_path):
    core, run = source_run(tmp_path, 'Original requirements ' * 500)
    service = runtime(core)
    service.user_input_sources.save(run['id'])
    queries = []
    async def execute(_run, _name, arguments, _limit):
        queries.append(arguments['query'])
        return '{"evidence":[]}'
    service.rag_grounding = SimpleNamespace(runs={run['id']: {}}, execute=execute)
    call = ToolCall('evidence', 'rag.search', {'query': 'focused requirements'})
    async def scenario():
        with pytest.raises(CoreDataError) as failure:
            await service._rag_tool(run, call, 4000)
        assert failure.value.code == 'user_input_read_required'
        assert queries == []
        cursor = None
        while True:
            arguments = {'result_id': 'user-input', 'length': 1000}
            if cursor:
                arguments['cursor'] = cursor
            page = json.loads(service._saved_result_tool(run['id'], ToolCall('read', 'tools.read_result', arguments)))
            cursor = page['next_cursor']
            if cursor is None:
                break
        await service._rag_tool(run, call, 4000)
        assert queries == ['focused requirements']
    asyncio.run(scenario())
