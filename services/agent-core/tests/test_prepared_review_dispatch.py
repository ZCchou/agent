import asyncio,importlib.util,io,json,sys
from pathlib import Path
import pytest
from docx import Document
from agent.storage import CoreDataError
from agent.execution.activity import associate
from agent.execution.document_goal import example_goal
from agent.execution.source_inventory import SourceInventory
from agent.execution.task_plans import TaskPlans
from agent.tool_contracts import ToolCall
from test_document_attachments import source_attachment
from test_tool_continuation_retry import fixture
from agent.execution import document_goal_runtime as candidate
REQUEST='结合项目资料库，审阅附件中已经填写的正式内容，跳过空白、占位和示例，把结果保存为“模板审阅结果.md”。'

async def prepared_fixture(tmp_path,monkeypatch,request=REQUEST,scope_operation='review',deliver=True):
 async def emit(_):pass
 core,project,work,service=await fixture(tmp_path,emit)
 doc=Document();doc.add_paragraph('Formal operating content');doc.add_paragraph('Example content')
 buf=io.BytesIO();doc.save(buf);attachment=source_attachment(tmp_path,buf.getvalue(),'fixture.docx')
 run=core.start_model_run(conversation_id=work['id'],content=request,attachments=[attachment],provider_profile_id='synthetic',model_id='model',idempotency_key='prepared')
 core.transition_model_run(run['id'],{'queued'},'generating');attempt=core.begin_run_attempt(run['id'])
 root=service.execution_monitor.start_task(run_id=run['id'],project_id=project['id'])
 service.task_plans.create(root['id'],{'content':request,'attachments':[attachment]})
 first=ToolCall('first-inspect','document.inspect_template',{'attachment_id':attachment['id'],'limit':1})
 with associate(service.execution_monitor,root['id']):
  operation,resolution=await service.tool_loop.request(run_id=run['id'],attempt_id=attempt['id'],sequence=1,call=first,
   local_executor=lambda:service._document_tool(run['id'],first,max_characters=12000))
 assert resolution.success
 service.result_store.save(operation['id'],resolution.output,success=True)
 current=next(m for m in core.get_run_conversation(run['id'])['messages'] if m['id']==run['user_message_id'])
 body=example_goal(attachment['id']);body.update(answer_kind='review',evidence_required=True)
 body['first_action']={'tool':'document.inspect_template','attachment_id':attachment['id'],'limit':1}
 body['source_scopes']=[{'source_id':attachment['id'],'operation':scope_operation,'scope':'selected','target_ids':[],
  'selector':{'kind':'selection_pending','condition':request},'reason':request}]
 body['deliverables']=[{'path':'模板审阅结果.md','format':'markdown'}] if deliver else []
 requirements={key:body[key] for key in ('source_scopes','deliverables','success_criteria')}
 goals=service.task_plans.capture_requirements(root['id'],requirements,expected_revision=0)
 body['_first_result_id']=operation['id']
 with core._connect() as db:db.execute('INSERT INTO task_goal_execution VALUES(?,?,?,?)',(root['id'],goals['revision'],goals['request_digest'],json.dumps(body)))
 state={'generation':0,'project_id':project['id']};service.rag_grounding.runs[run['id']]=state
 checked=[];monkeypatch.setattr(service.rag_grounding,'validate',lambda _:checked.append(state['generation']))
 invoked=[]
 async def review(run,attempt_id,args):
  invoked.append(dict(args));fresh=service.task_plans.goals.get(root['id'])
  selected={**fresh['source_scopes'][0],'scope':'selected','target_ids':['p-0']};selected.pop('selector',None)
  updated={key:fresh[key] for key in ('source_scopes','deliverables','success_criteria')};updated['source_scopes']=[selected]
  captured=service.task_plans.capture_requirements(root['id'],updated,expected_revision=fresh['revision'],reconciled=True)
  with core._connect() as db:
   db.execute('UPDATE task_goal_execution SET goal_revision=? WHERE task_id=?',(captured['revision'],root['id']))
   generation=TaskPlans.current_evidence_generation(db,root['id'])
   db.execute('INSERT OR REPLACE INTO task_review_proofs VALUES(?,?,?,?,?,?)',(root['id'],attachment['id'],attachment['sha256'],'p-0','done',generation))
  artifact=service.documents.create(run['id'],'Controlled review report','markdown')
  service.documents.record_review(run['id'],artifact['id'],{'template_id':args['template_id'],'template_sha256':attachment['sha256'],'coverage':{'total':1},'review_status':'reviewed'})
  with core._connect() as db:
   metadata=service.documents.get(run['id'],artifact['id'])['manifest'];metadata['rag_generation']=state['generation']
   db.execute('UPDATE document_artifacts SET manifest_json=? WHERE id=?',(json.dumps(metadata),artifact['id']))
  run['_review_reports']=['Controlled review report']
  return json.dumps({'artifact_id':artifact['id'],'sha256':artifact['sha256'],'format':'markdown'})
 service._review_targets_tool=review
 published=[]
 from agent.execution import prepared_review_publication
 async def publish(service_,run_,attempt_,current_,goals_,receipt):
  assert service_.task_plans.completion(root['id'])['ready'] is False,'File deliverable must remain pending before publication'
  assert SourceInventory(core).coverage(root['id'],goals_['source_scopes'][0])['ready']
  published.append(receipt);return 'Published controlled report'
 monkeypatch.setattr(prepared_review_publication,'publish_requested_review',publish)
 return service,run,attempt,root,current,body,invoked,published,checked


def test_prepared_pending_review_dispatches_actual_template_before_native_loop_and_publishes_after_review(tmp_path,monkeypatch):
 async def exercise():
  service,run,attempt,root,current,body,invoked,published,checked=await prepared_fixture(tmp_path,monkeypatch)
  try:
   with associate(service.execution_monitor,root['id']):result=await candidate.dispatch_explicit_review(service,run,attempt['id'],current,body,())
   assert result=='Published controlled report' and len(invoked)==1 and len(published)==1 and checked
   assert set(invoked[0])=={'template_id','format'} and invoked[0]['template_id']
   with service.repository._connect() as db:receipt=json.loads(db.execute('SELECT payload_json FROM task_goal_execution WHERE task_id=?',(root['id'],)).fetchone()[0])['_prepared_review']
   assert receipt['artifact_id']==published[0]['artifact_id'] and receipt['source_sha256']
   with associate(service.execution_monitor,root['id']):assert await candidate.dispatch_explicit_review(service,run,attempt['id'],current,body,())==result
   assert len(invoked)==1 and len(published)==2,'Resume reuses verified review, publication controls its own idempotency'
   service.repository.finish_attempt(attempt['id'],None)
   next_attempt=service.repository.begin_run_attempt(run['id'])
   with associate(service.execution_monitor,root['id']):assert await candidate.dispatch_explicit_review(service,run,next_attempt['id'],current,body,())==result
   assert len(invoked)==1 and len(published)==3,'A new run attempt must not replay the completed review'
  finally:await service.shutdown()
 asyncio.run(exercise())

@pytest.mark.parametrize('prompt,operation',[
 ('不要审阅附件，只读取标题。','read'),('审阅附件并修改正文，然后保存为“模板审阅结果.md”。','review'),
 ('请使用指定技能审阅附件，并保存为“模板审阅结果.md”。','review'),
 ('审阅附件，不要保存为“模板审阅结果.md”。','review')])
def test_unrequested_or_compound_work_keeps_existing_agent_path(tmp_path,monkeypatch,prompt,operation):
 async def exercise():
  service,run,attempt,root,current,body,invoked,published,_=await prepared_fixture(tmp_path,monkeypatch,prompt,operation)
  try:
   with associate(service.execution_monitor,root['id']):assert await candidate.dispatch_explicit_review(service,run,attempt['id'],current,body,()) is None
   assert not invoked and not published
  finally:await service.shutdown()
 asyncio.run(exercise())


@pytest.mark.parametrize('fault',['source_hash','cancelled','cached_artifact','cached_generation'])
def test_prepared_dispatch_never_reuses_revoked_changed_or_corrupt_scope_receipt(tmp_path,monkeypatch,fault):
 async def exercise():
  service,run,attempt,root,current,body,invoked,published,_=await prepared_fixture(tmp_path,monkeypatch)
  try:
   if fault.startswith('cached_'):
    with associate(service.execution_monitor,root['id']):assert await candidate.dispatch_explicit_review(service,run,attempt['id'],current,body,())
    assert len(invoked)==1 and len(published)==1
    artifact=published[0]['artifact_id']
    if fault=='cached_artifact':service.documents.path_for(run['id'],artifact).write_text('tampered')
    else:
     with service.repository._connect() as db:
      metadata=service.documents.get(run['id'],artifact)['manifest'];metadata['rag_generation']=99
      db.execute('UPDATE document_artifacts SET manifest_json=? WHERE id=?',(json.dumps(metadata),artifact))
   elif fault=='source_hash':
    with service.repository._connect() as db:db.execute("UPDATE task_inventory_sources SET source_hash='changed' WHERE task_id=?",(root['id'],))
   else:service.execution_monitor.control(root['id'],'cancel',revision=root['revision'],idempotency_key='cancel-prepared')
   before=len(published)
   with pytest.raises(CoreDataError):
    with associate(service.execution_monitor,root['id']):await candidate.dispatch_explicit_review(service,run,attempt['id'],current,body,())
   assert len(published)==before
   assert len(invoked)==(1 if fault.startswith('cached_') else 0)
  finally:await service.shutdown()
 asyncio.run(exercise())


def test_incomplete_review_proofs_allow_delivery_without_marking_coverage_ready(tmp_path,monkeypatch):
 async def exercise():
  service,run,attempt,root,current,body,invoked,published,_=await prepared_fixture(tmp_path,monkeypatch)
  original=service._review_targets_tool
  async def incomplete(*args):
   value=await original(*args)
   with service.repository._connect() as db:db.execute('DELETE FROM task_review_proofs WHERE task_id=?',(root['id'],))
   return value
  service._review_targets_tool=incomplete
  from agent.execution import prepared_review_publication
  async def publish_partial(service_,run_,attempt_,current_,goals_,receipt):
   assert not SourceInventory(service_.repository).coverage(root['id'],goals_['source_scopes'][0])['ready']
   published.append(receipt)
   return 'Report with unverified items'
  monkeypatch.setattr(prepared_review_publication,'publish_requested_review',publish_partial)
  try:
   with associate(service.execution_monitor,root['id']):
    text=await candidate.dispatch_explicit_review(service,run,attempt['id'],current,body,())
   assert text=='Report with unverified items' and len(published)==1
   with service.repository._connect() as db:payload=json.loads(db.execute('SELECT payload_json FROM task_goal_execution WHERE task_id=?',(root['id'],)).fetchone()[0])
   assert payload['_prepared_review']['artifact_id']==published[0]['artifact_id']
  finally:await service.shutdown()
 asyncio.run(exercise())
