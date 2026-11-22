import ast,asyncio,sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
import pytest
from agent import runtime
from agent.execution.compiler import InvocationCompiler
from agent.local_provider import ManagedLocalProvider
from agent.model_invocation import ModelInvocationService
from agent.providers import ProviderProfile
from agent.storage import CoreDataError
from agent.context_engine.policy import resolve_budget
from test_unified_invocation_v3 import model_spec
@pytest.mark.parametrize('mode',['small_jitter','unstable','no_capacity'])
def test_final_measured_budget_converges_conservatively_or_retains_scalar_trace(monkeypatch,mode):
 window=16128;_,_,reserve,total=resolve_budget(context_window=window,max_output_tokens=512);cap=total+reserve
 measured=cap-64-1000
 values=[measured,measured+1,measured+2] if mode=='small_jitter' else [measured,measured+200,measured+400] if mode=='unstable' else [cap-64-100]
 calls=[];messages=[{'role':'system','content':'budget'},{'role':'user','content':'Preserve original user request'}]
 async def measure(request):calls.append(request);return values[min(len(calls)-1,len(values)-1)]
 @asynccontextmanager
 async def session(_):yield SimpleNamespace(measure_request=measure)
 monkeypatch.setattr(ModelInvocationService,'provider_session',session)
 profile=ProviderProfile('managed:model','P','openai_compatible','http://127.0.0.1:1/v1',primary_protocol='openai_chat',model_capabilities={'model':{'context_length':window}})
 provider=ManagedLocalProvider(profile,None,None)
 service=SimpleNamespace(invocation_compiler=InvocationCompiler())
 spec=model_spec(protocol_id='openai_chat',adapter_id='openai.chat',max_output_tokens=2048)
 def refresh(current):messages[0]={'role':'system','content':'Current output cap '+str(current.max_output_tokens)}
 async def exercise():
  if mode=='small_jitter':
   final,prepared=await runtime.ChatRunService._prepare_measured_model_request(service,provider,spec,messages,requested_output_tokens=2048,refresh_snapshot=refresh)
   assert len(calls)==2 and 512<=final.max_output_tokens<=1000-64
   assert prepared.input_budget['remaining_input_tokens']>=0
  else:
   with pytest.raises(CoreDataError) as caught:await runtime.ChatRunService._prepare_measured_model_request(service,provider,spec,messages,requested_output_tokens=2048,refresh_snapshot=refresh)
   assert caught.value.code=='provider_context_limit_exceeded'
   trace=caught.value.details['measurement_attempts'];assert len(trace)==len(calls)
   assert all(set(v)<={'pass','measured_input_tokens','available_output_tokens','current_output_tokens','next_output_tokens'} for v in trace)
  assert messages[-1]['content']=='Preserve original user request'
 asyncio.run(exercise())
