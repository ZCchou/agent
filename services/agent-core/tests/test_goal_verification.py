import asyncio
import json

import pytest

from agent.execution.task_plans import TaskPlans
from agent.storage import CoreDataError
from test_goal_requirements import setup_task, requirements, scope


def verifier(core):
    import importlib.util
    assert importlib.util.find_spec('agent.execution.goal_verification'), 'Final goal verification is missing'
    from agent.execution.goal_verification import GoalVerification
    return GoalVerification(core)


def answer(status='satisfied', *, goals=None, unmet=None, questions=None):
    return {'status': status, 'summary': '已核对原始要求和实际成果',
            'unmet_requirements': unmet or [], 'questions': questions or [], 'goal_requirements': goals,
            'evidence_required': False}


def test_final_check_records_one_verified_result_and_reuses_same_candidate(tmp_path):
    async def scenario():
        core, monitor, root, plans = setup_task(tmp_path)
        value = requirements()
        value['deliverables'] = []
        plans.capture_requirements(root['id'], value, expected_revision=0)
        plans.register_source(root['id'], 'source', 'artifact', 'hash', [{'id': 'p-0'}], operation='review')
        plans.record_results(root['id'], 'artifact', 'hash', [{'target_id': 'p-0', 'status': 'reviewed'}])
        calls = []
        async def invoke(messages, schema):
            calls.append((messages, schema))
            return json.dumps(answer())
        service = verifier(core)
        for _ in range(2):
            result = await service.verify(root['id'], {'candidate': '完成报告', 'outputs': []}, invoke,
                                           fence=(root['revision'], root['attempt']), input_tokens=6000)
            assert result['status'] == 'satisfied'
        assert len(calls) == 1
        assert '审阅附件全部非空内容' in calls[0][0][-1]['content']
        assert service.latest(root['id'])['status'] == 'satisfied'
        monitor.finish_task(root['id'], 'completed')
        assert service.latest(root['id'])['status'] == 'satisfied'
    asyncio.run(scenario())


def test_checker_cannot_override_missing_application_coverage(tmp_path):
    async def scenario():
        core, _, root, plans = setup_task(tmp_path)
        calls = []
        async def invoke(messages, schema):
            calls.append(messages)
            return json.dumps(answer(goals=requirements()))
        result = await verifier(core).verify(root['id'], {'candidate': '完成'}, invoke,
            fence=(root['revision'], root['attempt']), input_tokens=6000)
        assert result['status'] == 'needs_work'
        assert not plans.completion(root['id'])['ready']
        assert len(calls) == 1
    asyncio.run(scenario())


def test_satisfied_checker_cannot_add_a_new_file_obligation(tmp_path):
    async def scenario():
        core, _, root, plans = setup_task(tmp_path)
        original = requirements()
        original['source_scopes'] = [{**scope(), 'scope':'metadata', 'target_ids':[]}]
        original['deliverables'] = []
        plans.capture_requirements(root['id'], original, expected_revision=0)
        invented = {**original, 'deliverables': [{'path': 'unrequested.docx', 'format': 'docx'}]}
        async def invoke(messages, schema):
            return json.dumps(answer(goals=invented))
        result = await verifier(core).verify(root['id'], {'candidate':'已回答，不导出', 'outputs': []}, invoke,
            fence=(root['revision'],root['attempt']), input_tokens=6000)
        assert plans.goals.get(root['id'])['deliverables'] == []
        assert result['status'] == 'satisfied'
    asyncio.run(scenario())


def test_invalid_checker_output_uses_three_actual_calls_then_keeps_failure(tmp_path):
    async def scenario():
        core, _, root, _ = setup_task(tmp_path)
        calls = []
        async def invoke(messages, schema):
            calls.append(messages)
            return '{broken'
        with pytest.raises(CoreDataError, match='核对'):
            await verifier(core).verify(root['id'], {'candidate': '报告'}, invoke,
                fence=(root['revision'], root['attempt']), input_tokens=6000)
        assert len(calls) == 3
    asyncio.run(scenario())


def test_satisfied_checker_ignores_redundant_resolved_selector(tmp_path):
    async def scenario():
        from agent.execution.source_inventory import SourceInventory

        core, _, root, plans = setup_task(tmp_path)
        original = {**requirements(), 'deliverables': [], 'source_scopes': [
            {**scope('read', 'selected'), 'selector': {'kind': 'prefix_nonempty', 'count': 3}}]}
        plans.capture_requirements(root['id'], original, expected_revision=0)
        inventory = SourceInventory(core)
        fields = [{'id': f'p-{index}', 'text': str(index)} for index in range(4)]
        fence = (root['revision'], root['attempt'])
        inventory.register(root['id'], 'source', 'artifact', 'hash', fields, complete=True, fence=fence)
        inventory.record_read(root['id'], 'source', 'hash', fields[:3], fence=fence)
        redundant = {**original, 'source_scopes': [
            {**original['source_scopes'][0], 'target_ids': [field['id'] for field in fields[:3]]}]}
        calls = []

        async def invoke(messages, schema):
            calls.append(messages)
            return json.dumps(answer(goals=redundant))

        result = await verifier(core).verify(root['id'], {'candidate': '已核验三个位置'}, invoke,
            fence=fence, input_tokens=6000)
        assert result['status'] == 'satisfied'
        assert result['goal_requirements'] is None
        assert plans.goals.get(root['id'])['source_scopes'] == original['source_scopes']
        assert len(calls) == 1

    asyncio.run(scenario())


def test_actionable_checker_retries_selector_with_target_ids(tmp_path):
    async def scenario():
        core, _, root, plans = setup_task(tmp_path)
        invalid = {**requirements(), 'source_scopes': [{**scope('read', 'selected'),
            'target_ids': ['p-0'], 'selector': {'kind': 'prefix_nonempty', 'count': 1}}]}
        corrected = {**invalid, 'source_scopes': [{**invalid['source_scopes'][0], 'target_ids': []}]}
        calls = []

        async def invoke(messages, schema):
            calls.append(messages)
            proposal = invalid if len(calls) == 1 else corrected
            return json.dumps(answer('needs_work', goals=proposal, unmet=['尚需读取第一个非空位置']))

        result = await verifier(core).verify(root['id'], {'candidate': '待处理'}, invoke,
            fence=(root['revision'], root['attempt']), input_tokens=6000)
        assert result['status'] == 'needs_work'
        assert plans.goals.get(root['id'])['source_scopes'] == corrected['source_scopes']
        assert len(calls) == 2
        assert 'target_ids=[]' in calls[1][0]['content']

    asyncio.run(scenario())


def test_unknown_checker_status_is_rejected(tmp_path):
    core, _, _, _ = setup_task(tmp_path)
    service = verifier(core)
    with pytest.raises(CoreDataError):
        service.validate(answer('looks_good'))
    with pytest.raises(CoreDataError):
        service.validate(answer('satisfied', unmet=['还有未审阅位置']))


def test_late_checker_cannot_publish_after_cancel(tmp_path):
    async def scenario():
        core, monitor, root, _ = setup_task(tmp_path)
        async def invoke(messages, schema):
            monitor.control(root['id'], 'cancel', revision=root['revision'], idempotency_key='cancel')
            return json.dumps(answer(goals=requirements()))
        with pytest.raises(CoreDataError):
            await verifier(core).verify(root['id'], {'candidate': '报告'}, invoke,
                fence=(root['revision'], root['attempt']), input_tokens=6000)
    asyncio.run(scenario())


def test_original_goal_and_schema_must_fit_checker_budget(tmp_path):
    async def scenario():
        core, _, root, _ = setup_task(tmp_path)
        async def unexpected(messages, schema):
            raise AssertionError('Cannot invoke beyond the context budget')
        with pytest.raises(CoreDataError):
            await verifier(core).verify(root['id'], {'candidate': '报告'}, unexpected,
                fence=(root['revision'], root['attempt']), input_tokens=32)
    asyncio.run(scenario())


def test_generated_file_preview_shrinks_to_budget_without_changing_authenticated_source(tmp_path):
    from copy import deepcopy
    from agent.context_engine.policy import estimate_tokens
    from agent.execution.goal_verification import GOAL_INSTRUCTION, VERIFICATION_SCHEMA
    core, _, _, _ = setup_task(tmp_path)
    payload = {'request': {'content': '核对真实Word正文'}, 'candidate': '已生成',
        'outputs': [{'id': 'artifact', 'sha256': 'hash', 'content': {'items': [
            {'id': f'p-{index}', 'text': '设备试验结论需要依据。' * 200} for index in range(8)],
            'total_positions': 8, 'total_characters': 17600, 'truncated': False}}]}
    original = deepcopy(payload)
    minimal = {**payload, 'outputs': []}
    budget = estimate_tokens({'messages': [{'role': 'system', 'content': GOAL_INSTRUCTION},
        {'role': 'user', 'content': json.dumps(minimal, ensure_ascii=False)}],
        'response_schema': VERIFICATION_SCHEMA}) + 1200
    messages = verifier(core)._messages(payload, budget)
    assert estimate_tokens({'messages': messages, 'response_schema': VERIFICATION_SCHEMA}) + 256 <= budget
    supplied = json.loads(messages[-1]['content'])
    content = supplied['outputs'][0]['content']
    assert content['truncated'] and content['total_positions'] == 8
    assert supplied['request'] == payload['request'] and payload == original
    for item in content['items']:
        source = next(value for value in original['outputs'][0]['content']['items'] if value['id'] == item['id'])
        assert source['text'].startswith(item['text'])


def test_checker_uses_json_object_fallback_within_three_physical_calls(tmp_path):
    from agent.providers import ProviderError
    async def scenario():
        core, _, root, plans = setup_task(tmp_path)
        value = requirements()
        value['source_scopes'] = [scope('read', 'metadata')]
        value['deliverables'] = []
        plans.capture_requirements(root['id'], value, expected_revision=0)
        calls = []
        async def invoke(messages, schema):
            calls.append(schema)
            if len(calls) == 1:
                raise ProviderError('response_format_unsupported', 'schema unsupported')
            return json.dumps(answer())
        result = await verifier(core).verify(root['id'], {'candidate': '报告'}, invoke,
            fence=(root['revision'], root['attempt']), input_tokens=6000)
        assert result['status'] == 'satisfied'
        assert calls[0] and calls[1] is None and len(calls) == 2
    asyncio.run(scenario())
