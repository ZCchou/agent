import asyncio
import pytest
from agent.storage import CoreDataError


QUOTE = '减压在15s内完成。保持此降低了的压力至少10min或设备规范中规定的时间。试验剖面见图4-7。'


def validate(summary, quote=QUOTE, original='2h'):
    from agent.document_review import _validate
    async def resolve(text, citations):
        return text, [{'id': 'source', 'title': 'DO-160F', 'revision': 'F',
            'clause_number': '4.6.2', 'quote': quote, 'locator': {'page': 18},
            'support': {'status': 'needs_review', 'uncovered_concepts': []}}]
    result = {'results': [{'target_id': 't-0-r2-c3', 'content_kind': 'content', 'status': 'pending',
        'summary': summary, 'problems': [], 'gaps': [], 'version_issues': [], 'facts': [],
        'evidence': [{'id': 'source', 'quote_id': 'q-selected'}]}]}
    return asyncio.run(_validate(result, [{'id': 't-0-r2-c3','text': original,'field_type': 'content'}],
        {'manifest': {'referenced_standards': []}}, resolve))


@pytest.mark.parametrize('summary,quote,original', [
    ('减压试验参数为2h，依据DO-160F 4.6.2及图4-7剖面，压力降低后保持时间至少2小时。', QUOTE, '2h'),
    ('条款规定至少10min，因此必须保持2h。', QUOTE, '2h'),
    ('原格75.26/11.60与所选摘录完全一致。', '50000 15240 11.60 -56.5', '75.26/11.60'),
])
def test_selected_quotes_must_contain_values_attributed_to_external_requirements(summary, quote, original):
    with pytest.raises(CoreDataError) as caught:
        validate(summary, quote, original)
    assert caught.value.code in {'rag_citation_invalid', 'document_review_invalid'}
    assert 't-0-r2-c3' in caught.value.message
    assert '摘录' in caught.value.message


@pytest.mark.parametrize('summary,quote,original', [
    ('原格写2h，本次所选摘录未证明此固定时长。', QUOTE, '2h'),
    ('模板采用2h；所引条款规定至少10min，二者阶段和依据仍需核对。', QUOTE, '2h'),
    ('依据所选条款，原文2h缺乏直接支持。', QUOTE, '2h'),
    ('条款规定120min，即2h。', '本阶段规定120min。', '2h'),
    ('条款规定0.25min，即15s。', '减压时间最大15s。', '<15s'),
    ('标准规定1000Pa，即1kPa。', '压力为1kPa。', '1kPa'),
    ('原文2h；由条款规定的10min计算得到约0.17h，阶段仍需核对。', QUOTE, '2h'),
])
def test_original_observations_uncertainty_and_exact_conversions_remain_allowed(summary, quote, original):
    result = validate(summary, quote, original)
    assert result[0]['summary'] == summary
