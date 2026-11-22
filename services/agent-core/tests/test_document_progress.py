import json
from types import SimpleNamespace
from test_task_plans import service
from agent.execution.source_inventory import SourceInventory

def fixture(tmp_path):
 core,monitor,root,plans=service(tmp_path)
 fields=[{'id':f'p-{i}','text':f'Original field {i}','field_type':'content'} for i in range(2409)]
 store=SourceInventory(core);store.register(root['id'],'source','template','sha',fields,complete=True)
 decisions={f'p-{i}':{'decision':'include' if i%2 else 'exclude','reason':'Saved source decision','text_sha256':'hash'} for i in range(859)}
 body={'_review_selection':{'identity':{'source_id':'source','source_sha256':'sha'},'decisions':decisions,'batches':[{}]*12,'context_notes':'PRIVATE HIDDEN REASONING NEVER PUBLIC','context_ids':[]}}
 with core._connect() as db:db.execute('INSERT INTO task_goal_execution VALUES(?,?,?,?)',(root['id'],1,'request',json.dumps(body)))
 return core,monitor,root,plans,body


def test_saved_selection_survives_null_token_progress_and_never_exposes_private_notes(tmp_path):
 from agent.execution.document_progress import DocumentProgress
 core,_,root,_,body=fixture(tmp_path);root.update(stage='document_review',progress={'completed':None,'total':None},status='running')
 progress=DocumentProgress(core).snapshot(root)
 assert (progress['completed'],progress['total'],progress['batch_count'])==(859,2409,12)
 assert progress['counts']['include']==429 and progress['counts']['exclude']==430
 assert progress['recent'][-1]['target_id']=='p-858' and progress['recent'][-1]['reason']=='Saved source decision'
 assert 'PRIVATE' not in json.dumps(progress) and progress['phase']=='source_selection'


def test_token_snapshots_do_not_reparse_large_checkpoint_but_explicit_refresh_is_fresh(tmp_path,monkeypatch):
 from agent.execution import document_progress as module
 core,_,root,_,body=fixture(tmp_path);reader=module.DocumentProgress(core,clock=lambda:1)
 original=module.json.loads;reads=[]
 def loads(value,*a,**kw):
  if 'PRIVATE HIDDEN' in value:reads.append(1)
  return original(value,*a,**kw)
 monkeypatch.setattr(module.json,'loads',loads)
 for _ in range(1000):assert reader.snapshot(root)['completed']==859
 assert len(reads)==1
 body['_review_selection']['decisions']['p-859']={'decision':'context','reason':'Saved new result'}
 with core._connect() as db:db.execute('UPDATE task_goal_execution SET payload_json=?',(json.dumps(body),))
 assert reader.snapshot(root,refresh=True)['completed']==860 and len(reads)==2


def test_cancel_or_recovery_never_becomes_completed_from_finished_classification(tmp_path):
 from agent.execution.document_progress import DocumentProgress
 core,_,root,_,body=fixture(tmp_path);reader=DocumentProgress(core)
 for status in ['cancelled','waiting_recovery']:
  root['status']=status;value=reader.snapshot(root)
  assert value['status']==status and not value.get('task_completed',False)


def test_public_review_count_uses_committed_current_primary_batches_only(tmp_path):
 from agent.execution.document_progress import DocumentProgress
 core,_,root,plans,_=fixture(tmp_path)
 plans.register_source(root['id'],'template','template','sha',[{'id':'p-1','text':'A requirement'}],operation='review')
 progress=DocumentProgress(core)
 assert progress.snapshot(root)['batch_count']==0
 identity={'template_id':'template','template_sha256':'sha'}
 payload={'goal_revision':1,'request_digest':'request','body':'Reviewed content'}
 with core._connect() as db:
  for suffix,ident,body in [('one',identity,payload),('one-supplement-retry',identity,payload),
      ('empty',identity,{**payload,'body':''}),('old',identity,{**payload,'goal_revision':0}),
      ('changed',{**identity,'template_sha256':'other'},payload)]:
   db.execute('INSERT INTO execution_wait_checkpoints VALUES(?,?,?,?,?,?,?,?)',
              (root['id'],'document_review','document-public-review-'+suffix,json.dumps(ident),
               json.dumps(body),root['attempt'],root['revision'],1))
 assert progress.snapshot(root,refresh=True)['batch_count']==1


def test_execution_status_and_events_use_same_public_projection(tmp_path):
 from agent.runtime import ChatRunService
 core,monitor,root,plans,_=fixture(tmp_path)
 service_=ChatRunService.__new__(ChatRunService);service_.repository=core;service_.execution_monitor=monitor;service_.task_plans=plans
 service_.goal_verification=SimpleNamespace(latest=lambda _:None);service_.task_input=SimpleNamespace(pending=lambda _:None,answered_ids=lambda _:[])
 root={**root,'progress':{'completed':None,'total':None},'stage':'model_output'}
 event=service_._execution_snapshot(root)
 assert event['document_progress']['completed']==859
 assert service_.execution_status({'task_id':root['id']})['document_progress']['total']==2409


def test_saved_technical_review_counts_only_current_proofs_and_displays_verified_citations(tmp_path):
 from agent.execution.document_progress import DocumentProgress
 core,_,root,plans,_=fixture(tmp_path)
 fields=[{'id':'p-1','text':'Operating requirement'},{'id':'p-2','text':'Second requirement'}]
 plans.register_source(root['id'],'template','template','sha',fields,operation='review')
 plans.record_results(root['id'],'template','sha',[{'target_id':'p-1','status':'reviewed','summary':'Saved technical assessment',
  'citations':[{'id':'source-evidence','quote_id':'q-exact'}],
  'evidence':[{'title':'Standard X','revision':'G','clause_number':'3.2','locator':'p.12','quote':'ORIGINAL QUOTE NOT A PROGRESS NOTE'}]}])
 with core._connect() as db:
  db.execute('INSERT INTO execution_wait_checkpoints VALUES(?,?,?,?,?,?,?,?)',(root['id'],'document_review','document-review-batch',
   json.dumps({'template_sha256':'sha'}),json.dumps({'results':[{'target_id':'p-1','summary':'PRIVATE_RAW_RESPONSE'}]}),root['attempt'],root['revision'],1))
 value=DocumentProgress(core).snapshot(root)
 assert value['phase']=='document_review' and (value['completed'],value['total'],value['batch_count'])==(1,2,1)
 assert value['latest_review']=={'target_id':'p-1','summary':'Saved technical assessment','citations':['Standard X · G · 3.2 · p.12']}
 assert 'PRIVATE_RAW_RESPONSE' not in json.dumps(value) and 'ORIGINAL QUOTE' not in json.dumps(value)
 with core._connect() as db:db.execute("UPDATE task_inventory_sources SET source_hash='changed' WHERE task_id=?",(root['id'],))
 assert DocumentProgress(core).snapshot(root) is None,'Historical source/proofs cannot impersonate current source work'


def test_changed_selected_scope_does_not_show_historical_review_items_as_current(tmp_path):
 from agent.execution.document_progress import DocumentProgress
 core,_,root,plans,_=fixture(tmp_path)
 fields=[{'id':'p-1','text':'Current item'},{'id':'p-2','text':'Historical item'}]
 plans.register_source(root['id'],'source','template','sha',fields,operation='review')
 plans.record_results(root['id'],'template','sha',[{'target_id':key,'status':'reviewed','summary':key} for key in ('p-1','p-2')])
 plans.capture_requirements(root['id'],{'source_scopes':[{'source_id':'source','operation':'review','scope':'selected','target_ids':['p-1'],'reason':'Current declared scope'}],
  'deliverables':[],'success_criteria':['Current selected scope only']},expected_revision=0)
 value=DocumentProgress(core).snapshot(root)
 assert (value['completed'],value['total'])==(1,1) and value['latest_review']['target_id']=='p-1'



def test_full_review_waiting_first_batch_uses_artifact_alias_and_real_current_items(tmp_path):
 from agent.execution.document_progress import DocumentProgress
 core,_,root,plans,_=fixture(tmp_path)
 plans.register_source(root['id'],'template','template','sha',[{'id':'p-1','text':'Actual item'}],operation='review')
 root['progress']={'details':{'public_document_event':{'phase':'document_review','event':'checking','total':1,'current':[{'target_id':'p-1','excerpt':'Actual item'}]}}}
 value=DocumentProgress(core).snapshot(root)
 assert value is not None and (value['completed'],value['total'])==(0,1)
 assert value['current'][0]['target_id']=='p-1' and value['phase']=='document_review'


def test_actual_integrity_file_progress_is_public_before_first_result(tmp_path):
 from agent.execution.document_progress import DocumentProgress
 core,_,root,_,_=fixture(tmp_path)
 root['progress']={'details':{'stage':'integrity_verification','integrity_phase':'runtime_hashes',
  'integrity_files_processed':10125,'integrity_total_files':13129,'integrity_bytes_read':4532044338}}
 value=DocumentProgress(core).snapshot(root)
 assert (value['phase'],value['completed'],value['total'])==('runtime_integrity',10125,13129)
 assert value['bytes_read']==4532044338


def test_public_business_events_are_bounded_deduplicated_and_cancel_fenced(tmp_path):
 from agent.execution.document_progress import publish_document_event
 core,monitor,root,_,_=fixture(tmp_path);activity=monitor._begin(root['id'],'document_review')
 fields=[{'id':f'p-{i}','text':'Actual original '*100} for i in range(20)]
 assert publish_document_event(activity,'document_review','checking',fields=fields,total=20)
 with core._connect() as db:before=db.execute('SELECT COUNT(*) FROM execution_wait_activity WHERE task_id=?',(root['id'],)).fetchone()[0]
 assert publish_document_event(activity,'document_review','checking',fields=fields,total=20)
 with core._connect() as db:assert db.execute('SELECT COUNT(*) FROM execution_wait_activity WHERE task_id=?',(root['id'],)).fetchone()[0]==before
 saved=monitor.get_task(root['id'])['progress']['details']['public_document_event']
 assert len(saved['current'])==3 and len(saved['current'][0]['excerpt'])<=160
 monitor.control(root['id'],'cancel',revision=monitor.get_task(root['id'])['revision'],idempotency_key='stop')
 assert not publish_document_event(activity,'document_review','searching',query='Actual query',total=20)
 assert not publish_document_event(activity,'document_review','checking',fields=fields,total=20)
 with core._connect() as db:assert db.execute('SELECT COUNT(*) FROM execution_wait_activity WHERE task_id=?',(root['id'],)).fetchone()[0]==before



def test_real_review_engine_announces_current_original_before_first_evidence_and_saved_result_after_validation(tmp_path):
 import asyncio
 from docx import Document
 from test_document_review import setup as review_fixture,register
 from agent.document_review import review_template
 workflow,run,monitor,root,sources=review_fixture.__wrapped__(tmp_path)
 document=Document();document.add_paragraph('实际填写的设备要求。')
 template=register(workflow,run,document)
 observed=[]
 def evidence(batch):
  event=monitor.get_task(root['id'])['progress']['details']['public_document_event']
  assert event['event']=='checking' and event['current'][0]['excerpt']=='实际填写的设备要求。'
  observed.append(event['event']);return sources.evidence(batch)
 async def operation(activity):
  return await review_template(workflow,run['id'],template['template_id'],'审阅全文',lambda batch,context:sources.response(batch),evidence,sources.resolve,activity=activity)
 asyncio.run(monitor.execute_stage(root['id'],'document_review',operation))
 with workflow.repository._connect() as db:
  rows=db.execute('SELECT payload_json FROM execution_wait_activity WHERE task_id=? ORDER BY id',(root['id'],)).fetchall()
 events=[json.loads(row[0])['public_document_event']['event'] for row in rows if 'public_document_event' in json.loads(row[0])]
 assert observed and events[0]=='checking' and 'results_saved' in events and events.index('checking')<events.index('results_saved')



def test_first_inspection_without_active_model_stage_is_public_and_replayable(tmp_path):
 from agent.execution.activity import associate
 from agent.execution.document_progress import publish_document_event,DocumentProgress
 core,monitor,root,_,_=fixture(tmp_path)
 with associate(monitor,root['id']):
  assert publish_document_event(None,'attachment_read','reading')
  assert publish_document_event(None,'attachment_read','inventory_ready',fields=[{'id':'p-1','text':'Read actual original'}],total=13)
 value=DocumentProgress(core).snapshot(monitor.get_task(root['id']))
 assert value['phase']=='attachment_read' and value['event']=='inventory_ready' and value['current'][0]['target_id']=='p-1'
 with core._connect() as db:assert db.execute("SELECT COUNT(*) FROM execution_wait_activity WHERE task_id=? AND kind='document_business'",(root['id'],)).fetchone()[0]==2
 monitor.control(root['id'],'cancel',revision=monitor.get_task(root['id'])['revision'],idempotency_key='cancel-read')
 with associate(monitor,root['id']):assert not publish_document_event(None,'attachment_read','reading')
