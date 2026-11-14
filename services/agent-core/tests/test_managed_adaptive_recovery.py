import asyncio
import json
import pytest

from agent.runtime import ChatRunService
from agent.storage import CoreRepository
from test_local_models_catalog import finish
from test_local_models_runtime import fake_runtime, imported


def recovering_runtime(tmp_path, monkeypatch, failures, crash_requests=(), partial_crash=False):
    import test_local_models_runtime as fixtures
    script = fixtures._SERVER
    start = script.index('    def do_POST(self):')
    end = script.index("HTTPServer(('127.0.0.1'", start)
    counter = tmp_path / 'requests.json'
    post = '''    def do_POST(self):
        from pathlib import Path
        payload = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
        counter = Path(COUNTER)
        records = json.loads(counter.read_text()) if counter.exists() else []
        records.append(os.getpid())
        counter.write_text(json.dumps(records))
        try:
            value = json.loads(payload['messages'][-1]['content'])
        except (ValueError, TypeError):
            value = {}
        if 'batch' in value:
            batches = counter.with_name('batches.json')
            previous = json.loads(batches.read_text()) if batches.exists() else []
            previous.append([field['id'] for field in value['batch']['fields']])
            batches.write_text(json.dumps(previous))
        failing = len(records) <= FAILURES or len(records) in CRASH_REQUESTS
        if failing and not PARTIAL_CRASH:
            os._exit(23)
        content = 'Recovered answer.'
        schema = payload.get('response_format', {}).get('json_schema', {}).get('schema', {})
        if 'task_kinds' in schema.get('properties', {}):
            identifiers = schema['properties']['read_attachment_ids']['items'].get('enum', [])
            content = json.dumps({'files': 'available', 'tools': 'available', 'web': 'off', 'web_evidence': 'optional',
                'reasoning': 'standard', 'task_kinds': ['attachment_extract'] if identifiers else ['general'],
                'explanation_code': 'managed_intent', 'follow_up': 'none', 'rag_requirement': 'not_needed',
                'read_attachment_ids': identifiers[:1], 'read_requests': [], 'task_scope': 'unspecified',
                'retrieval_scope': 'focused', 'document_workflow': 'none', 'allow_simulated_data': False, 'clarification': None})
        elif 'batch' in value:
            source = value['sources']['evidence'][0]
            content = json.dumps({'results': [{'target_id': field['id'], 'status': 'reviewed',
                'summary': 'Draft review requires human verification.', 'problems': [], 'version_issues': [], 'gaps': [],
                'evidence': [{'id': source['id'], 'quote_id': source['quote_options'][0]['quote_id']}]}
                for field in value['batch']['fields']]})
        data = ('data: ' + json.dumps({'choices': [{'delta': {'content': content}, 'finish_reason': 'stop'}]}) + '\\n\\ndata: [DONE]\\n\\n').encode()
        if failing and PARTIAL_CRASH:
            data = ('data: ' + json.dumps({'choices': [{'delta': {'content': content}, 'finish_reason': None}]}) + '\\n\\n').encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        self.wfile.flush()
        if failing and PARTIAL_CRASH:
            receipt = counter.with_name('output-delivered')
            deadline = time.monotonic() + 10
            while not receipt.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            os._exit(23)
'''.replace('COUNTER', repr(str(counter))).replace('FAILURES', str(failures)).replace('CRASH_REQUESTS', repr(list(crash_requests))).replace('PARTIAL_CRASH', repr(partial_crash))
    if partial_crash:
        post = post.replace("'finish_reason': 'stop'", "'finish_reason': None")
        post = post.replace("self.send_header('Content-Length', str(len(data)))", "self.send_header('Transfer-Encoding', 'chunked')")
        post = post.replace('self.wfile.write(data)', "self.wfile.write((format(len(data), 'x') + '\\r\\n').encode() + data + b'\\r\\n')")
    monkeypatch.setattr(fixtures, '_SERVER', script[:start] + post + script[end:])
    return fake_runtime(tmp_path / 'runtime'), counter


@pytest.mark.parametrize('failures', [1, 3])
def test_managed_engine_death_before_first_output_reloads_and_redispatches_normal_run(tmp_path, monkeypatch, failures):
    async def exercise():
        events = []
        paused = asyncio.Event()

        async def emit(value):
            events.append(value)
            if value['event_type'] == 'execution.activity' and value['data']['status'] == 'waiting_recovery':
                paused.set()

        root, counter = recovering_runtime(tmp_path, monkeypatch, failures=failures)
        core = CoreRepository(tmp_path / 'core.db')
        service = ChatRunService(core, emit)
        service.local_models.runtime_root = root
        model = await imported(service.local_models, tmp_path / 'model.gguf')
        assert (await finish(service.local_models, await service.local_models.handle('runtime.load',
            {'model_id': model['id']})))['status'] == 'completed'
        service.local_models.set_tool_mode(model['provider_profile_id'], 'text_only')

        async def classify(**params):
            return {'files': 'none', 'tools': 'none', 'web': 'off', 'web_evidence': 'optional',
                'reasoning': 'standard', 'task_kinds': ['general'], 'rag_requirement': 'not_needed',
                'explanation_code': 'managed_recovery'}, None

        service._preflight_intent_advisor = classify
        try:
            response = await service.start({'content': 'hello', 'kind': 'standard_chat', 'category': 'casual',
                'idempotency_key': 'recover-managed', 'provider_profile_id': model['provider_profile_id'],
                'model_id': model['id']})
            task = service.run_tasks[response['run']['id']]
            if failures == 3:
                await asyncio.wait_for(paused.wait(), 20)
                activity = service.execution_status({'owner_kind': 'run', 'owner_id': response['run']['id']})
                assert activity['auto_recoveries'] == 2
                assert len(json.loads(counter.read_text())) == 3
                assert not task.done()
                await service.handle_execution('retry', {'task_id': activity['id'], 'revision': activity['revision'],
                    'idempotency_key': 'manual-after-exhaustion'})
            await asyncio.wait_for(task, 20)
            run = core.get_model_run(response['run']['id'])
            assert run['status'] == 'completed', (run['error_code'], run['error_message'])
            assert run['assistant_message']['content'] == 'Recovered answer.'
            requests = json.loads(counter.read_text())
            assert len(requests) == failures + 1 and len(set(requests)) == len(requests)
            with core._connect() as connection:
                specs = connection.execute('SELECT i.status FROM invocation_specs i JOIN execution_plans p ON p.id=i.plan_id WHERE p.run_id=? ORDER BY i.round_no',
                    (run['id'],)).fetchall()
            assert [row['status'] for row in specs] == ['failed'] * failures + ['completed']
            assert not service.tool_loop.repository.list_for_run(run['id'])
            activity = service.execution_status({'owner_kind': 'run', 'owner_id': run['id']})
            assert activity['status'] == 'completed'
            assert sum(event['event_type'] == 'execution.activity' and event['data']['event'] == 'recovery'
                       for event in events) == min(failures, 2)
        finally:
            await service.shutdown()

    asyncio.run(exercise())


@pytest.mark.parametrize('scenario', ['partial', 'configuration_changed'])
def test_managed_recovery_preserves_partial_output_and_rejects_configuration_drift(tmp_path, monkeypatch, scenario):
    async def exercise():
        events = []
        changed = False

        async def emit(value):
            nonlocal changed
            events.append(value)
            if scenario == 'partial' and value['event_type'] == 'run.chunk':
                counter.with_name('output-delivered').write_text('received')
            if scenario == 'configuration_changed' and not changed and value['event_type'] == 'run.invocation_failed':
                changed = True
                record = service.local_models.catalog.get(model['id'])
                service.local_models.catalog.configure(model['id'], {'threads': record['threads'] + 1})

        runtime, counter = recovering_runtime(tmp_path, monkeypatch, failures=1, partial_crash=scenario == 'partial')
        core = CoreRepository(tmp_path / 'core.db')
        service = ChatRunService(core, emit)
        service.local_models.runtime_root = runtime
        model = await imported(service.local_models, tmp_path / 'model.gguf')
        assert (await finish(service.local_models, await service.local_models.handle('runtime.load',
            {'model_id': model['id']})))['status'] == 'completed'
        service.local_models.set_tool_mode(model['provider_profile_id'], 'text_only')

        async def classify(**params):
            return {'files': 'none', 'tools': 'none', 'web': 'off', 'web_evidence': 'optional',
                'reasoning': 'standard', 'task_kinds': ['general'], 'rag_requirement': 'not_needed',
                'explanation_code': 'managed_recovery'}, None

        service._preflight_intent_advisor = classify
        try:
            response = await service.start({'content': 'hello', 'kind': 'standard_chat', 'category': 'casual',
                'idempotency_key': 'recover-' + scenario, 'provider_profile_id': model['provider_profile_id'], 'model_id': model['id']})
            await asyncio.wait_for(service.run_tasks[response['run']['id']], 20)
            run = core.get_model_run(response['run']['id'])
            assert run['status'] == 'failed'
            assert len(json.loads(counter.read_text())) == 1
            if scenario == 'partial':
                assert 'Recovered answer.' in run['draft']['content']
                assert not any(event['event_type'] == 'execution.activity' and event['data'].get('event') == 'recovery'
                               for event in events)
                activity = service.execution_status({'owner_kind': 'run', 'owner_id': run['id']})
                await service.handle_execution('retry', {'task_id': activity['id'], 'revision': activity['revision'],
                    'idempotency_key': 'explicit-partial-run-retry'})
                await asyncio.wait_for(service.run_tasks[run['id']], 20)
                assert core.get_model_run(run['id'])['status'] == 'completed'
                assert len(json.loads(counter.read_text())) == 2
            else:
                assert run['error_code'] == 'model_recovery_identity_changed'
            assert not service.tool_loop.repository.list_for_run(run['id'])
        finally:
            await service.shutdown()

    asyncio.run(exercise())


def test_managed_engine_death_during_intent_restarts_same_root_before_run(tmp_path, monkeypatch):
    from test_document_runtime_plan import runtime_attachment

    async def exercise():
        async def emit(value):
            pass

        runtime, counter = recovering_runtime(tmp_path, monkeypatch, failures=1)
        core = CoreRepository(tmp_path / 'core.db')
        service = ChatRunService(core, emit)
        service.local_models.runtime_root = runtime
        model = await imported(service.local_models, tmp_path / 'model.gguf')
        assert (await finish(service.local_models, await service.local_models.handle('runtime.load',
            {'model_id': model['id']})))['status'] == 'completed'
        service.local_models.set_tool_mode(model['provider_profile_id'], 'text_only')
        attachment = runtime_attachment(tmp_path, 'reference', 'Authorized equipment values')
        try:
            task = (await service.intent_sessions.handle('start', {'request': {
                'content': '请概括附件内容', 'attachments': [attachment], 'kind': 'standard_chat', 'category': 'casual',
                'idempotency_key': 'recover-intent', 'provider_profile_id': model['provider_profile_id'],
                'model_id': model['id']}}))['task']
            await asyncio.wait_for(service.intent_sessions.wait(task['id']), 20)
            state = service.intent_sessions.get(task['id'])
            assert state['status'] == 'completed', state.get('error')
            assert state['attempts'] == 2
            run_id = state['result']['run']['id']
            running = service.run_tasks.get(run_id)
            if running:
                await asyncio.wait_for(running, 20)
            run = core.get_model_run(run_id)
            assert run['status'] == 'completed', (run['error_code'], run['error_message'])
            assert state['execution_task_id'] == service.execution_status({'owner_kind': 'run', 'owner_id': run_id})['id']
            requests = json.loads(counter.read_text())
            assert len(requests) == 3 and requests[0] != requests[1] == requests[2]
        finally:
            await service.shutdown()

    asyncio.run(exercise())


def test_second_review_batch_engine_death_resumes_first_checkpoint_under_same_root(tmp_path, monkeypatch):
    import io
    from docx import Document
    from agent.rag.grounding import RagGrounding
    from agent.rag.service import RagService
    from test_document_attachments import source_attachment
    from test_rag_service import Engines, ready, wait_job

    async def exercise():
        events = []

        async def emit(value):
            events.append(value)

        runtime, counter = recovering_runtime(tmp_path, monkeypatch, failures=0, crash_requests=[2])
        core = CoreRepository(tmp_path / 'core.db')
        project = core.create_project('Review', 'work')
        conversation = core.create_conversation('project_work', 'Review', project_id=project['id'])
        service = ChatRunService(core, emit)
        service.local_models.runtime_root = runtime
        model = await imported(service.local_models, tmp_path / 'model.gguf')
        assert (await finish(service.local_models, await service.local_models.handle('runtime.load',
            {'model_id': model['id']})))['status'] == 'completed'
        service.local_models.set_tool_mode(model['provider_profile_id'], 'text_only')
        core.update_conversation_routing(conversation['id'], 'fixed', 'balanced', model['provider_profile_id'], model['id'])
        await service.rag.close()
        service.rag = RagService(core, Engines())
        service.rag.execution_monitor = service.execution_monitor
        service.rag_grounding = RagGrounding(service.rag)
        service.document_attachments.parser = service.rag.parser
        document = Document()
        document.add_heading('Technical review', 1)
        for index in range(36):
            document.add_paragraph('Requirement ' + str(index))
        payload = io.BytesIO()
        document.save(payload)
        attachment = source_attachment(tmp_path, payload.getvalue(), 'reference.docx')
        attachment['purpose'] = 'reference'

        async def classify(**params):
            return {'files': 'read', 'tools': 'none', 'web': 'off', 'web_evidence': 'optional',
                'reasoning': 'standard', 'task_kinds': ['airworthiness'], 'rag_requirement': 'required',
                'task_scope': 'full', 'read_attachment_ids': [attachment['id']],
                'document_workflow': 'review_nonempty', 'explanation_code': 'review'}, None

        service._preflight_intent_advisor = classify
        try:
            library = (await service.rag.handle('library.create', {'name': 'Rules',
                'embedding_model_id': 'embed', 'reranker_model_id': 'rank'}))['library']['id']
            await ready(service.rag, library)
            assert (await wait_job(service.rag, await service.rag.handle('index.build', {'library_id': library})))['status'] == 'completed'
            await service.rag.handle('library.bind', {'library_id': library, 'project_id': project['id'], 'enabled': True})
            response = await service.start({'content': 'Review every existing item.', 'conversation_id': conversation['id'],
                'attachments': [attachment], 'idempotency_key': 'recover-review'})
            await asyncio.wait_for(service.run_tasks[response['run']['id']], 30)
            run = core.get_model_run(response['run']['id'])
            assert run['status'] == 'completed', (run['error_code'], run['error_message'])
            batches = json.loads(counter.with_name('batches.json').read_text())
            assert len(batches) >= 5 and batches.count(batches[0]) == 1
            assert batches[1] == batches[2]
            assert len(set(target for batch in batches for target in batch)) == 36
            activity = service.execution_status({'owner_kind': 'run', 'owner_id': run['id']})
            assert activity['id'] == response['execution_task_id'] and activity['status'] == 'completed'
            with core._connect() as connection:
                saved = connection.execute('SELECT COUNT(*) FROM execution_wait_checkpoints WHERE task_id=? AND stage=?',
                    (activity['id'], 'document_review')).fetchone()[0]
                calls = connection.execute('SELECT i.status FROM invocation_specs i JOIN execution_plans p ON p.id=i.plan_id WHERE p.run_id=?',
                    (run['id'],)).fetchall()
            assert saved >= 4
            assert len(calls) == len(batches) and sum(row['status'] == 'failed' for row in calls) == 1
            assert not service.tool_loop.repository.list_for_run(run['id'])
        finally:
            await service.shutdown()

    asyncio.run(exercise())
