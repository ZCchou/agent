import asyncio

import pytest

from agent.storage import CoreDataError, CoreRepository


def test_capability_query_reason_keeps_semantic_tool_intent_consistent():
    import json
    from agent.execution.coordinator import ExecutionCoordinator

    value = {'files': 'none', 'tools': 'available', 'web': 'off', 'web_evidence': 'optional',
        'reasoning': 'standard', 'task_kinds': ['general'], 'rag_requirement': 'not_needed',
        'explanation_code': 'capability_query', 'follow_up': 'none'}
    result = ExecutionCoordinator.parse_routing_intent(json.dumps(value))
    assert 'capability_query' in result['task_kinds']


def test_shared_probe_is_persisted_and_reused_after_restart(tmp_path):
    from agent.tool_verification import ToolVerification

    repository = CoreRepository(tmp_path / 'core.db')
    calls = []

    async def probe():
        calls.append(True)
        await asyncio.sleep(0)
        return {'tool_mode': 'native', 'verified': True}

    async def scenario():
        service = ToolVerification(repository)
        results = await asyncio.gather(service.ensure('identity', probe), service.ensure('identity', probe))
        assert [value['tool_mode'] for value in results] == ['native', 'native']
        assert results[0]['status'] == 'verified'
        restored = await ToolVerification(repository).ensure('identity', probe)
        assert restored['status'] == 'verified'
        assert len(calls) == 1
        await service.ensure('changed-template', probe)
        assert len(calls) == 2

    asyncio.run(scenario())


def test_native_protocol_rejection_uses_structured_probe(tmp_path, monkeypatch):
    import json
    from agent.runtime import ChatRunService
    from agent.providers import ProviderError, ProviderEvent

    async def scenario():
        async def emit(event):
            pass

        service = ChatRunService(CoreRepository(tmp_path / 'core.db'), emit)
        service.configure_provider({'id': 'local', 'name': 'Local', 'kind': 'openai_compatible',
            'base_url': 'http://127.0.0.1:9999/v1', 'allowed_model_ids': ['model']})

        class Provider:
            async def stream_events(self, model_id, messages, *, tools, options):
                if tools:
                    raise ProviderError('unsupported_capability', 'native tools unavailable')
                yield ProviderEvent('text_delta', text=json.dumps({
                    'type': 'tool_call', 'id': 'probe', 'name': 'fs.list', 'arguments': {'path': '.'}}))

        monkeypatch.setattr(service, '_provider', lambda profile: Provider())
        try:
            assert await service.probe_tool_capability('local', 'model') == {'tool_mode': 'structured', 'verified': True}
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_failed_probe_preserves_incomplete_state_and_manual_retry(tmp_path):
    from agent.tool_verification import ToolVerification

    service = ToolVerification(CoreRepository(tmp_path / 'core.db'))
    calls = []

    async def unavailable():
        calls.append(True)
        raise CoreDataError('insufficient_vram', 'no memory')

    async def scenario():
        with pytest.raises(CoreDataError):
            await service.ensure('identity', unavailable)
        state = service.get('identity')
        assert state['status'] == 'incomplete'
        assert state['error']['code'] == 'insufficient_vram'
        with pytest.raises(CoreDataError):
            await service.ensure('identity', unavailable)
        assert len(calls) == 2

        async def available():
            return {'tool_mode': 'structured', 'verified': True}

        assert (await service.ensure('identity', available))['tool_mode'] == 'structured'

    asyncio.run(scenario())


@pytest.mark.parametrize('probe_crash', [False, True])
def test_first_managed_tool_question_verifies_before_final_plan(tmp_path, monkeypatch, probe_crash):
    from agent.runtime import ChatRunService
    from test_managed_adaptive_recovery import recovering_runtime
    from test_local_models_runtime import imported
    from test_local_models_catalog import finish

    async def scenario():
        async def emit(event):
            pass

        runtime, _ = recovering_runtime(tmp_path, monkeypatch, failures=0)
        service = ChatRunService(CoreRepository(tmp_path / 'core.db'), emit)
        service.local_models.runtime_root = runtime
        model = await imported(service.local_models, tmp_path / 'model.gguf')
        job = await service.handle_local_model('runtime.load', {'model_id': model['id']})
        assert (await finish(service.local_models, job))['status'] == 'completed'
        service._sync_managed_profiles()
        project = service.repository.create_project('Tools', 'work')
        probes = []

        async def identify(**kwargs):
            return {'files': 'none', 'tools': 'available', 'web': 'off', 'web_evidence': 'optional',
                'reasoning': 'standard', 'task_kinds': ['capability_query'], 'rag_requirement': 'not_needed',
                'explanation_code': 'capabilities'}, None

        async def probe(profile_id, model_id):
            probes.append(model_id)
            if probe_crash and len(probes) == 1:
                raise CoreDataError('runtime_crashed', 'probe worker exited')
            return service._tool_probe_result(profile_id, 'native', True)

        service._preflight_intent_advisor = identify
        service._probe_tool_capability = probe
        try:
            result = await service.start({'content': '你有什么工具可以调用', 'kind': 'project_work',
                'project_id': project['id'], 'provider_profile_id': model['provider_profile_id'],
                'model_id': model['id'], 'idempotency_key': 'first-tools'})
            run = result['run']
            await service.run_tasks[run['id']]
            assert service.repository.get_model_run(run['id'])['status'] == 'completed'
            assert probes == [model['id']] * (2 if probe_crash else 1)
            plan = service.execution_repository.get_for_run(run['id'])
            assert plan['resolved_intent']['tool_verification']['status'] == 'verified'
            assert any('function_tools' in step['capability_signature'] for step in plan['steps'])
            assert service.execution_monitor.get_task(service.tool_probe_task_ids[model['provider_profile_id']])['status'] == 'completed'
        finally:
            await service.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize('task_kind', ['attachment_extract', 'attachment_summary', 'document_export'])
def test_semantic_document_task_verifies_tools_with_available_requirements(tmp_path, monkeypatch, task_kind):
    from agent.runtime import ChatRunService
    from test_managed_adaptive_recovery import recovering_runtime
    from test_local_models_runtime import imported
    from test_local_models_catalog import finish

    async def scenario():
        async def emit(event):
            pass

        runtime, _ = recovering_runtime(tmp_path, monkeypatch, failures=0)
        service = ChatRunService(CoreRepository(tmp_path / 'core.db'), emit)
        service.local_models.runtime_root = runtime
        model = await imported(service.local_models, tmp_path / 'model.gguf')
        await finish(service.local_models, await service.handle_local_model('runtime.load', {'model_id': model['id']}))

        async def identify(**kwargs):
            return {'files': 'available', 'tools': 'available', 'web': 'off', 'web_evidence': 'optional',
                'reasoning': 'standard', 'task_kinds': [task_kind], 'rag_requirement': 'not_needed',
                'explanation_code': 'document_task'}, None

        async def probe(*args, **kwargs):
            raise CoreDataError('probe_observed', 'Document task reached tool verification')

        service._preflight_intent_advisor = identify
        service._ensure_tool_capability = probe
        try:
            with pytest.raises(CoreDataError, match='Document task reached tool verification'):
                await service.start({'content': '处理这个文件', 'kind': 'standard_chat', 'category': 'casual',
                    'provider_profile_id': model['provider_profile_id'], 'model_id': model['id'],
                    'idempotency_key': 'document-tool-probe'})
        finally:
            await service.shutdown()

    asyncio.run(scenario())
