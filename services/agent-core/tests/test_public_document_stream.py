import asyncio
from types import SimpleNamespace
import pytest
from agent.model_invocation import ModelInvocationService
from agent.providers import ProviderEvent
from test_unified_invocation_v3 import model_spec


def test_public_callback_is_immediate_and_ignores_reasoning_and_tool_events():
 async def run():
  observed=[]
  async def receive(text):observed.append(text)
  async def stream():
   yield ProviderEvent('reasoning_delta',text='private reasoning')
   yield ProviderEvent('tool_call')
   yield ProviderEvent('text_delta',text='{"tool_calls":')
   assert observed==['{"tool_calls":'], 'First public text must be delivered before next provider event'
   yield ProviderEvent('text_delta',text='"public example"}')
   yield ProviderEvent('finish',finish_reason='stop')
  result=await ModelInvocationService().complete_prepared(SimpleNamespace(),model_spec(),SimpleNamespace(),stream_factory=stream,on_public_text=receive)
  assert result[0]=='{"tool_calls":"public example"}'
  assert observed==['{"tool_calls":','"public example"}']
 asyncio.run(run())


def test_cancelled_callback_stops_stream_without_replay():
 async def run():
  seen=[];closed=[]
  async def receive(text):seen.append(text);raise asyncio.CancelledError()
  async def stream():
   try:
    yield ProviderEvent('text_delta',text='first')
    yield ProviderEvent('text_delta',text='never-delivered')
   finally:closed.append(True)
  with pytest.raises(asyncio.CancelledError):await ModelInvocationService().complete_prepared(SimpleNamespace(),model_spec(),SimpleNamespace(),stream_factory=stream,on_public_text=receive)
  assert seen==['first'] and closed==[True]
 asyncio.run(run())

@pytest.mark.parametrize('mode',['ordinary','document','cancel'])
def test_runtime_optional_callback_checks_cancel_and_does_not_publish_chat_chunks(mode,monkeypatch):
 from contextlib import nullcontext
 from agent.runtime import ChatRunService
 from agent.providers import ProviderProfile
 async def run():
  state={'status':'generating'};seen=[];events=[]
  service=object.__new__(ChatRunService)
  service.repository=SimpleNamespace(get_model_run=lambda _:state,bind_attempt_invocation_spec=lambda *a:None)
  service.execution_repository=SimpleNamespace(save_invocation_spec=lambda *a:None,mark_invocation_dispatched=lambda *a:None,finish_invocation=lambda *a:None)
  service.local_models=SimpleNamespace(observe_activity=lambda _:nullcontext())
  service.model_invocation=ModelInvocationService()
  profile=ProviderProfile('provider','fake','fake','memory://fake')
  service._validate_context_dispatch_authority=lambda *a,**kw:None
  service._create_model_invocation_spec=lambda *a,**kw:model_spec()
  async def prepare(provider,spec,messages,**kw):return spec,SimpleNamespace(request_fingerprint='fp',input_budget=None)
  service._prepare_measured_model_request=prepare
  async def emit(run_id,kind,data):events.append(kind)
  service._emit=emit
  async def stream(*a):
   yield ProviderEvent('reasoning_delta',text='private')
   yield ProviderEvent('text_delta',text='first')
   if mode!='ordinary':assert seen==['first']
   yield ProviderEvent('text_delta',text='second')
   yield ProviderEvent('finish',finish_reason='stop')
  monkeypatch.setattr(service.model_invocation,'stream_prepared',stream)
  monkeypatch.setattr('agent.runtime.record_invocation_diagnostics',lambda *a,**kw:None)
  async def receive(text):
   seen.append(text)
   if mode=='cancel':state['status']='cancelled'
  options={} if mode=='ordinary' else {'on_public_text':receive}
  call=service._invoke_planned_step({'id':'run'}, {'id':'plan'}, {'id':'step'}, {'id':'attempt','attempt_number':1},SimpleNamespace(profile=profile),[{'role':'user','content':'review'}],max_output_tokens=128,**options)
  if mode=='cancel':
   with pytest.raises(asyncio.CancelledError):await call
   assert seen==['first']
  else:
   result=await call
   assert result[0]=='firstsecond'
   assert seen==([] if mode=='ordinary' else ['first','second'])
  assert 'run.chunk' not in events
 asyncio.run(run())
