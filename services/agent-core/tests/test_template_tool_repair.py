import asyncio
import json

import pytest

from agent.document_contracts import DOCUMENT_DEFINITIONS, validate_document_call
from agent.storage import CoreDataError


@pytest.mark.parametrize('identities', [[], ['attachment_id'], ['template_id'], ['path'],
                                      ['attachment_id', 'template_id'], ['attachment_id', 'template_id', 'path']])
def test_template_schema_admits_exactly_one_source_identity(identities):
    parameters = DOCUMENT_DEFINITIONS[0]['function']['parameters']
    alternatives = parameters.get('oneOf', [])
    matches = sum(set(branch['required']) <= set(identities) for branch in alternatives)
    assert (matches == 1) == (len(identities) == 1)


def test_template_identity_error_carries_a_static_correction_constraint():
    with pytest.raises(CoreDataError) as failure:
        validate_document_call('document.inspect_template', {
            'attachment_id': 'attachment', 'template_id': 'template', 'path': 'file.docx'})
    assert failure.value.details == {'tool_argument_constraint': 'template_identity'}


def test_template_identity_fields_explain_where_their_values_come_from():
    properties = DOCUMENT_DEFINITIONS[0]['function']['parameters']['properties']
    assert 'uploaded attachment' in properties['attachment_id'].get('description', '')
    assert 'previous inspection' in properties['template_id'].get('description', '')
    assert 'project-relative' in properties['path'].get('description', '')
    assert 'never an attachment ID' in properties['path'].get('description', '')


@pytest.mark.parametrize('attachments', [(), ('authorized-attachment',)])
def test_deferred_evidence_schema_never_sends_an_empty_enum(attachments):
    from agent.rag.grounding import RagGrounding

    grounding = RagGrounding(None)
    grounding.runs['run'] = {'admitted': {}, 'quote_options': {}}
    if not attachments:
        with pytest.raises(CoreDataError) as failure:
            grounding.answer_schema('run')
        assert failure.value.code == 'rag_evidence_missing'
    else:
        schema = grounding.answer_schema('run', read_attachment_ids=attachments)
        assert '"enum": []' not in json.dumps(schema)
        alternatives = schema['properties']['response']['anyOf']
        assert len(alternatives) == 1 and 'read_requests' in alternatives[0]['properties']


def native_call(name, arguments, identity):
    return {'tool_calls': [{'index': 0, 'id': identity, 'type': 'function',
                           'function': {'name': name, 'arguments': json.dumps(arguments)}}]}


def test_refresh_catalog_preserves_other_calls_in_the_same_batch():
    from agent.execution.task_tools import refresh_discovery_context

    messages = [
        {'role': 'user', 'content': 'Keep the original goal.'},
        {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': 'catalog', 'function': {'name': 'tools.discover'}},
            {'id': 'write', 'function': {'name': 'fs.propose_changes'}}]},
        {'role': 'tool', 'tool_call_id': 'catalog', 'name': 'tools.discover', 'content': 'old directory'},
        {'role': 'tool', 'tool_call_id': 'write', 'name': 'fs.propose_changes', 'content': 'published hash'},
        {'role': 'assistant', 'content': 'Keep meaningful progress.'},
    ]
    refresh_discovery_context(messages)
    assert messages[0] == {'role': 'user', 'content': 'Keep the original goal.'}
    assert [call['id'] for call in messages[1]['tool_calls']] == ['write']
    assert messages[2]['tool_call_id'] == 'write' and messages[2]['content'] == 'published hash'
    assert messages[3]['content'] == 'Keep meaningful progress.'


def test_native_template_correction_explains_source_exclusivity(tmp_path, monkeypatch):
    from test_tool_continuation_retry import fixture, install_http, start

    replies = [
        (native_call('tools_discover', {'names': ['document.inspect_template']}, 'discover'), 'tool_calls'),
        (native_call('document_inspect_template', {'attachment_id': 'RAW-ID-SENTINEL',
             'template_id': 'RAW-ID-SENTINEL', 'path': 'RAW-ID-SENTINEL'}, 'invalid'), 'tool_calls'),
        ({'content': 'The source selection needs one identity.'}, 'stop'),
    ]
    requests = install_http(monkeypatch, replies, goal_checks=True)

    async def scenario():
        async def sink(event):
            pass
        core, _, work, service = await fixture(tmp_path, sink)
        try:
            run_id = await start(service, work)
            assert core.get_model_run(run_id)['status'] == 'completed'
            correction = '\n'.join(item.get('content') or '' for item in requests[2][1]['messages'])
            assert 'attachment_id, template_id, or path' in correction
            assert 'omit the other two' in correction
            assert 'RAW-ID-SENTINEL' not in correction
        finally:
            await service.shutdown()
    asyncio.run(scenario())


def test_catalog_refresh_preserves_enabled_schemas_and_bounds_repeated_results(tmp_path, monkeypatch):
    from test_tool_continuation_retry import configuration, fixture, install_http

    replies = [(native_call('tools_discover', {'names': ['document.inspect_template']}, 'enable'), 'tool_calls')]
    replies += [(native_call('tools_discover', {}, 'catalog-' + str(index)), 'tool_calls') for index in range(6)]
    replies += [({'content': 'Capabilities inspected.'}, 'stop')]
    requests = install_http(monkeypatch, replies)

    async def scenario():
        async def sink(event):
            pass
        config = configuration()
        config['base_url'] = 'http://127.0.0.1:19999/v1'
        config['model_capabilities']['model']['context_length'] = 8192
        core, _, work, service = await fixture(tmp_path, sink, config)
        try:
            started = await service.start({'conversation_id': work['id'], 'content': '你有哪些实际可用工具？',
                                           'idempotency_key': 'catalog-refresh'})
            await service.tasks[started['run']['id']]
            run = core.get_model_run(started['run']['id'])
            assert run['status'] == 'completed', (run['error_code'], run['error_message'])
            assert len(requests) == 8
            for _, request in requests[1:]:
                assert 'document_inspect_template' in {item['function']['name'] for item in request['tools']}
            messages = requests[-1][1]['messages']
            catalogs = [item for item in messages if item.get('role') == 'tool' and
                        item.get('name') in {'tools.discover', 'tools_discover'}]
            assert len([item for item in catalogs if 'available_names' in item['content'] or
                        '"tools"' in item['content']]) <= 1
            assert len(service.list_tool_operations(started['run']['id'])) == 7
        finally:
            await service.shutdown()
    asyncio.run(scenario())


def test_tool_turn_rebalances_output_reserve_for_a_fitting_complete_request(tmp_path, monkeypatch):
    from test_tool_continuation_retry import configuration, fixture, install_http

    requests = install_http(monkeypatch, [
        (native_call('tools_discover', {'names': ['document.inspect_template']}, 'enable'), 'tool_calls'),
        (native_call('tools_discover', {}, 'catalog'), 'tool_calls'),
        ({'content': 'Available capabilities verified.'}, 'stop'),
    ])

    async def scenario():
        async def sink(event):
            pass
        config = configuration()
        config['base_url'] = 'http://127.0.0.1:19999/v1'
        config['model_capabilities']['model']['context_length'] = 8192
        core, _, work, service = await fixture(tmp_path, sink, config)
        try:
            content = '你有哪些实际可用工具？' + '背景说明。' * 300
            started = await service.start({'conversation_id': work['id'], 'content': content,
                                           'idempotency_key': 'reserve-rebalance'})
            await service.tasks[started['run']['id']]
            run = core.get_model_run(started['run']['id'])
            assert run['status'] == 'completed', (run['error_code'], run['error_message'])
            assert len(requests) == 3
            assert any(512 <= request['max_tokens'] < 2048 for _, request in requests)
            assert any(item.get('role') == 'user' and content in item.get('content', '')
                       for item in requests[-1][1]['messages'])
            assert len(service.list_tool_operations(started['run']['id'])) == 2
        finally:
            await service.shutdown()
    asyncio.run(scenario())


def test_small_window_inspection_budgets_the_actual_tool_result(tmp_path, monkeypatch):
    import io
    from docx import Document
    from test_document_attachments import source_attachment
    from test_tool_continuation_retry import configuration, fixture, install_http

    monkeypatch.setenv('AGENT_DATA_DIR', str(tmp_path))
    document = Document()
    document.add_paragraph('设备环境鉴定模板元数据')
    output = io.BytesIO()
    document.save(output)
    attachment = source_attachment(tmp_path, output.getvalue(), '设备鉴定模板.docx')
    requests = install_http(monkeypatch, [
        (native_call('tools_discover', {'names': ['document.inspect_template']}, 'discover'), 'tool_calls'),
        (native_call('document_inspect_template', {'attachment_id': 'doc', 'limit': 1}, 'inspect'), 'tool_calls'),
        ({'content': '模板元数据已读取。'}, 'stop'),
    ], goal_checks=True)

    async def scenario():
        async def sink(event):
            if event['event_type'] == 'run.plan_ready':
                run = core.get_model_run(event['run_id'])
                current = next(item for item in core.get_run_conversation(run['id'])['messages'] if item['id'] == run['user_message_id'])
                root = service.execution_monitor.start_task(run_id=run['id'])
                if service.task_plans.get(root['id']) is None:
                    service.task_plans.create(root['id'], current)
                scope = {'source_id': 'doc', 'operation': 'read', 'scope': 'metadata', 'target_ids': [], 'reason': '只读取元数据'}
                criteria = ['返回模板元数据']
                service.task_plans.update(root['id'], expected_revision=0, steps=[], success_criteria=criteria,
                    source_scopes=[scope], deliverables=[], expected_goal_revision=0,
                    goal_requirements={'source_scopes': [scope], 'deliverables': [], 'success_criteria': criteria})
        config = configuration()
        config['base_url'] = 'http://127.0.0.1:19999/v1'
        config['model_capabilities']['model']['context_length'] = 8192
        core, _, work, service = await fixture(tmp_path, sink, config)
        try:
            started = await service.start({'conversation_id': work['id'], 'content': '只读取上传模板的元数据。',
                'attachments': [attachment], 'idempotency_key': 'small-inspection'})
            await service.tasks[started['run']['id']]
            run = core.get_model_run(started['run']['id'])
            operations = service.list_tool_operations(run['id'])
            inspected = next(item for item in operations if item['tool_name'] == 'document.inspect_template')
            assert inspected['status'] == 'completed', inspected
            assert run['status'] == 'completed', (run['error_code'], run['error_message'])
            assert len(requests) == 4
        finally:
            await service.shutdown()
    asyncio.run(scenario())
