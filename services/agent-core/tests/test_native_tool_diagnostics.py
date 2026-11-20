import asyncio
import json
import sqlite3
from types import SimpleNamespace
import httpx
import pytest
from agent.execution.compiler import InvocationCompiler
from agent.execution.invocation_diagnostics import record
from agent.model_invocation import ModelInvocationService
from agent.providers import OpenAICompatibleProvider, ProviderError, ProviderProfile
from test_unified_invocation_v3 import model_spec

SECRET='PRIVATE-SOURCE-AND-ARGUMENT-SENTINEL'

@pytest.mark.parametrize('case,stage,reason',[
 ('json','arguments_json','invalid_json'),('schema','arguments_schema','invalid_schema'),
 ('targets','arguments_schema','invalid_schema'),('unknown','arguments_schema','invalid_schema'),
 ('index','fragment','invalid_index'),('argument_type','fragment','invalid_arguments_type')])
def test_native_protocol_metadata_is_precise_without_values(monkeypatch,case,stage,reason):
    raw={'index':0,'id':'opaque-call','type':'function','function':{'name':'fs_list','arguments':json.dumps({'path':SECRET})}}
    if case=='json':raw['function']['arguments']='{"path":"'+SECRET
    if case=='schema':raw['function']['arguments']=json.dumps({'path':SECRET,SECRET:SECRET})
    if case=='targets':raw['function']={'name':'document_review_targets','arguments':json.dumps({'template_id':SECRET,'target_ids':SECRET,'format':'markdown'})}
    if case=='unknown':raw['function']['name']=SECRET
    if case=='index':raw['index']=True
    if case=='argument_type':raw['function']['arguments']=[SECRET]
    original=httpx.AsyncClient
    def respond(_):
        chunk={'choices':[{'delta':{'tool_calls':[raw]},'finish_reason':'tool_calls'}]}
        return httpx.Response(200,text='data: '+json.dumps(chunk)+'\n\ndata: [DONE]\n\n')
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(respond),**kw))
    spec=model_spec(protocol_id='openai_chat',adapter_id='openai.chat',capability_signature=('function_tools','text'),allowed_tool_names=('fs.list','document.review_targets'),tool_transport='native_function_call')
    prepared=InvocationCompiler().compile_model(spec,[{'role':'user','content':'request'}])
    provider=OpenAICompatibleProvider(ProviderProfile('p','P','openai_compatible','https://synthetic.invalid/v1',primary_protocol='openai_chat'))
    with pytest.raises(ProviderError) as caught:asyncio.run(ModelInvocationService().complete_prepared(provider,spec,prepared))
    assert caught.value.code=='model_tool_protocol_failed'
    d=caught.value.details['native_tool_diagnostic']
    assert d['stage']==stage and d['reason']==reason
    assert SECRET not in json.dumps(d)
    if case=='json':assert type(d['json_error_position']) is int
    if case=='schema':assert d['tool_name']=='fs.list' and d['argument_types']=={'path':'string'} and d['unknown_key_count']==1 and d['validation_rule']=='unsupported_fields'
    if case=='targets':assert d['tool_name']=='document.review_targets' and d['argument_types']['target_ids']=='string' and d['validation_rule']=='document_targets'
    if case=='unknown':assert 'tool_name' not in d and d['unknown_tool'] is True
    if case=='index':assert 'call_index' not in d


def test_protocol_record_allowlists_and_merges_runtime_block_reason(tmp_path):
    path=tmp_path/'audit.db';repo=SimpleNamespace(_connect=lambda:sqlite3.connect(path))
    with repo._connect() as db:db.execute('CREATE TABLE invocation_diagnostics(spec_id TEXT PRIMARY KEY,payload_json TEXT)')
    spec=model_spec()
    error=ProviderError('model_tool_protocol_failed','private',details={
      'native_tool_correction_eligible':False,'native_tool_diagnostic':{
      'stage':'arguments_schema','reason':'invalid_schema','tool_name':'fs.list','call_count':2,'call_index':1,
      'argument_types':{'path':'string',SECRET:'string'},'unknown_key_count':1,'validation_rule':'unsupported_fields',SECRET:SECRET}})
    record(repo,spec,error=error)
    error.details['native_tool_runtime_blocked_reason']='not_certified'
    record(repo,spec,error=error)
    with repo._connect() as db:raw=db.execute('SELECT payload_json FROM invocation_diagnostics').fetchone()[0]
    d=json.loads(raw)['native_tool_protocol']
    assert d['tool_name']=='fs.list' and d['argument_types']=={'path':'string'}
    assert d['correction_eligible'] is False and d['runtime_blocked_reason']=='not_certified'
    assert SECRET not in raw
    error.details={'native_tool_diagnostic':{'stage':SECRET,'reason':SECRET,'tool_name':SECRET,'argument_types':{'path':SECRET}},'native_tool_runtime_blocked_reason':SECRET}
    record(repo,spec,error=error)
    with repo._connect() as db:assert SECRET not in db.execute('SELECT payload_json FROM invocation_diagnostics').fetchone()[0]


def test_deep_arguments_cannot_mask_existing_fragment_error(monkeypatch):
    original=httpx.AsyncClient
    def respond(_):
        chunks=[{'choices':[{'delta':{'tool_calls':[{'index':0,'id':'first','function':{'name':'fs_list','arguments':'['*10000+']'*10000}}]},'finish_reason':None}]},
                {'choices':[{'delta':{'tool_calls':[{'index':0,'id':'conflicting'}]},'finish_reason':'tool_calls'}]}]
        return httpx.Response(200,text=''.join('data: '+json.dumps(item)+'\n\n' for item in chunks)+'data: [DONE]\n\n')
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(respond),**kw))
    spec=model_spec(protocol_id='openai_chat',adapter_id='openai.chat',capability_signature=('function_tools','text'),allowed_tool_names=('fs.list',),tool_transport='native_function_call')
    prepared=InvocationCompiler().compile_model(spec,[{'role':'user','content':'request'}])
    provider=OpenAICompatibleProvider(ProviderProfile('p','P','openai_compatible','https://synthetic.invalid/v1',primary_protocol='openai_chat'))
    with pytest.raises(ProviderError) as caught:asyncio.run(ModelInvocationService().complete_prepared(provider,spec,prepared))
    assert caught.value.code=='model_tool_protocol_failed'
    d=caught.value.details['native_tool_diagnostic']
    assert d['stage']=='fragment' and d['reason']=='conflicting_metadata'
    assert '[[' not in json.dumps(d) and 'conflicting' not in json.dumps(d.get('argument_types',{}))
