"""Template-id entry exercises real query callback, RAG store and review artifact."""
import asyncio,io,json
from types import SimpleNamespace
import pytest
from docx import Document
from agent.execution.activity import associate
from agent.tool_contracts import ToolCall
from agent.rag.service import RagService
from agent.rag.grounding import RagGrounding
from test_tool_continuation_retry import fixture
from test_rag_service import Engines,ready,wait_job
from test_unified_invocation_v3 import model_spec

@pytest.mark.parametrize('legacy_name_missing',[False,True])
@pytest.mark.parametrize('rag_mode',['hits','empty','disabled'])
@pytest.mark.parametrize('public_body',[None,'现有步骤缺少升温阶段说明。\n\n建议保留原文，补充记录要求。 [模型引用](rag-evidence:invented-id)'])
def test_review_targets_template_id_uses_manifest_topic_without_attachment(tmp_path,monkeypatch,legacy_name_missing,public_body,rag_mode,fault=None):
 async def exercise():
  public_events=[]
  async def emit(event):
   if event.get('event_type')=='run.document_processing' and event['data'].get('phase')=='public_text':
    public_events.append(event)
  core,project,work,service=await fixture(tmp_path,emit)
  await service.rag.close();service.rag=RagService(core,Engines());service.rag_grounding=RagGrounding(service.rag)
  service.document_attachments.parser=service.rag.parser
  service.rag.execution_monitor=service.execution_monitor
  try:
   lib=(await service.rag.handle('library.create',{'name':'Rules','embedding_model_id':'embed','reranker_model_id':'rank'}))['library']['id']
   await ready(service.rag,lib)
   assert (await wait_job(service.rag,await service.rag.handle('index.build',{'library_id':lib})))['status']=='completed'
   await service.rag.handle('library.bind',{'library_id':lib,'project_id':project['id'],'enabled':True})
   if rag_mode=='disabled':
    await service.rag.handle('library.bind',{'library_id':lib,'project_id':project['id'],'enabled':False})
   request='结合项目资料库审阅所有现有位置。'
   run=core.start_model_run(conversation_id=work['id'],content=request,idempotency_key='template-id-review',provider_profile_id='synthetic',model_id='model')
   core.transition_model_run(run['id'],{'queued'},'generating');attempt=core.begin_run_attempt(run['id'])
   root=service.execution_monitor.start_task(run_id=run['id'],project_id=project['id'])
   service.task_plans.create(root['id'],{'content':request})
   doc=Document();doc.add_paragraph('短时工作低温试验应保留试验报告。');buf=io.BytesIO();doc.save(buf)
   template=service.documents.register_template(run['id'],buf.getvalue(),'短时工作低温试验步骤.docx')
   if legacy_name_missing:
    with core._connect() as db:
     manifest=dict(template['manifest']);manifest.pop('name',None)
     db.execute('UPDATE document_artifacts SET manifest_json=? WHERE id=?',(json.dumps(manifest),template['id']))
   step={'id':'fixture-step','step_type':'final_response','protocol_id':'openai_chat','model_id':'model'}
   monkeypatch.setattr(service.execution_coordinator,'get_for_run',lambda _: {'steps':[step]})
   monkeypatch.setattr(service,'_create_model_invocation_spec',lambda *a,**kw:model_spec(protocol_id='openai_chat',adapter_id='openai.chat',route_id='chat',allowed_tool_names=kw['allowed_tools'],max_output_tokens=kw['max_output_tokens'],response_format=kw['response_format']))
   queries=[];search=service.rag.search
   async def checked_search(project_id,query,**kw):
    assert project_id==project['id'] and kw['run_id']==run['id'] and kw['constraint_text']==request
    queries.append(query)
    if rag_mode=='empty':return {'evidence':[],'generation':service.rag.project_state(project['id'])['generation']}
    return await search(project_id,query,**kw)
   monkeypatch.setattr(service.rag,'search',checked_search)
   calls=[]
   async def invoke(run_,plan_,step_,attempt_,provider,messages,**kw):
    calls.append(kw)
    assert kw.get('response_schema') is None and kw.get('response_format') is None
    payload=json.loads(messages[-1]['content']);batch=payload['batch']
    if fault=='cancel':
     task=service.execution_monitor.get_task(root['id']);service.execution_monitor.control(root['id'],'cancel',revision=task['revision'],idempotency_key='cancel-public-body')
    if fault=='source':service.documents.path_for(run['id'],template['id']).write_bytes(b'changed-original')
    if public_body is not None:
     if public_body and (not fault or fault in ('after_public','length')):
      notify=kw['on_public_text'];await notify(public_body[:1])
      assert public_events[-1]['data']['delta']==public_body[:1]
      with core._connect() as db:assert db.execute("SELECT count(*) FROM document_artifacts WHERE run_id=? AND kind='markdown'",(run['id'],)).fetchone()[0]==0
      if fault=='after_public':
       from agent.storage import CoreDataError
       raise CoreDataError('rag_worker_crash','Synthetic failure after public output')
      if len(public_body)>1:await notify(public_body[1:])
     if fault=='length':
      from agent.providers import validate_provider_completion
      validate_provider_completion(public_body,'length',{})
     return public_body,{},[]
    assert payload['sources']['generation']==(None if rag_mode=='disabled' else service.rag.project_state(project['id'])['generation'])
    return json.dumps({'response':{'results':[{'target_id':f['id'],'status':'pending','content_kind':f['field_type'],'summary':'当前资料不足，待补充直接条款。','problems':[],'evidence':[],'version_issues':[],'gaps':['缺少直接适用依据。']} for f in batch['fields']]}}),{},[]
   monkeypatch.setattr(service,'_invoke_planned_step',invoke)
   with associate(service.execution_monitor,root['id']):
    args={'template_id':template['id'],'format':'markdown'}
    try:
     operation,resolution=await service.tool_loop.request(run_id=run['id'],attempt_id=attempt['id'],sequence=1,call=ToolCall('template-id-review-call','document.review_targets',args),async_executor=lambda:service._review_targets_tool(run,attempt['id'],args))
    except asyncio.CancelledError:
     assert fault=='cancel'
     with core._connect() as db:assert db.execute("SELECT count(*) FROM document_artifacts WHERE run_id=? AND kind='markdown'",(run['id'],)).fetchone()[0]==0
     return
    if fault and fault!='length':
     assert len(calls)==1, 'A public generation must not be replayed after output'
     assert not resolution.success
     with core._connect() as db:assert db.execute("SELECT count(*) FROM document_artifacts WHERE run_id=? AND kind='markdown'",(run['id'],)).fetchone()[0]==0
     return
    assert resolution.success, resolution.output
    receipt=service.result_store.save(operation['id'],resolution.output,success=resolution.success)
    assert operation['status']=='completed' and receipt['success'] and receipt['result_sha256']
    result=json.loads(resolution.output)
   assert (not queries if rag_mode=='disabled' else 1<=len(queries)<=3)
   artifact=service.documents.get(run['id'],result['artifact_id'])
   assert artifact['sha256']==result['sha256'] and artifact['manifest']['review_report']['template_sha256']==template['sha256']
   assert service.documents.path_for(run['id'],artifact['id']).read_text(encoding='utf8')
   assert result['coverage']['total']==1
   assert len(calls)==2, 'Unmapped positions receive only one bounded supplement'
   assert all(item['status']=='pending' for section in artifact['manifest']['review_report']['public_review_sections'] for item in section['target_delivery'])
   if public_body is not None:
    saved=service.documents.path_for(run['id'],artifact['id']).read_text(encoding='utf8')
    if public_body:
     assert ''.join(e['data']['delta'] for e in public_events)==public_body * 2
     assert len({e['data']['stream_id'] for e in public_events})==2
    else:
     assert '系统状态：本批没有收到公开正文' in saved
     assert artifact['manifest']['review_report']['public_review_sections'][0]['public_output_state']=='empty'
    if fault=='length':
     assert result['coverage']['partial_targets']==1 and result['coverage']['prose_processed']==0 and '输出上限' in saved
    assert '出处' in saved
    assert artifact['manifest']['review_report']['grounding_status']=='unverified_model_prose'
    assert artifact['manifest']['review_report']['evidence_participation']==('retrieved' if rag_mode=='hits' else 'no_evidence')
    if rag_mode=='disabled':assert 'rag_generation' not in artifact['manifest']
    assert artifact['manifest']['review_report']['public_review_sections'][0]['body']==public_body
  finally:await service.shutdown()
 asyncio.run(exercise())


@pytest.mark.parametrize('fault',['cancel','source'])
def test_public_prose_does_not_bypass_cancellation_or_original_sha(tmp_path,monkeypatch,fault):
 test_review_targets_template_id_uses_manifest_topic_without_attachment(tmp_path,monkeypatch,False,'现有步骤缺少升温阶段说明。','hits',fault=fault)


def test_non_target_json_and_unverified_citation_are_saved_as_public_prose(tmp_path,monkeypatch):
 body='{"observation":"现有步骤缺少升温阶段说明。","reference":{"id":"model-invented","quote_id":"not-verified"}}'
 test_review_targets_template_id_uses_manifest_topic_without_attachment(tmp_path,monkeypatch,False,body,'empty')


@pytest.mark.parametrize('body',['{"tool_calls":"user requested public JSON"}','{"function_call":{"example":true}}',''])
def test_public_json_keys_are_not_guessed_as_transport_or_system_authorship(tmp_path,monkeypatch,body):
 test_review_targets_template_id_uses_manifest_topic_without_attachment(tmp_path,monkeypatch,False,body,'disabled')


def test_public_generation_is_not_transparently_replayed_after_partial_output(tmp_path,monkeypatch):
 test_review_targets_template_id_uses_manifest_topic_without_attachment(tmp_path,monkeypatch,False,'public partial','disabled',fault='after_public')


def test_public_length_preserves_streamed_text_and_marks_partial(tmp_path,monkeypatch):
 test_review_targets_template_id_uses_manifest_topic_without_attachment(tmp_path,monkeypatch,False,'部分正文','hits',fault='length')
