import asyncio
import copy
import hashlib
import json

import pytest

from agent.document_plan import validate_source_locators
from agent.rag.grounding import RagGrounding, evidence_with_quotes, quote_options
from agent.storage import CoreDataError
from test_rag_grounding import EvidenceService, evidence, prepare


TEXT = ('8－3\r\n注：1) 工况甲记录保持3h。\r\n 2) 保留全部条件。\r\n图 2－1 工况甲\r\n'
        '注：1) 工况乙转换最大10s。\r\n 2) 随后检测至少20min。\r\n图 2－2 工况乙')


def source(text=TEXT, **locator):
    return {**evidence(), 'title': 'DO-160F', 'revision': 'F', 'clause_number': None, 'text': text,
            'locator': {'kind': 'pdf', 'page': 23, 'block_id': 'block', 'fragment_count': 1,
                        'illustration_labels': ['图2-1', '图2-2'], **locator}}


def ground(record=None):
    record = source() if record is None else record
    class Service(EvidenceService):
        async def search(self, *args, **kwargs):
            return {'evidence': [record], 'generation': 4}
    result = RagGrounding(Service())
    prepare(result)
    return result


def test_quote_options_bind_complete_preceding_notes_to_each_own_caption():
    options = quote_options(TEXT)
    assert len(options) == 2
    assert ''.join(option['text'] for option in options) == TEXT
    assert [option.get('illustration_labels') for option in options] == [['图2-1'], ['图2-2']]
    assert '3h' in options[0]['text'] and '10s' not in options[0]['text']
    assert '10s' in options[1]['text'] and '3h' not in options[1]['text']
    assert options == quote_options(TEXT)


def test_long_note_groups_remain_exact_bounded_and_owner_scoped():
    text = '注：1) ' + '保留试验条件。' * 100 + '\n图2-1 工况甲\n注：1) 条件乙。\n图2-2 工况乙'
    options = quote_options(text)
    assert ''.join(option['text'] for option in options) == text
    assert all(len(option['text']) <= 400 for option in options)
    assert all(option.get('illustration_labels') == ['图2-1'] for option in options[:-1])
    assert options[-1].get('illustration_labels') == ['图2-2']


@pytest.mark.parametrize('text', [
    '图2-1 工况甲\n注：1) 甲。\n图2-2 工况乙\n注：1) 乙。',
    TEXT.replace('注：1) 工况甲', '注：3) 工况甲'),
    TEXT.replace('注：1) 工况甲', '4.6.2 正文条款\n注：1) 工况甲'),
    TEXT + '\n4.6.3 后续正文',
])
def test_unproven_caption_order_keeps_original_slices_and_marks_ambiguity(text):
    before = source(text)
    result = evidence_with_quotes(before)
    assert result['quote_options'][0]['text'] == text
    assert not result['quote_options'][0].get('illustration_labels')
    assert any('图注范围未核实' in str(warning) for warning in result.get('warnings', []))
    assert before.get('warnings') is None


@pytest.mark.parametrize('locator', [{'fragment_count': 2}, {'continues_before': True}])
def test_fragment_scope_is_not_guessed_and_admission_matches_model_options(locator):
    g = ground(source(**locator))
    visible = g.evidence('run-1')[0]
    assert len(visible['quote_options']) == 1
    assert not visible['quote_options'][0].get('illustration_labels')
    g.admit('run-1', ['evidence-1'])
    assert set(g.runs['run-1']['quote_options']['evidence-1']) == {q['quote_id'] for q in visible['quote_options']}


@pytest.mark.parametrize('text', ['完整普通正文。', '注：1) 完整条件。\n图2-1 单一图题'])
def test_single_and_plain_quote_identifiers_remain_unchanged(text):
    option = quote_options(text)[0]
    assert option['text'] == text
    assert option['quote_id'] == 'q-' + hashlib.sha256(('0:' + text).encode()).hexdigest()[:16]


def test_selected_quote_narrows_metadata_without_mutating_full_page_and_rejects_wrong_figure():
    g = ground()
    original = copy.deepcopy(g.runs['run-1']['evidence']['evidence-1'])
    first = g.evidence('run-1')[0]['quote_options'][0]
    g.admit('run-1', ['evidence-1'])
    _, metadata = g.document_claim('run-1', 'DO-160F图2-1记录3h。', [{'id': 'evidence-1', 'quote_id': first['quote_id']}])
    assert metadata[0]['locator']['illustration_labels'] == ['图2-1']
    assert metadata[0]['locator']['page'] == 23
    assert g.runs['run-1']['evidence']['evidence-1'] == original
    with pytest.raises(CoreDataError) as failure:
        g.document_claim('run-1', 'DO-160F图2-2记录3h。', [{'id': 'evidence-1', 'quote_id': first['quote_id']}])
    assert failure.value.code == 'rag_citation_invalid'
    assert '图' in failure.value.message


def test_current_admission_does_not_accept_hidden_legacy_cross_figure_option():
    g = ground()
    legacy = 'q-' + hashlib.sha256(('0:' + TEXT).encode()).hexdigest()[:16]
    g.admit('run-1', ['evidence-1'])
    with pytest.raises(CoreDataError) as failure:
        g.finalize('run-1', json.dumps({'claims': [{'text': '说明', 'evidence': [{'id': 'evidence-1', 'quote_id': legacy}]}]}))
    assert failure.value.code == 'rag_quote_invalid'
    assert g.runs['run-1']['evidence']['evidence-1']['text'] == TEXT


def test_explicit_local_template_figure_number_is_not_an_external_locator():
    validate_source_locators('本模板图9-9需要以后填写。', [
        {'quote': '图2-1 工况甲', 'locator': {'illustration_labels': ['图2-1']}}])


def test_template_reference_to_external_figure_still_requires_selected_quote_scope():
    with pytest.raises(CoreDataError):
        validate_source_locators('模板引用DO-160F图2-2。', [
            {'title': 'DO-160F', 'quote': '注：1) 甲。图2-1 工况甲',
             'locator': {'illustration_labels': ['图2-1', '图2-2']}}])


def test_comparison_may_select_both_separately_scoped_figures():
    g = ground()
    options = g.evidence('run-1')[0]['quote_options']
    g.admit('run-1', ['evidence-1'])
    _, metadata = g.document_claim('run-1', 'DO-160F图2-1记录3h，图2-2记录10s。',
        [{'id': 'evidence-1', 'quote_id': option['quote_id']} for option in options])
    assert [item['locator']['illustration_labels'] for item in metadata] == [['图2-1'], ['图2-2']]


def test_scoped_cross_reference_cannot_grant_the_other_figure_owner():
    g = ground(source(TEXT.replace('工况甲记录保持3h', '工况甲记录保持3h，另见图2-2')))
    first = g.evidence('run-1')[0]['quote_options'][0]
    g.admit('run-1', ['evidence-1'])
    with pytest.raises(CoreDataError) as failure:
        g.document_claim('run-1', 'DO-160F图2-2规定保持3h。', [{'id': 'evidence-1', 'quote_id': first['quote_id']}])
    assert failure.value.code == 'rag_citation_invalid'


def test_repeated_long_quote_slice_uses_selected_id_for_owner():
    common = '注：1) ' + '相同条件' * 120
    g = ground(source(common + '\n图2-1 工况甲\n' + common + '\n图2-2 工况乙'))
    options = g.evidence('run-1')[0]['quote_options']
    first = options[0]
    second = next(option for option in options if option['text'] == first['text']
                  and option.get('illustration_labels') == ['图2-2'])
    assert first['quote_id'] != second['quote_id']
    g.admit('run-1', ['evidence-1'])
    _, metadata = g.document_claim('run-1', 'DO-160F图2-2的条件。',
        [{'id': 'evidence-1', 'quote_id': second['quote_id']}])
    assert metadata[0]['locator']['illustration_labels'] == ['图2-2']


def test_read_parent_expansion_does_not_infer_a_cross_block_note_scope():
    first = '注：1) 甲保持3h。\n图2-1 工况甲\n'
    parent = '注：1) 乙最大10s。\n图2-2 工况乙'
    g = ground(source(first, illustration_labels=['图2-1']))
    g.admit('run-1', ['evidence-1'])
    g.service.read_evidence = lambda *args, **kwargs: {
        'evidence': {'id': 'evidence-1', 'content': first},
        'parent_context': {'text': parent, 'text_hash': hashlib.sha256(parent.encode()).hexdigest()}}
    result = json.loads(asyncio.run(g.execute('run-1', 'rag.read', {'evidence_id': 'evidence-1'}, 20000)))
    assert len(result['quote_options']) == 1
    assert not result['quote_options'][0].get('illustration_labels')


def test_read_same_slice_at_different_offsets_has_distinct_admitted_identity():
    g = ground(source('相同原文相同原文', illustration_labels=[]))
    g.admit('run-1', ['evidence-1'])
    g.service.read_evidence = lambda *args, **kwargs: {'evidence': {'id': 'evidence-1', 'content': '相同原文'}}
    first = json.loads(asyncio.run(g.execute('run-1', 'rag.read', {'evidence_id': 'evidence-1', 'offset': 0}, 20000)))
    second = json.loads(asyncio.run(g.execute('run-1', 'rag.read', {'evidence_id': 'evidence-1', 'offset': 4}, 20000)))
    assert first['quote_options'][0]['quote_id'] != second['quote_options'][0]['quote_id']
    assert all(option['quote_id'] in g.runs['run-1']['quote_options']['evidence-1']
               for result in (first, second) for option in result['quote_options'])


def test_runtime_reauthorization_uses_the_same_fragment_options_as_model_view():
    from types import SimpleNamespace
    from agent.runtime import ChatRunService
    record = source(fragment_count=2)
    g = ground(record)
    visible = g.evidence('run-1')[0]['quote_options']
    runtime = ChatRunService.__new__(ChatRunService)
    runtime.rag_grounding = g
    runtime.rag = SimpleNamespace(reauthorize_evidence=lambda *args, **kwargs: {
        'generation': 4, 'aliases': {'old-evidence': 'evidence-1'}, 'evidence': [record]})
    runtime._reauthorize_document_field('run-1', {'evidence': [
        {'id': 'old-evidence', 'quote_id': visible[0]['quote_id']}]})
    assert set(g.runs['run-1']['quote_options']['evidence-1']) == {option['quote_id'] for option in visible}


def test_multifigure_fragment_without_caption_marks_unresolved_scope():
    record = source('局部注释仅记载保持3h。', fragment_count=2)
    visible = evidence_with_quotes(record)
    assert not visible['quote_options'][0].get('illustration_labels')
    assert any('图注范围未核实' in str(value) for value in visible.get('warnings', []))
    assert record.get('warnings') is None
