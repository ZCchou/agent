import ast
import asyncio
from copy import deepcopy
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from agent import runtime
from agent.context_engine.policy import estimate_tokens
from agent.execution.compiler import InvocationCompiler
from agent.execution.domain import InvocationSpec
from agent.execution.results import ResultStore
from agent.storage import CoreDataError
from agent.tool_contracts import ToolContractRegistry

tree = ast.parse(Path(runtime.__file__).read_text(encoding='utf-8-sig'))
cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'ChatRunService')
method = next(node for node in cls.body if isinstance(node, ast.AsyncFunctionDef) and node.name == '_generate_tool_turns')
block = next(node for node in ast.walk(method) if isinstance(node, ast.If)
             and any(isinstance(child, ast.FunctionDef) and child.name == 'measure_working' for child in node.body))
source = '''async def prepare(self, messages, spec, window=16128, max_output_tokens=4032, external_input=False, run_id="offline-run"):
    invocation_round=1
    run={'id':run_id,'model_id':spec.model_id}
    execution_plan=execution_step=provider=None
    attempt_id='offline-attempt'
    attempt_number=1
    response_only=False
'''
source += '\n'.join('    ' + line for statement in block.body for line in ast.unparse(statement).splitlines())
source += '\n    return request_messages,spec,invocation_round\n'
namespace = dict(vars(runtime))
exec(compile(source, runtime.__file__, 'exec'), namespace)


def spec_for_tools(names=('skills.load', 'tools.discover')):
    return InvocationSpec(spec_id='offline', plan_id='plan', step_id='step', round_no=1, attempt_no=1,
        kind='model', provider_or_backend_id='provider', model_id='model', route_id='chat', protocol_id='openai_chat',
        adapter_id='openai.chat', endpoint_role='final_response', route_fingerprint='offline',
        capability_signature=('text','function_tools'), max_output_tokens=4032,
        allowed_tool_names=names, tool_transport='native_function_call')


def service():
    value = SimpleNamespace(invocation_compiler=InvocationCompiler(), summaries=[], saved=[])
    async def summarize(*args, **kwargs):
        value.summaries.append({'roles': [item['role'] for item in args[7]],
                                'source_characters': sum(len(item['content']) for item in args[7])})
        return args[9] + 1
    async def emit(*args):
        pass
    value.user_input_sources=SimpleNamespace(_manifest=lambda _: SimpleNamespace(is_file=lambda:False))
    value._summarize_working_context=summarize
    value._emit=emit
    value._save_working_context=lambda *args, **kwargs: value.saved.append(deepcopy(args[2]))
    return value


def messages_with_durable_input(padding=10000):
    body=json.dumps({'result_id':'skill-original', 'result_sha256':'a'*64,
        'source_ref':{'tool':'skills.load','arguments':{},'range':{}},
        'preview':'original skill source '*padding})
    return [{'role':'system','content':'<local-skills>Keep effective instructions and permissions unchanged.</local-skills>'},
        {'role':'user','content':'Review formal content, exclude examples, and save the report'},
        {'role':'user','_document_read_result':True,'content':'Exact durable source '+('x'*2503)},
        {'role':'assistant','content':None,'tool_calls':[{'id':'skill','type':'function','function':{'name':'skills.load','arguments':'{}'}}]},
        {'role':'tool','name':'skills.load','tool_call_id':'skill','content':body}]


def execute(messages, spec=None):
    value=service()
    request, current, round_no=asyncio.run(namespace['prepare'](value,messages,spec or spec_for_tools()))
    return value,request,current,round_no


def test_durable_only_pressure_uses_zero_summaries_and_bounded_saved_preview():
    messages=messages_with_durable_input()
    original=deepcopy(messages)
    value,request,spec,round_no=execute(messages)
    assert value.summaries == []
    assert round_no == 1
    assert next(m for m in messages if m.get('_document_read_result')) == original[2]
    assert next(m for m in messages if m['role']=='user' and not m.get('_document_read_result')) == original[1]
    assert original[0] in messages
    assert len(json.loads(next(m for m in messages if m['role']=='tool')['content'])['preview']) <= 256
    reference=next(m for m in messages if m.get('_working_results'))['_references'][0]
    assert reference['result_id']=='skill-original' and reference['result_sha256']=='a'*64
    assert InvocationCompiler().compile_model(spec,request,context_window=16128).input_budget['remaining_input_tokens'] >= 0


def test_existing_notes_alone_do_not_create_repeated_summaries():
    messages=messages_with_durable_input()
    notes={'role':'assistant','_working_notes':True,'content':'prior validated derived notes'}
    messages.insert(1,notes)
    value,_,_,_=execute(messages)
    assert value.summaries == []
    assert notes in messages


def test_repeated_discovery_without_new_assistant_work_never_resummarizes_durable_input():
    messages=messages_with_durable_input()
    for index in range(3):
        call='discover-'+str(index)
        messages.extend([{'role':'assistant','content':None,'tool_calls':[{'id':call,'type':'function',
            'function':{'name':'tools.discover','arguments':'{}'}}]},
            {'role':'tool','name':'tools.discover','tool_call_id':call,
             'content':json.dumps({'result_id':call,'result_sha256':str(index)*64,'preview':'catalog '*500})}])
        value,request,spec,round_no=execute(messages)
        assert not value.summaries and round_no==1
        assert len([m for m in messages if m.get('_document_read_result')])==1
        assert messages[-1]['tool_call_id']==call
        InvocationCompiler().compile_model(spec,request,context_window=16128)


def test_new_reclaimable_assistant_still_allows_summary_if_pressure_remains():
    messages=messages_with_durable_input(1)
    messages.insert(3,{'role':'assistant','content':'new derived answer '*6000})
    value,_,_,round_no=execute(messages)
    assert len(value.summaries)==1 and round_no==2


def test_overlarge_irreducible_user_fails_budget_without_summary_or_loop():
    messages=messages_with_durable_input(1)
    messages[1]['content']='User requirement '*20000
    value,request,spec,round_no=execute(messages)
    assert value.summaries==[] and round_no==1
    with pytest.raises(CoreDataError) as caught:
        InvocationCompiler().compile_model(spec,request,context_window=16128)
    assert caught.value.code=='provider_context_limit_exceeded'


def test_pending_tool_call_and_effective_skill_are_preserved():
    messages=messages_with_durable_input()
    pending={'role':'assistant','content':None,'tool_calls':[{'id':'pending','type':'function','function':{'name':'tools.discover','arguments':'{}'}}]}
    messages.append(pending)
    value,_,_,_=execute(messages)
    assert value.summaries==[]
    assert messages[-1]==pending
    assert messages[0]['content']=='<local-skills>Keep effective instructions and permissions unchanged.</local-skills>'


def test_scalar_piece_trace_distinguishes_validation_correction_without_logging_bodies(monkeypatch):
    from agent.execution import working_context
    from test_working_summary_recovery import summary_service
    trace=[]
    validate=working_context.validate_working_summary
    def checked(value, required):
        try:
            result=validate(value,required)
        except CoreDataError as error:
            trace[-1]['validation_code']=error.code
            raise
        trace[-1]['validation_code']='valid'
        return result
    monkeypatch.setattr(working_context,'validate_working_summary',checked)
    async def invoke(*args, **kwargs):
        payload=json.loads(args[5][1]['content'])
        piece=payload['next_history_piece']
        trace.append({'piece_role':piece['role'],'piece_offset':piece['offset'],
            'piece_characters':len(piece['content']),'correction_index':len(trace),
            'round_no':kwargs['round_no'],'output_reserve':kwargs['max_output_tokens'],
            'protected_record_count':sum(len(v) for v in payload['protected_record_ids'].values())})
        result={'notes':'new bounded notes',**payload['protected_record_ids']}
        if len(trace)==1:
            result['source_refs']=['x'*513]
        return json.dumps(result),{},[]
    value=summary_service(invoke)
    messages=[{'role':'user','content':'Retain authoritative original request'},
              {'role':'assistant','content':'Uncovered derived work'}]
    asyncio.run(value._summarize_working_context({'id':'run'}, {}, {}, 'attempt',1,None,messages,
        [{'role':'assistant','content':'Uncovered derived work'}],12000,3))
    assert [event['validation_code'] for event in trace]==['working_context_summary_invalid','valid']
    assert [event['correction_index'] for event in trace]==[0,1]
    assert all(set(event)=={'piece_role','piece_offset','piece_characters','correction_index',
        'round_no','output_reserve','protected_record_count','validation_code'} for event in trace)


def test_real_summary_replacement_keeps_post_summary_compaction_budget_opportunity(monkeypatch):
    from agent.providers import ProviderError
    from test_working_summary_recovery import summary_service
    messages=messages_with_durable_input(1)[:3]
    # An irreducible original keeps pressure high after initial compaction.
    # The successful summary is larger than the short derived text it replaces;
    # retain the existing post-summary opportunity and check the full compiler.
    messages[1]['content']='Original requirement '+('中'*18000)
    for index in range(9):
        call='saved-'+str(index)
        tool='skills.load' if index==8 else 'tools.discover'
        messages.extend([{'role':'assistant','content':None,'tool_calls':[{'id':call,'type':'function',
            'function':{'name':tool,'arguments':'{"name":"local-document-work"}' if index==8 else '{}'}}]},
            {'role':'tool','name':tool,'tool_call_id':call,
             'content':json.dumps({'result_id':call,'result_sha256':str(index)*64,'padding':'Z'*2800})}])
    messages.append({'role':'assistant','content':'New short derived answer'})
    original_users=deepcopy([m for m in messages if m['role']=='user'])
    calls=[]
    async def invoke(*args, **kwargs):
        calls.append(kwargs['max_output_tokens'])
        payload=json.loads(args[5][1]['content'])
        return json.dumps({'notes':'validated longer notes '*15,**payload['protected_record_ids']}),{},[]
    value=summary_service(invoke)
    value.invocation_compiler=InvocationCompiler()
    value.user_input_sources=SimpleNamespace(_manifest=lambda _: SimpleNamespace(is_file=lambda:False))
    value._save_working_context=lambda *args, **kwargs: value.saved.append(deepcopy(args[2]))
    value.task_plans.get=lambda _: {'revision':1,'request_digest':'fixed-digest',
        'source_scopes':[{'source_id':'source-'+str(i),'operation':'read','scope':'full',
                         'target_ids':[],'reason':'Authorized original'} for i in range(2)],
        'success_criteria':['Retain every authorized source and the original task']}
    summarized=[]
    real_summary=value._summarize_working_context
    async def tracked(*args, **kwargs):
        result=await real_summary(*args, **kwargs)
        summarized.append(True)
        return result
    value._summarize_working_context=tracked
    compactions=[]
    original_compact=namespace['compact_tool_history']
    def compact(*args, **kwargs):
        compactions.append(bool(summarized))
        return original_compact(*args, **kwargs)
    monkeypatch.setitem(namespace,'compact_tool_history',compact)
    spec=spec_for_tools(tuple(ToolContractRegistry.names()) if hasattr(ToolContractRegistry,'names') else ('tools.discover','skills.load'))
    request,current,_=asyncio.run(namespace['prepare'](value,messages,spec))
    compiled=InvocationCompiler().compile_model(current,request,context_window=16128)
    assert compiled.input_budget['remaining_input_tokens']>=0
    assert summarized==[True] and True in compactions, (compactions,compiled.input_budget['request_tokens'])
    assert [m for m in messages if m['role']=='user']==original_users
    assert len([m for m in messages if m.get('_working_notes')])==1
    assert calls and all(limit==1024 for limit in calls)
    assert len(next(m for m in messages if m.get('_working_notes'))['content']) > len('New short derived answer')



@pytest.mark.parametrize('external', [False, True])
def test_external_user_pages_keep_summary_before_compaction_ordinary_sources_skip_it(monkeypatch, external):
    messages=messages_with_durable_input()
    messages[2]['_input_read_result']=True
    messages[2].pop('_document_read_result')
    original=deepcopy(messages)
    value=service()
    order=[]
    compact=namespace['compact_tool_history']
    def observed_compact(*args, **kwargs):
        order.append('compact')
        return compact(*args, **kwargs)
    monkeypatch.setitem(namespace,'compact_tool_history',observed_compact)
    async def summary(*args, **kwargs):
        order.append('summary')
        assert messages==original  # No page/body compaction before protected summary.
        assert any(item['content']==original[2]['content'] for item in args[7])
        return args[9]+1
    value._summarize_working_context=summary
    request,current,_=asyncio.run(namespace['prepare'](value,messages,spec_for_tools(),external_input=external))
    assert order==(['summary','compact'] if external else ['compact'])
    assert original[1] in messages and original[2] in messages
    InvocationCompiler().compile_model(current,request,context_window=16128)


def test_external_summary_failure_preserves_original_pages_and_skips_discard(monkeypatch):
    messages=messages_with_durable_input()
    messages[2]['_input_read_result']=True
    messages[2].pop('_document_read_result')
    original=deepcopy(messages)
    value=service()
    order=[]
    compact=namespace['compact_tool_history']
    def observed_compact(*args, **kwargs):
        order.append('compact')
        return compact(*args, **kwargs)
    monkeypatch.setitem(namespace,'compact_tool_history',observed_compact)
    async def fail(*args, **kwargs):
        order.append('summary')
        raise CoreDataError('working_context_summary_invalid','Protected requirements missing')
    value._summarize_working_context=fail
    with pytest.raises(CoreDataError) as caught:
        asyncio.run(namespace['prepare'](value,messages,spec_for_tools(),external_input=True))
    assert caught.value.code=='working_context_summary_invalid'
    assert messages==original and order==['summary'] and not value.saved


def test_external_without_summary_still_runs_existing_post_compactor(monkeypatch):
    messages=[{'role':'user','content':'Original bounded input'}]
    value=service()
    calls=[]
    compact=namespace['compact_tool_history']
    def observed_compact(*args, **kwargs):
        calls.append(True)
        return compact(*args, **kwargs)
    monkeypatch.setitem(namespace,'compact_tool_history',observed_compact)
    asyncio.run(namespace['prepare'](value,messages,spec_for_tools(),external_input=True))
    assert calls==[True] and not value.summaries



def test_native_late_user_input_manifest_preserves_both_pages_before_summary(tmp_path):
    from test_input_sources import source_run
    from test_user_input_runtime import runtime as input_runtime
    from test_working_summary_recovery import summary_service
    from agent.tool_contracts import ToolCall
    marker='LATE_ORIGINAL_REQUIREMENT_MARKER'
    original=marker+('A'*2500)+('B'*2500)
    core,run=source_run(tmp_path,original)
    native=input_runtime(core)
    assert not native.user_input_sources._manifest(run['id']).is_file()
    page1=native._saved_result_tool(run['id'],ToolCall('page1','tools.read_result',
        {'result_id':'user-input','length':2000}),6000)
    assert native.user_input_sources._manifest(run['id']).is_file()
    next_cursor=json.loads(page1)['next_cursor']
    page2=native._saved_result_tool(run['id'],ToolCall('page2','tools.read_result',
        {'result_id':'user-input','length':2000,'cursor':next_cursor}),6000)
    assert marker in page1 and marker not in page2
    messages=[{'role':'system','content':'policy '*2600},
              {'role':'user','content':'Read every original requirement from user-input.'}]
    for index,page in enumerate([page1,page2]):
        call='original-'+str(index)
        messages.extend([{'role':'assistant','content':None,'tool_calls':[{'id':call,'type':'function',
            'function':{'name':'tools.read_result','arguments':json.dumps({'result_id':'user-input'})}}]},
            {'role':'tool','name':'tools.read_result','tool_call_id':call,'content':page}])
    summarized_pieces=[]
    async def invoke(*args,**kwargs):
        payload=json.loads(args[5][1]['content'])
        piece=payload['next_history_piece']['content']
        summarized_pieces.append(piece)
        notes=payload['previous_notes'] or 'Original requirements retained.'
        if marker in piece and marker not in notes:
            notes+=' '+marker
        return json.dumps({'notes':notes,**payload['protected_record_ids']}),{},[]
    value=summary_service(invoke)
    value._save_working_context=lambda *args,**kwargs: value.saved.append(deepcopy(args[2]))
    value.invocation_compiler=InvocationCompiler()
    value.user_input_sources=native.user_input_sources
    # Native read created the manifest after the turn's initial False value.
    request,current,_=asyncio.run(namespace['prepare'](value,messages,
        spec_for_tools(('tools.read_result','tools.discover')),external_input=False,run_id=run['id']))
    assert any(marker in piece for piece in summarized_pieces)
    assert any(m.get('_working_notes') and marker in m['content'] for m in messages)
    assert native.user_input_sources._original(run['id'])==original
    assert len([m for m in messages if m.get('_working_notes')])==1
    InvocationCompiler().compile_model(current,request,context_window=16128)
