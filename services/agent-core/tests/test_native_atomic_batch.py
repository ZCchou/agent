import asyncio,importlib.util,json,os,sys
from pathlib import Path
import httpx,pytest
from agent import providers
from agent.execution.compiler import InvocationCompiler
from test_unified_invocation_v3 import model_spec


def call(index,name='document_inspect_template',args='{}',identity=None):
 return {'index':index,'id':identity if identity is not None else 'call-'+str(index),'type':'function','function':{'name':name,'arguments':args}}


def run(monkeypatch,calls,finish='tool_calls'):
 original=httpx.AsyncClient
 def respond(_):return httpx.Response(200,text='data: '+json.dumps({'choices':[{'delta':{'tool_calls':calls},'finish_reason':finish}]})+'\n\ndata: [DONE]\n\n')
 monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(respond),**kw))
 spec=model_spec(protocol_id='openai_chat',adapter_id='openai.chat',capability_signature=('function_tools','text'),allowed_tool_names=('document.inspect_template','fs.list','fs.read'),tool_transport='native_function_call')
 prepared=InvocationCompiler().compile_model(spec,[{'role':'user','content':'request'}])
 provider=providers.OpenAICompatibleProvider(providers.ProviderProfile('p','P','openai_compatible','https://synthetic.invalid/v1',primary_protocol='openai_chat'))
 observed=[];failure=None
 async def exercise():
  nonlocal failure
  try:
   async for event in provider.stream_events('model',list(prepared.messages),tools=True,options=prepared.options):
    if event.tool_call:observed.append((event.kind,event.tool_call.name))
  except providers.ProviderError as error:failure=error
 asyncio.run(exercise())
 return observed,failure

@pytest.mark.parametrize('bad_first',[False,True])
def test_atomic_invalid_local_batch_never_forwards_and_can_be_corrected(monkeypatch,bad_first):
 calls=[call(0,'fs_list','{"path":"."}'),call(1)]
 if bad_first:calls=[dict(calls[1],index=0),dict(calls[0],index=1)]
 observed,error=run(monkeypatch,calls)
 assert error.code=='model_tool_protocol_failed'
 assert observed==[]
 assert error.details['native_tool_correction_eligible'] is True
 assert error.details['native_tool_diagnostic']['certification_reason']=='certified_local_batch'


def test_successful_batch_preserves_original_call_order(monkeypatch):
 observed,error=run(monkeypatch,[call(0,'fs_read','{"path":"a.txt"}'),call(1,'fs_list','{"path":"."}')])
 assert error is None and observed==[('tool_call','fs.read'),('tool_call','fs.list')]

@pytest.mark.parametrize('case,reason',[
 ('duplicate','duplicate_identity'),('missing_id','missing_identity'),('unknown','unknown_tool'),
 ('native','native_tool'),('limit','batch_limit'),('no_terminal','missing_terminal'),('fragment','not_fully_drained')])
def test_uncertified_batch_still_never_forwards_or_retries(monkeypatch,case,reason):
 calls=[call(0),call(1,'fs_list','{"path":"."}')];finish='tool_calls'
 if case=='duplicate':calls[1]['id']=calls[0]['id']
 if case=='missing_id':calls[1]['id']=''
 if case=='unknown':calls[1]['function']['name']='UNKNOWN-TOOL-SENTINEL'
 if case=='native':calls[1]['function']['name']='$web_search'
 if case=='limit':calls=[call(i) for i in range(33)]
 if case=='no_terminal':finish=None
 if case=='fragment':calls[0]['index']=True
 observed,error=run(monkeypatch,calls,finish)
 assert error.code=='model_tool_protocol_failed' and observed==[]
 assert error.details.get('native_tool_correction_eligible',False) is False
 assert error.details['native_tool_diagnostic']['certification_reason']==reason
 assert 'UNKNOWN-TOOL-SENTINEL' not in json.dumps(error.details)


def test_length_termination_still_discards_entire_batch(monkeypatch):
 observed,error=run(monkeypatch,[call(0,'fs_list','{"path":"."}'),call(1)],'length')
 assert error.code=='provider_response_incomplete' and observed==[]
 assert error.details.get('native_tool_correction_eligible',False) is False


def test_empty_inspection_pair_uses_existing_bounded_correction_without_execution(tmp_path,monkeypatch):
 from test_tool_continuation_retry import fixture,install_http,list_reply,start
 requests=install_http(monkeypatch,[
  ({'tool_calls':[call(0,'tools_discover',json.dumps({'names':['document.inspect_template']}))]},'tool_calls'),
  ({'tool_calls':[call(0,identity='empty-first'),call(1,identity='empty-second')]},'tool_calls'),
  (list_reply('corrected-read'),'tool_calls'),({'content':'Corrected complete.'},'stop')],goal_checks=True)
 async def exercise():
  executed=[]
  async def sink(event):
   if event['event_type']=='run.tool_requested':
    operation=event['data']['operation'];executed.append(operation['provider_call_id'])
    service.tool_status(operation['id'],'executing');service.resolve_tool(operation['id'],True,'{"entries":[]}')
  core,_,work,service=await fixture(tmp_path,sink)
  try:
   identity=await start(service,work)
   result=core.get_model_run(identity)
   assert result['status']=='completed',result['error_code']
   assert executed==['corrected-read']
   assert not any(operation['tool_name']=='document.inspect_template' for operation in service.list_tool_operations(identity))
   correction='\n'.join(message.get('content') or '' for message in requests[2][1]['messages'])
   assert 'attachment_id, template_id, or path' in correction and 'omit the other two' in correction
   assert 'empty-first' not in correction and 'empty-second' not in correction
   with core._connect() as db:
    rows=[json.loads(row[0]) for row in db.execute('SELECT d.payload_json FROM invocation_diagnostics d JOIN invocation_specs i ON i.id=d.spec_id WHERE i.plan_id=? AND i.status=?',(result['execution_plan_id'],'failed'))]
   assert rows[0]['native_tool_protocol']['certification_reason']=='certified_local_batch'
   assert rows[0]['native_tool_protocol']['runtime_blocked_reason']=='eligible'
  finally:await service.shutdown()
 asyncio.run(exercise())
