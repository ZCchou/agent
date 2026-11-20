import asyncio,json
import httpx
from agent.storage import CoreDataError
from agent.rag.service import RagService
from agent.rag.grounding import RagGrounding
from test_rag_service import Engines,ready,wait_job
from test_rag_chat_integration import fixture,configuration
from test_template_tool_repair import native_call


def test_real_native_dispatch_closes_capacity_then_control_only_loop_with_one_no_tool_answer(tmp_path,monkeypatch):
 original=httpx.AsyncClient;requests=[];holder={};observed=[]
 from agent.rag.capacity_progress import CapacitySynthesisGuard
 observe=CapacitySynthesisGuard.observe
 def inspect(self,fingerprint,names,**kw):
  result=observe(self,fingerprint,names,**kw);observed.append({'names':names,'allowed':kw['allowed'],'has_evidence':kw['has_evidence'],'failures':self.failures,'controls':self.controls,'fingerprint':fingerprint[:12],'triggered':result});return result
 monkeypatch.setattr(CapacitySynthesisGuard,'observe',inspect)
 def respond(request):
  payload=json.loads(request.content)
  from goal_check_support import goal_check_content
  checked=goal_check_content(payload)
  if checked is not None:delta={'content':checked};finish='stop'
  else:
   requests.append(payload);number=len(requests)
   if number<=3:delta=native_call('rag_search',{'query':f'25.1309 失效 安全 {number}'},f'search-{number}');finish='tool_calls'
   elif number==4:delta=native_call('tools_discover',{'names':['task.update_plan']},'discover');finish='tool_calls'
   elif number==5:
    service=holder['service'];run=next(iter(service.rag_grounding.runs));root=service.execution_monitor.start_task(run_id=run)
    revision=service.task_plans.get(root['id'])['revision']
    delta=native_call('task_update_plan',{'expected_revision':revision,'steps':[],'success_criteria':['依据实际证据回答用户问题']},'plan');finish='tool_calls'
   else:
    state=next(iter(holder['service'].rag_grounding.runs.values()));key=next(iter(state['admitted']));quote=next(iter(state['quote_options'][key]))
    delta={'content':json.dumps({'claims':[{'text':'需要核查失效状态安全性要求。','evidence':[{'id':key,'quote_id':quote}]}]},ensure_ascii=False)};finish='stop'
  return httpx.Response(200,text='data: '+json.dumps({'choices':[{'delta':delta,'finish_reason':finish}]})+'\n\ndata: [DONE]\n\n')
 monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(respond),**kw))
 async def exercise():
  async def sink(_):pass
  config=configuration();config['base_url']='http://127.0.0.1:12345/v1'
  core,project,chat,service=await fixture(tmp_path,sink,config);holder['service']=service
  await service.rag.close();service.rag=RagService(core,Engines());service.rag_grounding=RagGrounding(service.rag);service.context.rag_context=service.rag_grounding.evidence
  try:
   library=(await service.rag.handle('library.create',{'name':'Rules','embedding_model_id':'embed','reranker_model_id':'rank'}))['library']['id']
   await ready(service.rag,library);assert (await wait_job(service.rag,await service.rag.handle('index.build',{'library_id':library})))['status']=='completed'
   await service.rag.handle('library.bind',{'library_id':library,'project_id':project['id'],'enabled':True})
   core._insert_message(chat['id'],'user','设备条件：飞行关键设备。');core._insert_message(chat['id'],'assistant','已记录设备条件。')
   actual=service._rag_tool;calls=[]
   async def capacity(run,call,max_characters):
    calls.append(call.name)
    if len(calls)>1:
     from agent.rag.capacity_progress import capacity_feedback
     details={'evidence_capacity_exhausted':True,'admitted_citations':[]}
     message=capacity_feedback(run,call.name,details)
     raise CoreDataError('rag_context_too_small',message,details)
    return await actual(run,call,max_characters)
   service._rag_tool=capacity
   result=await service.start({'conversation_id':chat['id'],'content':'按上面的设备条件，请依据项目资料库引用原文说明25.1309失效要求。','idempotency_key':'capacity-control'})
   identity=result['run']['id'];await service.tasks[identity];run=core.get_model_run(identity)
   assert run['status']=='completed',(run.get('error_code'),run.get('error_message'))
   assert len(requests)==6 and not requests[-1].get('tools'),observed
   assert 'rag-evidence:' in run['assistant_message']['content']
   operations=service.list_tool_operations(identity)
   assert [op['tool_name'] for op in operations]==['rag.search']*3+['tools.discover','task.update_plan']
   assert sum(op['error_code']=='rag_context_too_small' for op in operations)==2
  finally:await service.shutdown()
 asyncio.run(exercise())
