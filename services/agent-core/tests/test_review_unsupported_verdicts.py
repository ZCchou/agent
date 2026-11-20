import pytest
from docx import Document
from test_document_review import setup, register, execute


@pytest.mark.parametrize('section,claim', [
    ('summary', '按试验前、中、后描述，符合常规逻辑，无违规。'),
    ('summary', '无具体参数或违规项。'),
    ('problems', '该模板没有任何违规风险。'),
    ('gaps', 'The plan has no regulatory violations.'),
    ('version_issues', '本项完全符合全部要求。'),
    ('summary', 'Not verified. Fully compliant.'),
])
def test_unquoted_categorical_verdict_requires_support_and_receives_feedback(setup, section, claim):
    workflow, run, _, _, sources = setup
    document = Document(); document.add_paragraph('本节按试验前、中、后说明具体步骤。')
    artifact = register(workflow, run, document)
    calls = []
    def generate(batch, context):
        calls.append(batch)
        response = sources.response(batch)
        row = response['results'][0]
        row.update(evidence=[], summary='该段是描述步骤顺序的模板指导。', problems=[], gaps=[], version_issues=[])
        if len(calls) == 1:
            row[section] = claim if section == 'summary' else [claim]
        return response
    result = execute(setup, artifact, generate, target_ids=['p-0'])
    assert len(calls) == 2
    assert calls[1]['format_error'] == 'document_review_invalid'
    assert 'p-0' in calls[1]['format_error_message']
    row = result['report']['targets'][0]
    assert row['status'] == 'pending' and row['evidence'] == []


@pytest.mark.parametrize('summary,original', [
    ('本次不能确认无违规，仍需取得适用依据。', '本节描述步骤。'),
    ('本项不能声称无违规。', '本节描述步骤。'),
    ('无违规尚无法确认。', '本节描述步骤。'),
    ('本项并非已确认完全符合要求。', '本节描述步骤。'),
    ('原文列出全部符合性分析项目。', '全部符合性分析项目'),
    ('未发现违规不等于确认合规。', '本节描述步骤。'),
    ('未证实本项完全符合要求。', '本节描述步骤。'),
    ('原文写“无违规”，本次只记录该声明，结论待复核。', '无违规'),
    ('This review cannot confirm no regulatory violations.', '本节描述步骤。'),
    ('保留原文顺序；本次没有关联直接支持，待人工复核。', '本节描述步骤。'),
])
def test_explicit_uncertainty_and_verbatim_source_declarations_remain_reviewable(setup, summary, original):
    workflow, run, _, _, sources = setup
    document = Document(); document.add_paragraph(original)
    artifact = register(workflow, run, document)
    calls = []
    def generate(batch, context):
        calls.append(batch)
        response = sources.response(batch)
        response['results'][0].update(evidence=[], summary=summary, problems=[], gaps=[], version_issues=[])
        return response
    result = execute(setup, artifact, generate, target_ids=['p-0'])
    assert len(calls) == 1 and result['report']['targets'][0]['summary'] == summary


def test_supported_categorical_text_still_uses_the_existing_citation_resolver(setup):
    workflow, run, _, _, sources = setup
    document = Document(); document.add_paragraph('振动试验要求。')
    artifact = register(workflow, run, document)
    def generate(batch, context):
        response = sources.response(batch)
        response['results'][0]['summary'] = '本项完全符合引用要求。'
        return response
    result = execute(setup, artifact, generate, target_ids=['p-0'])
    assert result['report']['targets'][0]['evidence']



@pytest.mark.parametrize('text', [
    '无具体违规或适用性问题。',
    '本字段无技术违规。',
    '未填写参数，但本项没有任何重大技术违规。',
    'No technical violations.',
])
def test_unqualified_verdict_modifiers_are_not_an_evidence_loophole(text):
    from agent.document_review import _unsupported_verdict
    assert _unsupported_verdict(text, '模板原始字段名称')


@pytest.mark.parametrize('text', [
    '无技术依据证明违规。',
    '没有足够证据证明违规。',
    '“无违规风险”这一说法尚未证实。',
])
def test_missing_evidence_and_explicitly_unconfirmed_claims_are_not_categorical(text):
    from agent.document_review import _unsupported_verdict
    assert not _unsupported_verdict(text, '模板原始字段名称')
