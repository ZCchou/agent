import ast,asyncio,json,os,sys
from pathlib import Path
from types import MethodType
import pytest
from agent import runtime
from agent.storage import CoreDataError
from test_tool_continuation_retry import fixture,install_http,start
from test_template_tool_repair import native_call
ns={name:getattr(runtime.ChatRunService,name) for name in ['_generate_tool_turns','_generate_with_tools']}

@pytest.mark.parametrize('code',['document_selection_invalid','document_selection_context_too_small'])
def test_selection_protocol_failure_is_checkpointed_and_never_becomes_read_loop_or_success(tmp_path,monkeypatch,code):
 requests=install_http(monkeypatch,[
  (native_call('tools_discover',{'names':['document.review_targets']},'discover'),'tool_calls'),
  (native_call('document_review_targets',{'template_id':'synthetic-template','format':'markdown'},'review'),'tool_calls'),
  ({'content':'Incorrectly resumed as a successful final answer.'},'stop')],goal_checks=True)
 async def exercise():
  async def sink(_):pass
  core,_,work,service=await fixture(tmp_path,sink)
  service._generate_tool_turns=MethodType(ns['_generate_tool_turns'],service)
  service._generate_with_tools=MethodType(ns['_generate_with_tools'],service)
  async def reject(*_):raise CoreDataError(code,'Controlled selection failure')
  service._review_targets_tool=reject
  try:
   identity=await start(service,work);result=core.get_model_run(identity)
   assert result['status']=='failed' and result['error_code']==code
   operations=service.list_tool_operations(identity)
   assert len(operations)==2 and operations[-1]['tool_name']=='document.review_targets' and operations[-1]['status']=='failed'
   assert len(requests)==2 and not any(o['tool_name']=='tools.read_result' for o in operations)
   with core._connect() as db:
    saved=json.loads(db.execute('SELECT context_json FROM run_working_contexts WHERE run_id=?',(identity,)).fetchone()[0])
    assert db.execute('SELECT count(*) FROM tool_result_objects WHERE run_id=? AND success=0',(identity,)).fetchone()[0]==1
   assert saved['phase']=='main'
   assert any(any(c['id']=='review' for c in m.get('tool_calls',[])) for m in saved['messages'] if m['role']=='assistant')
   assert any(m.get('tool_call_id')=='review' for m in saved['messages'] if m['role']=='tool')
  finally:await service.shutdown()
 asyncio.run(exercise())
