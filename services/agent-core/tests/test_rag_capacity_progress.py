from agent.rag.capacity_progress import capacity_feedback, note_evidence_progress, paused_tools
import asyncio
from unittest.mock import Mock, AsyncMock
import pytest
from agent.runtime import ChatRunService
from agent.storage import CoreDataError
from agent.tool_contracts import ToolCall


def test_tool_can_change_page_or_query_once_before_closing():
    run = {}
    details = {'evidence_capacity_exhausted': True, 'admitted_citations': [{'id': 'E1', 'quote_ids': ['Q1']}]}
    message = capacity_feedback(run, 'rag.search', details)
    assert 'E1' in message and not paused_tools(run)
    capacity_feedback(run, 'rag.search', details)
    assert paused_tools(run) == {'rag.search'}
    assert 'rag.read' not in paused_tools(run)
    note_evidence_progress(run)
    assert not paused_tools(run)


def test_other_errors_do_not_suspend_tools_and_read_has_separate_budget():
    run = {}
    assert capacity_feedback(run, 'rag.read', {}) is None
    details = {'evidence_capacity_exhausted': True}
    for name in ('rag.read', 'rag.search', 'rag.read', 'rag.search'):
        capacity_feedback(run, name, details)
    assert paused_tools(run) == {'rag.read', 'rag.search'}


def test_runtime_rag_tool_reaches_search_and_preserves_citation_on_capacity(tmp_path):
    service = Mock()
    run = {'id': 'current'}
    service.user_input_sources._manifest.return_value = tmp_path / 'no-input-file'
    service._prepare_requested_project_evidence = AsyncMock(return_value=True)
    state = {'used_tokens': 15}
    service.rag_grounding.runs = {'current': state}
    call = ToolCall('first', 'rag.search', {'query': '试验异常处理'})
    capacity = CoreDataError('rag_context_too_small', '无法容纳',
        {'evidence_capacity_exhausted': True, 'admitted_citations': [{'id': 'e-1', 'quote_ids': ['q-1']}]})
    service.rag_grounding.execute = AsyncMock(side_effect=capacity)
    for _ in range(2):
        with pytest.raises(CoreDataError, match='e-1'):
            asyncio.run(ChatRunService._rag_tool(service, run, call, 1600))
    assert paused_tools(run) == {'rag.search'}
    async def successful(run_id, name, arguments, limit):
        assert (run_id, name, limit) == ('current', 'rag.read', 1600)
        state['used_tokens'] += 20
        return '{"evidence":"recovered"}'
    service.rag_grounding.execute = successful
    result = asyncio.run(ChatRunService._rag_tool(service, run,
        ToolCall('second','rag.read', {'evidence_id': 'e-1'}), 1600))
    assert 'recovered' in result and not paused_tools(run)



def test_capacity_synthesis_requires_two_real_failures_then_two_unchanged_control_rounds_and_only_once():
 from agent.rag.capacity_progress import CapacitySynthesisGuard
 g=CapacitySynthesisGuard()
 for _ in range(2):assert not g.observe('A',['rag.search'],failures=1,allowed=True,has_evidence=True)
 assert not g.observe('A',['tools.discover'],allowed=True,has_evidence=True)
 assert g.observe('A',['task.update_plan'],allowed=True,has_evidence=True)
 assert not g.observe('A',['tools.discover'],allowed=True,has_evidence=True)


@pytest.mark.parametrize('change',['generation','admitted_quote','actual_work'])
def test_new_evidence_or_generation_or_actual_work_requires_new_failure_sequence(change):
 from agent.rag.capacity_progress import CapacitySynthesisGuard
 g=CapacitySynthesisGuard()
 for _ in range(2):g.observe('A',['rag.search'],failures=1,allowed=True,has_evidence=True)
 assert not g.observe('A',['tools.discover'],allowed=True,has_evidence=True)
 assert not g.observe(change,['task.update_plan'],allowed=True,has_evidence=True)
 assert not g.observe(change,['tools.discover'],allowed=True,has_evidence=True)


@pytest.mark.parametrize('content,deliverables,allowed',[
 ('解释上文的设备试验要求',[],True),('解释要求，不要导出或修改文件',[],True),
 ('解释要求并保存为报告.md',[],False),('解释要求并上传报告',[],False),
 ('使用技能并解释要求',[],False),('解释要求',[{'path':'result.md'}],False)])
def test_synthesis_never_drops_external_actions_or_deliverables(content,deliverables,allowed):
 from agent.rag.capacity_progress import text_synthesis_allowed
 assert text_synthesis_allowed({'content':content},{'task_type':'evidence_answer'},
  {'deliverables':deliverables}) is allowed


def test_empty_or_unavailable_evidence_and_unrelated_errors_never_arm_synthesis():
 from agent.rag.capacity_progress import CapacitySynthesisGuard,retrieval_failure
 g=CapacitySynthesisGuard()
 for name in ['rag.search','rag.search','tools.discover','task.update_plan']:
  assert not g.observe('A',[name],failures=int(name=='rag.search'),allowed=True,has_evidence=False)
 assert not retrieval_failure('fs.read',False,'rag_context_too_small','{}')
 assert not retrieval_failure('rag.search',False,'permission_denied','{}')
 assert retrieval_failure('rag.search',True,None,'{"evidence":[],"gap":"No matching evidence"}')
