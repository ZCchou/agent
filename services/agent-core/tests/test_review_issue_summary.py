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
