import asyncio
import json
from types import SimpleNamespace

import pytest

from agent.context_engine.policy import estimate_tokens
from agent.execution.compiler import InvocationCompiler
from agent.execution.document_goal import example_goal, normalize_goal
from agent.execution.document_goal_runtime import build_final_messages, final_messages, resolve_final_extraction
from agent.execution.working_context import compact_tool_history
from agent.storage import CoreDataError
from test_unified_invocation_v3 import model_spec
from test_working_context import pair


def source(count, text='verified original text'):
    return {'source_id': 'attachment', 'template_id': 'template', 'total': count,
            'fields': [{'id': f'p-{i}', 'text': f'{text} {i}'} for i in range(count)]}


async def measure(messages):
    try:
        prepared = InvocationCompiler().compile_model(model_spec(max_output_tokens=2048), messages, context_window=8192)
        return prepared.input_budget['request_tokens']
    except CoreDataError as error:
        return error.details['request_tokens']


def test_thousands_of_positions_use_exact_selection_reference_without_omission():
    packet = {'sources': [source(5000)], 'success_criteria': ['提取全文原文，不改写。']}
    messages, prompt = asyncio.run(build_final_messages(packet, '提取全文原文，不改写。', 'extract',
        input_limit=4915, measure=measure))
    selected = prompt['sources'][0]
    assert selected['selected_count'] == 5000
    assert 'fields' not in selected
    assert 'verified original text' not in json.dumps(messages)
    answer = {'template_id': 'template', 'total': 5000, 'selection_ref': selected['selection_ref']}
    resolved = json.loads(resolve_final_extraction(json.dumps(answer), packet))
    assert resolved['fields'] == packet['sources'][0]['fields']
    assert asyncio.run(measure(messages)) <= 4915
    with pytest.raises(CoreDataError):
        resolve_final_extraction(json.dumps({**answer, 'selection_ref': 'forged'}), packet)


def test_full_read_after_five_compactions_jointly_budgets_material_and_evidence():
    request = '审阅全文并保留原始设备参数。'
    history = [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': request}]
    compactions = 0
    for index in range(8):
        history.extend(pair(index, json.dumps({'result_id': str(index), 'preview': '原始正文' * 3000})))
        compactions += compact_tool_history(history, input_budget=3000, measure=estimate_tokens)
    assert compactions >= 5
    packet = {'sources': [source(1000, '设备参数依据中文正文' * 100)],
              'success_criteria': ['全文结果覆盖，不假称省略的正文已经总结。'],
              'coverage': {'ready': True, 'completed': 1000, 'remaining': 0}}
    evidence = [{'id': f'e-{i}', 'quote_options': [{'quote_id': 'q', 'text': '实际条款摘录' * 100}]}
                for i in range(12)]
    messages, prompt = asyncio.run(build_final_messages(packet, request, 'review', input_limit=4915,
        measure=measure, evidence=evidence, evidence_instruction='Use actual evidence for every professional claim.'))
    assert asyncio.run(measure(messages)) <= 4915
    assert prompt['sources'][0]['selected_count'] == 1000
    assert prompt['sources'][0]['material_truncated'] is True
    assert prompt['sources'][0]['omitted_count'] > 0
    assert prompt['coverage'] == packet['coverage']
    assert request in messages[1]['content']
    evidence_message = json.loads(messages[-1]['content'])
    assert evidence_message['evidence']
    for item in evidence_message['evidence']:
        assert item in evidence
    assert evidence_message['omitted_count'] == len(evidence) - len(evidence_message['evidence'])


def test_impossible_fixed_requirements_fail_instead_of_truncating_them():
    packet = {'sources': [source(1)], 'success_criteria': ['不可删除的要求' * 5000]}
    with pytest.raises(CoreDataError) as caught:
        asyncio.run(build_final_messages(packet, 'original', 'synthesis', input_limit=4915, measure=measure))
    assert caught.value.code == 'task_final_context_too_small'
    assert packet['success_criteria'][0] == '不可删除的要求' * 5000


def test_extract_goal_cannot_hide_a_review_operation():
    goal = example_goal('attachment')
    goal['answer_kind'] = 'extract'
    goal['source_scopes'][0]['operation'] = 'review'
    with pytest.raises(CoreDataError):
        normalize_goal(goal, [{'id': 'attachment'}])


def test_final_material_counts_the_compiled_response_schema():
    spec = model_spec(max_output_tokens=2048, response_format='json_schema',
        response_schema_json=json.dumps({'enum': [f'allowed-quote-{i}' for i in range(250)]}))
    service = SimpleNamespace(invocation_compiler=InvocationCompiler(),
        _create_model_invocation_spec=lambda *args, **kwargs: spec,
        rag_grounding=SimpleNamespace(runs={}))
    packet = {'sources': [source(200, '正文' * 200)], 'success_criteria': ['保留完成情况']}
    messages, prompt = asyncio.run(final_messages(service, {'id': 'run'}, object(), {}, {}, 1,
        packet, {'answer_kind': 'synthesis'}, {'content': '总结全文读取结果'}))
    result = InvocationCompiler().compile_model(spec, messages, context_window=8192)
    assert result.input_budget['request_tokens'] <= result.input_budget['input_hard_limit'] - 128
    assert prompt['sources'][0]['material_truncated'] is True


def test_read_only_extract_does_not_receive_conflicting_claims_instruction():
    packet = {'sources': [source(2)], 'success_criteria': ['只提取']}
    messages, _ = asyncio.run(build_final_messages(packet, '只提取', 'extract', input_limit=4915,
        measure=measure, evidence=[{'id': 'evidence-id'}], evidence_instruction='Return claims with quote IDs.'))
    assert 'Return claims' not in json.dumps(messages)
    assert 'evidence-id' not in json.dumps(messages)
