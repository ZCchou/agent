import pytest
from agent.execution.document_goal import normalize_goal
from agent.execution.document_goal_runtime import plan_named_evidence_review
from agent.execution.prepared_review_publication import requested_review_path


def plan(request):
    source={'id':'source','name':'template.docx'}
    current={'content':request, 'attachments':[source]}
    draft=plan_named_evidence_review({'rag_decision':{'requested_mode':'required'}},current,[source])
    return normalize_goal(draft,[source]) if draft else None


def test_named_file_and_explicit_range_reach_prepared_publication_contract():
    request='结合项目资料库，只审阅附件中过压试验目的的适用条件（p-484），以及减压试验参数表这一行（t-30-r2-c2 到 t-30-r2-c6）。用中文把结果保存为“压力试验专项审阅结果.md”。'
    goal=plan(request)
    assert goal['source_scopes'][0]['target_ids']==['p-484',*(f't-30-r2-c{i}' for i in range(2,7))]
    assert requested_review_path({'content':request},goal)=='压力试验专项审阅结果.md'


@pytest.mark.parametrize('scope,path',[
    ('t-1-r1-c1 到 t-2-r2-c1','安全.md'),('p-1 到 p-120','安全.md'),
    ('t-1-r1-c1 到 c6','安全.md'),('p-3','../越界.md')])
def test_unresolved_range_or_path_is_not_dispatched(scope,path):
    assert plan(f'结合项目资料库，只审阅附件 {scope}，保存为“{path}”。') is None
