from agent.review_issue_summary import issue_rows, issue_table
from agent.document_review import _public_review_layout


def test_summary_uses_source_fields_escapes_cells_and_keeps_evidence_per_item():
    fields = [{'id': 'p-1', 'text': 'A|B\n[C](file:bad)'}, {'id': 'p-2','text':'second'}]
    records = [{'target_id':'p-1','title':'第1段','verdict_status':'submitted','verdict':'待补充',
                'finding':'缺少记录','recommendation':'补齐记录','source_numbers':[7,7]},
               {'target_id':'p-2','title':'第2段','verdict_status':'submitted','verdict':'符合','source_numbers':[2]}]
    rows=issue_rows([{'review_items':records}],fields)
    assert len(rows)==1 and rows[0]['source_excerpt']==fields[0]['text']
    text=issue_table(rows)
    assert 'A\\|B' in text and '[7](#审阅出处-7)' in text and '#审阅出处-2' not in text
    assert '补齐记录' in text and '[C](file:bad)' not in text


def test_summary_merges_retries_and_reports_conflicts_and_empty_results():
    fields=[{'id':'p-1','text':'text'}]
    def run(status,verdict):
        return {'review_items':[{'target_id':'p-1','verdict_status':status,'verdict':verdict}]}
    assert not issue_rows([run('partial',None),run('submitted','符合')],fields)
    rows=issue_rows([run('submitted','符合'),run('submitted','不符合')],fields)
    assert len(rows)==1 and rows[0]['verdict']=='无法判断' and rows[0]['verdict_status']=='conflict'
    assert '没有列入' in issue_table([]) and issue_rows([],fields)[0]['verdict_status']=='unmapped'


def test_request_for_final_issue_table_keeps_individual_review_layout():
    assert _public_review_layout('逐项审阅。最后用表格汇总问题项、原文、建议和证据。')=='default'
    assert _public_review_layout('用表格输出审阅结果')=='table'


def test_groups_findings_by_decision_and_keeps_source_positions_and_evidence():
    verdicts = ['无法判断', '待补充', '不符合', '无法判断']
    fields = [{'id': f'p-{i}', 'text': f'段落原文{i}'} for i in range(4)]
    section = {
        'review_items': [
            {'target_id': field['id'], 'title': f'位置{i}', 'verdict_status': 'submitted',
             'verdict': verdict, 'finding': f'理由{i}', 'recommendation': f'建议{i}', 'source_numbers': [i+1]}
            for i, (field, verdict) in enumerate(zip(fields, verdicts))
        ],
        'verified_source_quotes': [{'title': '测试规范', 'page': i+7} for i in range(4)],
    }
    text = issue_table(issue_rows([section], fields))
    assert '已确认问题 1 项 · 待补充材料 1 项 · 无法判断 2 项' in text
    groups = text.split('### ')[1:]
    assert [group.splitlines()[0] for group in groups] == ['已确认问题', '待补充材料', '无法判断']
    for group, indices in zip(groups, [[2], [1], [0, 3]]):
        for i in indices:
            assert f'位置{i} · 理由{i} | {verdicts[i]} | 段落原文{i} | 建议{i}' in group
            assert f'测试规范 · PDF 第 {i+7} 页 [{i+1}](#审阅出处-{i+1})' in group
    assert text.count('| 问题项及位置 |') == 3
    assert text.count('](#审阅出处-') == 4
