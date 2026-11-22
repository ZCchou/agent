import pytest

from agent.document_review import append_scope_gaps
from agent.execution.document_review_selection import sha
from agent.storage import CoreDataError


def test_unresolved_position_is_disclosed_separately_from_reviewed_positions():
    result = {'markdown': '审阅正文', 'report': {'targets': [{'target_id': 'p-1'}], 'coverage': {'total': 1}}}
    fields = [{'id': 'p-2', 'chapter_path': ['温度试验'], 'text': 'a) 试验前'}]
    gaps = [{'target_id': 'p-2', 'text_sha256': sha('a) 试验前'), 'reason': '范围仍待确认'}]
    append_scope_gaps(result, fields, gaps)
    assert result['report']['scope_gap_sha256'] == sha(gaps)
    assert result['report']['coverage'] == {'total': 1}
    assert result['report']['targets'] == [{'target_id': 'p-1'}]
    for phrase in ('审阅正文', '范围待确认', '温度试验', 'p-2', '试验前'):
        assert phrase in result['markdown']


@pytest.mark.parametrize('problem', ['reviewed', 'altered', 'duplicate', 'foreign'])
def test_reject_conflicting_scope_gap(problem):
    field = {'id': 'p-2', 'text': '源文', 'chapter_path': []}
    gap = {'target_id': 'p-2', 'text_sha256': sha('源文'), 'reason': '不确定'}
    result = {'markdown': '原报告', 'report': {'targets': [], 'coverage': {'total': 0}}}
    gaps = [gap]
    if problem == 'reviewed':
        result['report']['targets'].append({'target_id': 'p-2'})
    if problem == 'altered':
        field['text'] = '不同源文'
    if problem == 'duplicate':
        gaps.append(dict(gap))
    if problem == 'foreign':
        gap['target_id'] = 'p-3'
    with pytest.raises(CoreDataError):
        append_scope_gaps(result, [field], gaps)
    assert result['markdown'] == '原报告'
