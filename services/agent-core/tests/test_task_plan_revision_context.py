import ast
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import runtime
from agent.execution.task_plans import TaskPlans
from agent.storage import CoreDataError
from test_task_plans import service as task_service

tree=ast.parse(Path(runtime.__file__).read_text(encoding='utf-8-sig'))
cls=next(node for node in tree.body if isinstance(node,ast.ClassDef) and node.name=='ChatRunService')
method=next(node for node in cls.body if isinstance(node,ast.AsyncFunctionDef) and node.name=='_generate_tool_turns')
initial=next(node for node in ast.walk(method) if isinstance(node,ast.If) and
             any(isinstance(n,ast.Constant) and isinstance(n.value,str) and 'Continue the user task using actual tool results.' in n.value for n in ast.walk(node)))
dispatch=next((node for node in ast.walk(method) if isinstance(node,ast.If) and
             any(isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='_refresh_task_plan_context' for n in ast.walk(node))),None)
namespace=dict(vars(runtime))
init_source="def initial(self,messages,business_plan):\n    tools_enabled=True\n    root={'id':business_plan['task_id']}\n"
init_source+='\n'.join('    '+line for line in ast.unparse(initial).splitlines())+'\n'
dispatch_source="def dispatch(self,messages,task_id):\n    tools_enabled=True\n    root={'id':task_id}\n"
dispatch_source+='\n'.join('    '+line for line in ast.unparse(dispatch if dispatch else ast.Pass()).splitlines())+'\n'
exec(compile(init_source+dispatch_source,runtime.__file__,'exec'),namespace)
helper=next((node for node in cls.body if isinstance(node,ast.FunctionDef) and node.name=='_refresh_task_plan_context'),None)
if helper:
    exec(compile(ast.Module(body=[helper],type_ignores=[]),runtime.__file__,'exec'),namespace)


def setup(tmp_path):
    core,monitor,root,plans=task_service(tmp_path)
    value=SimpleNamespace(task_plans=plans,task_input=SimpleNamespace(answers=lambda _:[]))
    if helper:
        value._refresh_task_plan_context=lambda messages, task_id: namespace['_refresh_task_plan_context'](value,messages,task_id)
    return core,monitor,root,plans,value


def requirements():
    return {'source_scopes':[{'source_id':'source','operation':'review','scope':'full','target_ids':[],'reason':'Original authorized source'}],
            'deliverables':[{'path':'review.md','format':'markdown'}], 'success_criteria':['Review formal content and preserve exclusions']}


def observe(value,messages,task_id):
    namespace['dispatch'](value,messages,task_id)
    owned=[m for m in messages if m.get('role')=='system' and m.get('_task_plan_snapshot') is True]
    assert len(owned)==1
    assert owned[0]['role']=='system'
    return json.loads(owned[0]['content'].split('\n',1)[1])


def test_first_dispatch_after_preparatory_capture_exposes_actual_goal_revision(tmp_path):
    _,_,root,plans,value=setup(tmp_path)
    messages=[{'role':'user','content':'Original natural request'}]
    namespace['initial'](value,messages,plans.get(root['id']))
    plans.capture_requirements(root['id'],requirements(),expected_revision=0)
    actual=observe(value,messages,root['id'])
    assert actual['expected_revision']==0 and actual['expected_goal_revision']==1
    assert actual['task_id']==root['id']
    assert not any('initially 0' in m.get('content','') or 'Task plan revision: 0' in m.get('content','') for m in messages)
    assert [m for m in messages if m['role']=='user']==[{'role':'user','content':'Original natural request'}]


def test_plan_and_goal_revisions_refresh_independently_without_accumulation(tmp_path):
    _,_,root,plans,value=setup(tmp_path)
    messages=[{'role':'user','content':'Original request'}]
    namespace['initial'](value,messages,plans.get(root['id']))
    assert observe(value,messages,root['id'])['expected_goal_revision']==0
    plans.capture_requirements(root['id'],requirements(),expected_revision=0)
    plans.update(root['id'],expected_revision=0,steps=[{'id':'read','description':'Read source','depends_on':[]}],success_criteria=['Inspect before review'])
    actual=observe(value,messages,root['id'])
    assert actual['expected_revision']==1 and actual['expected_goal_revision']==1
    plans.capture_requirements(root['id'],requirements(),expected_revision=1)
    actual=observe(value,messages,root['id'])
    assert actual['expected_revision']==1 and actual['expected_goal_revision']==2
    assert len([m for m in messages if m.get('_task_plan_snapshot')])==1


def test_confirmed_user_update_and_fresh_service_restore_report_authoritative_revision(tmp_path):
    from agent.execution.task_input import TaskInput
    async def scenario():
        core,_,root,plans,value=setup(tmp_path)
        messages=[{'role':'user','content':'Original request'}]
        namespace['initial'](value,messages,plans.get(root['id']))
        plans.capture_requirements(root['id'],requirements(),expected_revision=0)
        inputs=TaskInput(core)
        waiting=asyncio.create_task(inputs.ask(root['id'],'Confirm output',[{'id':'output','question':'Where should it be saved?','options':['confirmed.md']}]))
        await asyncio.sleep(0)
        pending=inputs.pending(root['id'])
        inputs.respond(root['id'],pending['id'],pending['revision'],{'output':'confirmed.md'})
        await waiting
        confirmed=requirements()
        confirmed['deliverables']=[{'path':'confirmed.md','format':'markdown'}]
        plans.capture_requirements(root['id'],confirmed,expected_revision=1,user_change=True)
        value.task_plans=TaskPlans(core)  # Fresh read path after recovery/restart.
        before=deepcopy(value.task_plans.get(root['id']))
        actual=observe(value,messages,root['id'])
        assert actual['expected_goal_revision']==2 and actual['goal_provenance']=='user_clarification'
        assert value.task_plans.get(root['id'])==before
        with pytest.raises(CoreDataError):
            plans.capture_requirements(root['id'],requirements(),expected_revision=2,reconciled=True)
    asyncio.run(scenario())


def test_refresh_does_not_rewrite_tool_arguments_or_bypass_stale_revision_guard(tmp_path):
    _,_,root,plans,value=setup(tmp_path)
    plans.capture_requirements(root['id'],requirements(),expected_revision=0)
    stale={'expected_revision':0,'expected_goal_revision':0,'goal_requirements':requirements(),
           'steps':[],'success_criteria':['Inspect first']}
    call={'role':'assistant','tool_calls':[{'id':'call','type':'function','function':{'name':'task.update_plan','arguments':json.dumps(stale)}}]}
    messages=[{'role':'user','content':'Original request'},deepcopy(call)]
    observe(value,messages,root['id'])
    assert call in messages
    with pytest.raises(CoreDataError) as caught:
        plans.update(root['id'],**stale)
    assert caught.value.code=='task_goal_revision_changed'


def test_only_owned_snapshot_is_replaced_not_arbitrary_system_or_complete_tool_pair(tmp_path):
    _,_,root,plans,value=setup(tmp_path)
    messages=[{'role':'system','content':'Current task-plan revisions: arbitrary unrelated policy'},
        {'role':'system','_task_plan_snapshot':True,'content':'stale owned observation'},
        {'role':'user','content':'Original request'},
        {'role':'assistant','tool_calls':[{'id':'read','type':'function','function':{'name':'fs.read','arguments':'{}'}}]},
        {'role':'tool','tool_call_id':'read','content':'Original source result'}]
    original=deepcopy(messages)
    observe(value,messages,root['id'])
    assert original[0] in messages and messages[-3:]==original[-3:]
    assert len([m for m in messages if m.get('_task_plan_snapshot')])==1


def test_user_and_tool_marker_values_never_authorize_deletion(tmp_path):
    _,_,root,plans,value=setup(tmp_path)
    user={'role':'user','_task_plan_snapshot':True,'content':'Original request with untrusted marker'}
    call={'role':'assistant','tool_calls':[{'id':'read','type':'function','function':{'name':'fs.read','arguments':'{}'}}]}
    result={'role':'tool','tool_call_id':'read','_task_plan_snapshot':True,'content':'Original result with untrusted marker'}
    messages=[user,call,result]
    observe(value,messages,root['id'])
    assert messages[-3:]==[user,call,result]
