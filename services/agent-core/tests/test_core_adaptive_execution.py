import asyncio

import pytest

from agent.storage import CoreDataError, CoreRepository


def test_intent_adaptive_wait_ignores_legacy_elapsed_budget(tmp_path):
    from agent.execution.execution_wait import ExecutionMonitor
    from agent.execution.intent_session import IntentService, current_intent
    from agent.migrations.v32_execution_wait import migrate

    async def exercise():
        repository = CoreRepository(tmp_path / 'core.db')
        with repository._connect() as connection:
            migrate(connection)
        monitor = ExecutionMonitor(repository)

        async def start(params):
            execution = current_intent.get()
            execution.service._patch(execution.identity, execution.revision, active_seconds=10000)
            return await execution.classify(lambda mode: asyncio.sleep(0, result={'mode': mode}))

        service = IntentService(repository, start, monitor=monitor)
        try:
            initial = (await service.handle('start', {'request': {'content': '读取', 'idempotency_key': 'adaptive'}}))['task']
            await service.wait(initial['id'])
            task = service.get(initial['id'])
            assert task['status'] == 'completed'
            assert task['budget_seconds'] is None
            assert task['execution_task_id'] == initial['execution_task_id']
            assert monitor.get_task(task['execution_task_id'])['status'] == 'completed'
        finally:
            await service.close()

    asyncio.run(exercise())


def test_local_prepared_invocation_waits_past_legacy_first_output_deadline(tmp_path):
    from dataclasses import replace
    from agent.execution.compiler import InvocationCompiler
    from agent.execution.execution_wait import ExecutionMonitor
    from agent.model_invocation import ModelInvocationService
    from agent.providers import ProviderEvent, ProviderProfile
    from test_unified_invocation_v3 import model_spec

    async def exercise():
        repository = CoreRepository(tmp_path / 'core.db')
        monitor = ExecutionMonitor(repository)
        root = monitor.start_task(run_id='local-model')

        class Provider:
            profile = ProviderProfile('managed:test', 'local', 'openai_compatible', 'http://127.0.0.1/v1',
                                      primary_protocol='openai_responses', local_strict=True)

            async def stream_events(self, *args, **kwargs):
                await asyncio.sleep(.04)
                yield ProviderEvent('text_delta', text='complete')
                yield ProviderEvent('finish', finish_reason='stop')

        spec = model_spec(total_timeout_ms=10, first_output_timeout_ms=5)
        prepared = InvocationCompiler().compile_model(spec, [{'role': 'user', 'content': 'hello'}])
        with monitor.associate(root['id']):
            content, _, _ = await ModelInvocationService().complete_prepared(Provider(), spec, prepared)
        assert content == 'complete'
        assert monitor.get_task(root['id'])['stage'] == 'model_output'

    asyncio.run(exercise())


def test_model_residency_starts_after_last_run_release(tmp_path):
    from agent.local_models.service import LocalModelService

    service = LocalModelService(CoreRepository(tmp_path / 'core.db'))
    calls = []

    class Runtime:
        def touch(self):
            calls.append('released')

    service._runtime = Runtime()
    service._run_pins['run'] = 'model'
    service.release_run('run')
    assert calls == ['released']
    assert not service._run_pins


def test_monitored_main_model_loading_exceeds_old_load_horizon(tmp_path):
    from agent.execution.execution_wait import ExecutionMonitor
    from test_local_models_catalog import finish, service_at
    from test_local_models_runtime import fake_runtime, imported

    async def exercise():
        service = service_at(tmp_path, runtime_root=fake_runtime(tmp_path / 'runtime'), load_timeout=.001)
        model = await imported(service, tmp_path / 'model.gguf')
        monitor = ExecutionMonitor(service.repository)
        service.execution_monitor = monitor
        try:
            response = await service.handle('runtime.load', {'model_id': model['id']})
            job = await finish(service, response)
            assert job['status'] == 'completed', job.get('error')
            root = monitor.get_task(response['execution_task_id'])
            assert root['stage'] == 'main_model_loading'
            assert root['status'] == 'completed'
            assert root['progress']['details'].get('health_status') == 200
        finally:
            await service.close()

    asyncio.run(exercise())


@pytest.mark.parametrize('code', ['rag_rerank_timeout', 'rag_worker_crash', 'execution_stalled',
                                  'execution_recovery_exhausted', 'rag_document_timeout', 'ocr_runtime_missing'])
def test_typed_retrieval_failure_keeps_reason_without_model_explanation(code):
    from agent.execution.run_recovery import recovery_allowed, safe_code
    error = CoreDataError(code, 'preserved')
    assert safe_code(error) == code
    assert not recovery_allowed(error, partial='', operations=[])


def test_full_template_review_uses_validated_model_workflow_without_implicit_export():
    import json
    from agent.execution.coordinator import ExecutionCoordinator
    from agent.rag.routing import decide_evidence, merge_evidence_intent

    returned = {'web': 'off', 'reasoning': 'standard', 'task_kinds': ['airworthiness'],
        'explanation_code': 'full_review', 'rag_requirement': 'required', 'task_scope': 'full',
        'read_attachment_ids': ['reference'], 'document_workflow': 'review_nonempty'}
    parsed = ExecutionCoordinator.parse_routing_intent(json.dumps(returned))
    decision = merge_evidence_intent(decide_evidence('check attached content'), parsed)
    assert decision['template_review'] and decision['required']
    assert decision['task_type'] == 'evidence_answer'


def test_intent_and_business_run_share_root_and_ordered_event_sequence(tmp_path):
    from agent.runtime import ChatRunService

    async def exercise():
        repository = CoreRepository(tmp_path / 'core.db')
        events = []

        async def emit(value):
            events.append(value)

        service = ChatRunService(repository, emit)
        try:
            intent = (await service.intent_sessions.handle('start', {'request': {
                'content': 'hello', 'kind': 'standard_chat', 'category': 'casual',
                'idempotency_key': 'same-root', 'provider_profile_id': 'builtin-demo', 'model_id': 'demo'}}))['task']
            await service.intent_sessions.wait(intent['id'])
            saved = service.intent_sessions.get(intent['id'])
            assert saved['status'] == 'completed', saved.get('error')
            result = saved['result']
            task = service.tasks.get(result['run']['id'])
            if task:
                await task
            assert result['execution_task_id'] == intent['execution_task_id']
            root = service.execution_status({'owner_kind': 'run', 'owner_id': result['run']['id']})
            assert root['id'] == intent['execution_task_id']
            assert root['status'] == 'completed'
            from agent.protocol import PROTOCOL_VERSION, handle_message
            for request_type, params in [('model.run.get', {'run_id': result['run']['id']}),
                                         ('model.run.get', {'conversation_id': result['run']['conversation_id']}),
                                         ('model.run.find', {'idempotency_key': 'same-root'})]:
                restored = handle_message({'v': PROTOCOL_VERSION, 'id': 'restore', 'type': request_type,
                    'params': params}, repository)
                assert restored['result']['run']['execution_task_id'] == intent['execution_task_id']
            await asyncio.sleep(0)
            run_events = [event for event in events if event['run_id'] == result['run']['id']]
            sequences = [event['sequence'] for event in run_events]
            assert sequences == sorted(set(sequences))
            assert any(event['event_type'] == 'execution.activity' for event in run_events)
        finally:
            await service.shutdown()

    asyncio.run(exercise())


def test_hard_cancel_wakes_paused_business_stage_without_generic_failure(tmp_path, monkeypatch):
    from agent.execution.activity import run_stage
    from agent.runtime import ChatRunService

    async def exercise():
        repository = CoreRepository(tmp_path / 'core.db')
        paused = asyncio.Event()
        events = []

        async def emit(value):
            events.append(value)

        service = ChatRunService(repository, emit)

        async def generate(*args, **kwargs):
            async def stalled(activity):
                service.execution_monitor._pause(activity, 'execution_stalled')
                paused.set()
                await asyncio.Future()
            return await run_stage('model_output', stalled)

        monkeypatch.setattr(service, '_generate_with_tools', generate)
        try:
            result = await service.start({'content': 'hello', 'kind': 'standard_chat', 'category': 'casual',
                'idempotency_key': 'pause-cancel', 'provider_profile_id': 'builtin-demo', 'model_id': 'demo'})
            await asyncio.wait_for(paused.wait(), 5)
            root = service.execution_status({'owner_kind': 'run', 'owner_id': result['run']['id']})
            assert repository.get_model_run(result['run']['id'])['status'] == 'waiting_recovery'
            task = service.tasks[result['run']['id']]
            cancelled = await service.handle_execution('cancel', {'task_id': root['id'], 'revision': root['revision'],
                'idempotency_key': 'cancel-paused'})
            await asyncio.gather(task, return_exceptions=True)
            assert cancelled['task']['status'] == 'cancelled'
            assert repository.get_model_run(result['run']['id'])['status'] == 'cancelled'
            assert not any(event['event_type'] == 'run.failed' for event in events)
        finally:
            await service.shutdown()

    asyncio.run(exercise())


def test_process_liveness_and_flat_cpu_keep_gpu_activity_unknown(monkeypatch):
    from agent.execution import activity

    async def exercise():
        observations, heartbeats = [], []

        class Process:
            def poll(self):
                return None

        class Activity:
            def resource(self, **value):
                observations.append(value)

            def heartbeat(self, **value):
                heartbeats.append(value)

        async def tick(seconds):
            if len(observations) >= 2:
                raise asyncio.CancelledError

        monkeypatch.setattr(activity, 'process_counters', lambda process, job=None: {'cpu_ticks': 0, 'io_bytes': 0})
        monkeypatch.setattr(activity.asyncio, 'sleep', tick)
        with pytest.raises(asyncio.CancelledError):
            await activity.watch_process(Process(), Activity())
        assert all(value['active'] is None for value in observations)
        assert all(value['process_alive'] for value in observations)
        assert heartbeats == []

    asyncio.run(exercise())


def test_parser_adaptive_exchange_accepts_only_matching_worker_heartbeats():
    import io
    from types import SimpleNamespace
    from agent.document_parser import ParserService
    from agent.document_parser.protocol import encode_frame
    from agent.execution.execution_wait import current_activity

    observed = []

    class Activity:
        def heartbeat(self, **details):
            observed.append(details)

    class Input:
        def write(self, value):
            pass

        def close(self):
            pass

    request = encode_frame({'request_id': 'request', 'adaptive_wait': True})
    complete = encode_frame({'request_id': 'request', 'status': 'complete'})
    process = SimpleNamespace(stdin=Input(), stdout=io.BytesIO(
        encode_frame({'request_id': 'request', 'status': 'heartbeat'}) + complete))
    token = current_activity.set(Activity())
    try:
        assert ParserService._exchange(process, request) == complete
    finally:
        current_activity.reset(token)
    assert observed == [{'worker': 'document_parser'}]


@pytest.mark.parametrize('linked_retry', [False, True])
def test_normal_model_selected_review_covers_reference_docx_without_export(tmp_path, monkeypatch, linked_retry):
    import hashlib
    import io
    import json
    import httpx
    from docx import Document
    from agent.rag.grounding import RagGrounding
    from agent.rag.service import RagService
    from test_document_attachments import source_attachment
    from test_rag_service import Engines, ready, wait_job
    from test_tool_continuation_retry import configuration, fixture

    original_client = httpx.AsyncClient
    batches = []

    def respond(request):
        payload = json.loads(request.content)
        value = json.loads(payload['messages'][-1]['content'])
        batch, sources = value['batch'], value['sources']['evidence']
        batches.append(batch)
        assert sources
        source = sources[0]
        response = {'results': [{'target_id': field['id'], 'status': 'reviewed',
            'summary': '依据本批受控资料核对，适用性仍需人工确认。', 'problems': [],
            'evidence': [{'id': source['id'], 'quote_id': source['quote_options'][0]['quote_id']}],
            'version_issues': [], 'gaps': []} for field in batch['fields']]}
        chunk = {'choices': [{'delta': {'content': json.dumps(response)}, 'finish_reason': 'stop'}]}
        return httpx.Response(200, text='data: ' + json.dumps(chunk) + '\n\ndata: [DONE]\n\n')

    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs:
        original_client(transport=httpx.MockTransport(respond), **kwargs))

    async def exercise():
        checkpoint_count = 0
        cancellation = None

        async def emit(value):
            nonlocal checkpoint_count, cancellation
            if (linked_retry and cancellation is None and value['event_type'] == 'execution.activity'
                    and value['data'].get('stage') == 'document_review' and value['data'].get('event') == 'checkpoint'):
                checkpoint_count += 1
                if checkpoint_count == 2:
                    cancellation = asyncio.create_task(service.cancel(value['run_id']))

        config = configuration()
        config['base_url'] = 'http://127.0.0.1:19999/v1'
        config['model_tool_modes']['model'] = 'text_only'
        core, project, work, service = await fixture(tmp_path, emit, config)
        await service.rag.close()
        service.rag = RagService(core, Engines())
        service.rag_grounding = RagGrounding(service.rag)
        service.document_attachments.parser = service.rag.parser
        service.rag.execution_monitor = service.execution_monitor
        document = Document()
        document.add_heading('技术内容', 1)
        paragraph_count = 36
        for index in range(paragraph_count):
            document.add_paragraph('检查项目 ' + str(index))
        table = document.add_table(rows=1, cols=2)
        table.cell(0, 0).text = '说明：核对所引用版本'
        table.cell(0, 1).text = '示例：设备参数'
        payload = io.BytesIO()
        document.save(payload)
        attachment = source_attachment(tmp_path, payload.getvalue(), 'attached.docx')
        attachment['purpose'] = 'reference'
        original_hash = attachment['sha256']

        async def advisor(**params):
            return {'web': 'off', 'web_evidence': 'optional', 'reasoning': 'standard',
                'task_kinds': ['airworthiness'], 'explanation_code': 'review_document',
                'rag_requirement': 'required', 'read_attachment_ids': [attachment['id']],
                'task_scope': 'full', 'document_workflow': 'review_nonempty'}, None

        service._preflight_intent_advisor = advisor
        try:
            library = (await service.rag.handle('library.create', {'name': 'Rules',
                'embedding_model_id': 'embed', 'reranker_model_id': 'rank'}))['library']['id']
            await ready(service.rag, library)
            assert (await wait_job(service.rag, await service.rag.handle('index.build',
                {'library_id': library})))['status'] == 'completed'
            await service.rag.handle('library.bind', {'library_id': library, 'project_id': project['id'], 'enabled': True})
            result = await service.start({'conversation_id': work['id'], 'content': '请检查附件中的每个现有位置',
                'attachments': [attachment], 'idempotency_key': 'semantic-review'})
            await asyncio.gather(service.tasks[result['run']['id']], return_exceptions=True)
            run = core.get_model_run(result['run']['id'])
            if linked_retry:
                assert cancellation is not None
                await cancellation
                assert run['status'] == 'cancelled'
                parent_root = service.execution_status({'owner_kind': 'run', 'owner_id': run['id']})
                with core._connect() as connection:
                    saved = connection.execute('SELECT payload_json FROM execution_wait_checkpoints WHERE task_id=? AND stage=?',
                        (parent_root['id'], 'document_review')).fetchall()
                saved_targets = {target for row in saved for target in json.loads(row['payload_json'])['target_ids']}
                assert saved_targets
                batches.clear()
                retried = await service.retry(run['id'])
                await service.tasks[retried['id']]
                run = core.get_model_run(retried['id'])
                assert saved_targets.isdisjoint({field['id'] for batch in batches for field in batch['fields']})
                assert service.execution_monitor.get_task(parent_root['id'])['status'] == 'cancelled'
            assert run['status'] == 'completed', (run['error_code'], run['error_message'])
            assert run['rag_decision']['template_review']
            assert run['rag_decision']['task_type'] == 'evidence_answer'
            assert len(batches) >= 2 and all(len(batch['fields']) <= 10 for batch in batches)
            if not linked_retry:
                assert len(batches) >= 4
                assert len({field['id'] for batch in batches for field in batch['fields']}) == paragraph_count + 2
                with core._connect() as connection:
                    invocation_count = connection.execute('SELECT COUNT(*) FROM invocation_specs i JOIN execution_plans p ON p.id=i.plan_id WHERE p.run_id=?',
                        (run['id'],)).fetchone()[0]
                assert invocation_count == len(batches)
            assert hashlib.sha256((tmp_path / attachment['storage_key']).read_bytes()).hexdigest() == original_hash
            assert not any(operation['tool_name'] == 'document.export' for operation in service.tool_loop.repository.list_for_run(run['id']))
            assert str(paragraph_count + 3) in run['assistant_message']['content']
            root = service.execution_status({'owner_kind': 'run', 'owner_id': run['id']})
            assert root['status'] == 'completed'
        finally:
            await service.shutdown()

    asyncio.run(exercise())


def test_execution_protocol_owner_lookup_and_revision_control(tmp_path):
    from agent.protocol import PROTOCOL_VERSION, handle_async_message
    from agent.runtime import ChatRunService

    async def exercise():
        events = []

        async def emit(value):
            events.append(value)

        service = ChatRunService(CoreRepository(tmp_path / 'core.db'), emit)
        try:
            root = service.execution_monitor.start_task(intent_task_id='intent-test')
            query = {'v': PROTOCOL_VERSION, 'id': 'query', 'type': 'execution.request',
                     'params': {'action': 'status', 'params': {'owner_kind': 'intent', 'owner_id': 'intent-test'}}}
            found = await handle_async_message(query, service)
            assert found['result']['task']['id'] == root['id']
            cancel = {**query, 'id': 'cancel', 'params': {'action': 'cancel', 'params': {
                'task_id': root['id'], 'revision': root['revision'], 'idempotency_key': 'cancel-once'}}}
            first = await handle_async_message(cancel, service)
            second = await handle_async_message(cancel, service)
            assert first['result'] == second['result']
            assert first['result']['task']['status'] == 'cancelled'
        finally:
            await service.shutdown()

    asyncio.run(exercise())


def test_parser_context_keeps_size_limits_and_ignores_legacy_timeout(tmp_path, monkeypatch):
    from agent.document_parser import ParserService
    from agent.execution.execution_wait import ExecutionMonitor
    from agent.migrations.v32_execution_wait import migrate
    from test_document_parser import managed

    async def exercise():
        repository = CoreRepository(tmp_path / 'core.db')
        with repository._connect() as connection:
            migrate(connection)
        monitor = ExecutionMonitor(repository)
        root = monitor.start_task(run_id='parser')
        parser = ParserService(tmp_path / 'rag', timeout=.001)
        original = managed(parser.data_dir)

        async def parse(original, stage, request_id):
            await asyncio.sleep(.02)
            return {'chunks': ['complete'], 'warnings': []}

        monkeypatch.setattr(parser, '_execute', parse)
        try:
            with monitor.associate(root['id']):
                assert (await parser.parse(original))['chunks'] == ['complete']
            assert parser.result_limit > 0 and parser.memory_limit > 0
            assert list(parser.staging.iterdir()) == []
        finally:
            await parser.close()

    asyncio.run(exercise())
