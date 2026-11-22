import json

import pytest
from docx import Document

from agent.document_consistency import review_facts
from agent.document_contracts import document_batch_schema
from agent.storage import CoreDataError
from test_document_review import setup, register, execute


@pytest.mark.parametrize('change,reason', [
    ({'name': '  '}, 'name 不能为空'),
    ({'value': '  '}, 'value 不能为空'),
    ({'value': {'PRIVATE_NOT_TO_SERIALIZE': 'secret'}}, 'value 必须为文字'),
    ({'unit': 'h' * 257}, 'unit 超过256字符'),
    ({'extra': 'PRIVATE_NOT_TO_SERIALIZE'}, '字段结构'),
    ({'value': '2小时'}, 'value 未逐字匹配'),
])
def test_fact_failure_distinguishes_invalid_contract_without_dumping_objects(change, reason):
    fact = {'name': '时间', 'value': '2h', 'unit': 'h', 'condition': '', **change}
    with pytest.raises(CoreDataError) as failure:
        review_facts([fact], '2h')
    assert failure.value.code == 'document_review_invalid'
    assert reason in failure.value.message
    assert 'PRIVATE_NOT_TO_SERIALIZE' not in failure.value.message


def test_fact_mismatch_feedback_bounds_model_value_and_never_dumps_long_source():
    source = 'LONG_SOURCE_MUST_NOT_BE_DUMPED ' * 1000
    with pytest.raises(CoreDataError) as failure:
        review_facts([{'name': '时间', 'value': 'Z' * 200, 'unit': '', 'condition': ''}], source)
    assert 'value 未逐字匹配' in failure.value.message
    assert 'Z' * 24 in failure.value.message and 'Z' * 25 not in failure.value.message
    assert 'LONG_SOURCE_MUST_NOT_BE_DUMPED' not in failure.value.message
    assert len(failure.value.message) < 180


def test_fact_diagnostics_keep_empty_list_and_existing_literal_representations_valid():
    assert review_facts([], '2h') == []
    for value in ('2', '2h'):
        result = review_facts([{'name': '时间', 'value': value, 'unit': 'h', 'condition': ''}], '2h')
        assert result[0]['value'] == value


@pytest.mark.parametrize('name', ['name', 'value'])
def test_fact_decoding_contract_requires_nonempty_name_and_value(name):
    schema = document_batch_schema({'stage': 'review', 'fields': [{'id': 'p-0', 'field_type': 'content'}],
                                    'evidence': [], 'include_facts': True}, [])
    reviewed = schema['properties']['response']['anyOf'][0]['properties']['results']['items']
    properties = reviewed['properties']['facts']['items']['properties']
    assert properties[name].get('minLength', 0) >= 1
    assert properties['unit'].get('minLength', 0) == properties['condition'].get('minLength', 0) == 0


def test_short_source_mismatch_is_corrected_from_bounded_persisted_feedback(setup):
    workflow, run, monitor, root, sources = setup
    document = Document()
    document.add_paragraph('2h')
    artifact = register(workflow, run, document)
    calls = []
    def generate(batch, context):
        message = batch.get('format_error_message', '')
        calls.append(message)
        corrected = '"2小时"' in message and '"2h"' in message
        response = sources.response(batch)
        response['results'][0]['facts'] = [{'name': '时间', 'value': '2h' if corrected else '2小时',
                                           'unit': 'h', 'condition': ''}]
        return response
    result = execute(setup, artifact, generate)
    assert len(calls) == 2
    assert result['report']['targets'][0]['facts'][0]['value'] == '2h'
    with workflow.repository._connect() as connection:
        records = [json.loads(row['payload_json']) for row in connection.execute(
            "SELECT payload_json FROM execution_wait_activity WHERE task_id=? "
            "AND json_extract(payload_json,'$.review_validation_failure') IS NOT NULL", (root['id'],))]
    failure = records[0]['review_validation_failure']
    assert len(failure['message']) <= 160
    assert '"2小时"' in failure['message'] and '"2h"' in failure['message']


def test_valid_fact_spacing_and_case_preserve_original_representation():
    fact = {'name': ' 时间 ', 'value': ' 2 H ', 'unit': ' h ', 'condition': ''}
    assert review_facts([fact], '2h') == [fact]
    assert fact['value'] == ' 2 H '


def test_invalid_surrogate_value_remains_serializable_feedback_data():
    with pytest.raises(CoreDataError) as failure:
        review_facts([{'name': '时间', 'value': chr(0xD800) + 'x', 'unit': '', 'condition': ''}], '2h')
    assert 'value 未逐字匹配' in failure.value.message
    failure.value.message.encode('utf-8')


def test_escaped_fact_data_cannot_push_target_identity_out_of_persisted_feedback(setup):
    workflow, run, monitor, root, sources = setup
    document = Document()
    document.add_paragraph(chr(34) * 48)
    artifact = register(workflow, run, document)
    calls = []
    def generate(batch, context):
        message = batch.get('format_error_message', '')
        calls.append(message)
        corrected = 'p-0' in message and 'value 未逐字匹配' in message
        response = sources.response(batch)
        response['results'][0]['facts'] = ([] if corrected else
            [{'name': '参数', 'value': chr(92) * 24, 'unit': '', 'condition': ''}])
        return response
    result = execute(setup, artifact, generate)
    assert len(calls) == 2
    assert 'p-0' in calls[1] and 'value 未逐字匹配' in calls[1]
    assert len(calls[1]) <= 160
    assert not result['report']['targets'][0].get('error_code')
