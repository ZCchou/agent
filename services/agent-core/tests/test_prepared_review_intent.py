import asyncio,copy,json
import pytest
from test_prepared_review_dispatch import prepared_fixture,REQUEST
from agent.execution.activity import associate
from agent.execution.source_inventory import SourceInventory
from agent.execution import document_goal_runtime as c

def test_conditional_review_planning_is_determined_by_request_not_model_label():
 a={'id':'source','name':'any.docx'};current={'content':REQUEST,'attachments':[a]};run={'rag_decision':{}}
 draft=c.plan_conditional_evidence_review(run,current,[a]);assert draft is not None
 assert draft['answer_kind']=='review' and len(draft['source_scopes'])==1
 assert draft['source_scopes'][0]['selector']['condition']==REQUEST
 assert draft['deliverables']==[{'path':'模板审阅结果.md','format':'markdown'}]

@pytest.mark.parametrize('explicit_read',[False,True])
def test_normalize_synthesis_with_same_source_read_and_review_preserves_real_read_obligation(explicit_read):
 request=('请阅读全文，然后'+REQUEST) if explicit_read else REQUEST
 from agent.execution.document_goal import example_goal
 d=example_goal('s');review=copy.deepcopy(d['source_scopes'][0]);review.update(operation='review',scope='selected',target_ids=[],selector={'kind':'selection_pending','condition':request})
 d['source_scopes'].append(review)
 c.normalize_document_answer_draft(request,d)
 assert d['answer_kind']=='review'
 read=next(v for v in d['source_scopes'] if v['operation']=='read')
 assert read['scope']==('full' if explicit_read else 'metadata')


def test_same_task_saved_synthesis_read_plus_review_is_reconciled_before_dispatch(tmp_path,monkeypatch):
 async def exercise():
  service,run,attempt,root,current,body,invoked,published,_=await prepared_fixture(tmp_path,monkeypatch)
  try:
   body['answer_kind']='synthesis';read=copy.deepcopy(body['source_scopes'][0]);read.update(operation='read',scope='full',target_ids=[],selector=None)
   body['source_scopes'].insert(0,read)
   old=service.task_plans.goals.get(root['id']);req={k:body[k] for k in ('source_scopes','deliverables','success_criteria')}
   captured=service.task_plans.capture_requirements(root['id'],req,expected_revision=old['revision'],reconciled=True)
   with service.repository._connect() as db:db.execute('UPDATE task_goal_execution SET goal_revision=?,payload_json=? WHERE task_id=?',(captured['revision'],json.dumps(body),root['id']))
   assert c.reconcile_cached_local_document_goal(service,run,root,current,captured,captured['request_digest'])
   with service.repository._connect() as db:newbody=json.loads(db.execute('SELECT payload_json FROM task_goal_execution WHERE task_id=?',(root['id'],)).fetchone()[0])
   assert newbody['_first_result_id']==body['_first_result_id'] and newbody['answer_kind']=='review'
   # Fixture review updates the actual review scope and retains independent metadata/read scope.
   original=service._review_targets_tool
   async def review(*args):
    fresh=service.task_plans.goals.get(root['id']);read=next(copy.deepcopy(v) for v in fresh['source_scopes'] if v['operation']=='read')
    # The existing synthetic reviewer expects its review scope first.
    reordered={k:copy.deepcopy(fresh[k]) for k in ('source_scopes','deliverables','success_criteria')};reordered['source_scopes'].sort(key=lambda v:v['operation']!='review')
    cap=service.task_plans.capture_requirements(root['id'],reordered,expected_revision=fresh['revision'],reconciled=True)
    with service.repository._connect() as db:db.execute('UPDATE task_goal_execution SET goal_revision=? WHERE task_id=?',(cap['revision'],root['id']))
    value=await original(*args);fresh=service.task_plans.goals.get(root['id']);req={k:copy.deepcopy(fresh[k]) for k in ('source_scopes','deliverables','success_criteria')};req['source_scopes'].append(read)
    cap=service.task_plans.capture_requirements(root['id'],req,expected_revision=fresh['revision'],reconciled=True)
    with service.repository._connect() as db:db.execute('UPDATE task_goal_execution SET goal_revision=? WHERE task_id=?',(cap['revision'],root['id']))
    return value
   service._review_targets_tool=review
   with associate(service.execution_monitor,root['id']):assert await c.dispatch_explicit_review(service,run,attempt['id'],current,newbody,())
   assert len(invoked)==1 and published
  finally:await service.shutdown()
 asyncio.run(exercise())


@pytest.mark.parametrize('prompt',[ '不要审阅，仅总结附件。', '审阅已填写内容并修改正文，保存为“报告.md”。',
 '用指定技能审阅已填写内容，保存为“报告.md”。', '结合资料库审阅已填写内容，保存为“报告.docx”。'])
def test_nonreview_or_compound_intent_not_replanned(prompt):
 a={'id':'s','name':'a.docx'}
 assert c.plan_conditional_evidence_review({}, {'content':prompt,'attachments':[a]},[a]) is None


def test_confirmed_requirements_are_not_reduced_or_relabelled():
 from agent.execution.document_goal import example_goal
 d=example_goal('s');d['source_scopes'].append({**copy.deepcopy(d['source_scopes'][0]),'operation':'review'})
 before=copy.deepcopy(d)
 c.normalize_document_answer_draft(REQUEST,d,confirmed_requirements={'provenance':'user_clarification'})
 assert d==before


def test_explicit_full_read_is_not_completed_by_inspection_metadata_or_review_proof(tmp_path,monkeypatch):
 async def exercise():
  request='请阅读全文，然后'+REQUEST
  service,run,attempt,root,current,body,invoked,published,_=await prepared_fixture(tmp_path,monkeypatch,request)
  try:
   read=copy.deepcopy(body['source_scopes'][0]);read.update(operation='read',scope='full',target_ids=[],selector=None)
   body['source_scopes'].append(read);old=service.task_plans.goals.get(root['id'])
   req={k:body[k] for k in ('source_scopes','deliverables','success_criteria')}
   captured=service.task_plans.capture_requirements(root['id'],req,expected_revision=old['revision'],reconciled=True)
   with service.repository._connect() as db:db.execute('UPDATE task_goal_execution SET goal_revision=?,payload_json=? WHERE task_id=?',(captured['revision'],json.dumps(body),root['id']))
   assert not SourceInventory(service.repository).coverage(root['id'],read)['ready']
   with associate(service.execution_monitor,root['id']):assert await c.dispatch_explicit_review(service,run,attempt['id'],current,body,()) is None
   assert not invoked and not published
   # Actual original parser read, not synthesized coverage/proof.
   from agent.tool_contracts import ToolCall
   with associate(service.execution_monitor,root['id']):service._document_tool(run['id'],ToolCall('full-read','document.inspect_template',{'attachment_id':body['first_action']['attachment_id'],'limit':10}),complete_inventory=True)
   assert SourceInventory(service.repository).coverage(root['id'],read)['ready']
  finally:await service.shutdown()
 asyncio.run(exercise())
