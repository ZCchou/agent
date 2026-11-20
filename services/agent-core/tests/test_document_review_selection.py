import asyncio
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import sqlite3

import pytest

from agent.execution.document_goal import example_goal, normalize_goal
from agent.execution.document_goal_runtime import normalize_document_answer_draft
from agent.execution.source_inventory import SourceInventory
from agent.storage import CoreDataError
from test_task_plans import service

from agent.execution import document_review_selection as selection
CONDITION='Review filled formal content in the attachment, skip blank positions, placeholders and examples. Save review.md.'


def fields():
    return [{'id':'p-0','text':'Background heading','field_type':'heading','chapter_path':['Main'],'section_id':'main'},
        {'id':'p-1','text':'Formal operating requirement A','field_type':'content','chapter_path':['Main'],'section_id':'main'},
        {'id':'p-2','text':'Example: sample equipment','field_type':'content','chapter_path':['Main'],'section_id':'main'},
        {'id':'t-0-r0-c0','text':'Example continuation value','field_type':'content','kind':'table_cell','position':{'table':0,'row':0,'column':0},'chapter_path':['Main'],'section_id':'main'},
        {'id':'p-3','text':'Formal operating requirement B','field_type':'content','chapter_path':['Main'],'section_id':'main'},
        {'id':'p-4','text':'','field_type':'placeholder','chapter_path':['Main'],'section_id':'main'}]


def setup(tmp_path, scope='pending', operation='review'):
    core,monitor,root,plans=service(tmp_path)
    original=fields()
    template={'id':'template','sha256':selection.sha(original),'manifest':{'authorized_source_id':'source'}}
    store=SourceInventory(core)
    store.register(root['id'],'source','template',template['sha256'],original,complete=True)
    draft=example_goal('source')
    draft.update(answer_kind='review',evidence_required=True,deliverables=[{'path':'review.md','format':'markdown'}])
    chosen=draft['source_scopes'][0]
    chosen.update(operation=operation,scope='selected' if scope!='full' else 'full',target_ids=[])
    if scope=='pending':chosen['selector']={'kind':'selection_pending','condition':CONDITION}
    elif scope=='selected':chosen['target_ids']=['p-1']
    requirements=normalize_goal(draft,[{'id':'source'}])
    captured=plans.capture_requirements(root['id'],requirements,expected_revision=0)
    with core._connect() as db:
        db.execute('INSERT INTO task_goal_execution VALUES(?,?,?,?)',
            (root['id'],captured['revision'],captured['request_digest'],json.dumps({**draft,'_first_result_id':'original-result'})))
    return core,monitor,root,plans,template,original


def reply(payload,decision_override=None):
    codes={'include':'I:req','exclude':'E:outside','context':'C:guidance','uncertain':'U:ambiguous'}
    decisions=[codes[decision_override] if decision_override else 'I:req' if field['text'].startswith('Formal')
               else 'E:example' if field['text'].startswith('Example') else 'C:heading' for field in payload['fields']]
    indices=[item['source_index'] for item in payload['source_context_candidates'] if item['text'].startswith('Example')][:1]
    return {'decisions':decisions,'context_refs':[],
            'context_notes':'Example context stays source-bound; other sections need their own anchors.', 'context_indices':indices}


def call(core,root,template,original,generate,**kwargs):
    return asyncio.run(selection.resolve_review_selection(core,root['id'],template,CONDITION,original,
        generate=generate,fits=kwargs.pop('fits',lambda payload,schema:len(payload['fields'])<=2),
        fence=(root['revision'],root['attempt']),model_identity={'model':'test'},**kwargs))


@pytest.mark.parametrize('prompt',[CONDITION,'结合资料库，审阅附件已填写的正式内容，跳过空白、占位和示例。',
    '请审阅附件，但排除示例段落。','对模板里面不是空或案例的内容进行一次rag审阅',
    '对模板里已经填写了的内容进行审阅'])
def test_real_model_goal_full_does_not_erase_positive_selection_condition(prompt):
    draft=example_goal('source');draft['answer_kind']='review';draft['source_scopes'][0]['operation']='review'
    normalize_document_answer_draft(prompt,draft)
    assert draft['source_scopes'][0]['scope']=='selected'
    assert draft['source_scopes'][0]['selector']['kind']=='selection_pending'


@pytest.mark.parametrize('prompt',['审阅全文全部非空内容，包括所有示例和占位符。','审阅全文，包括所有示例。','审阅全文，不要跳过示例和占位。',
                                  'Review the whole document. Do not skip examples.'])
def test_unfiltered_or_negated_skip_preserves_full_review(prompt):
    draft=example_goal('source');draft['answer_kind']='review';draft['source_scopes'][0]['operation']='review'
    selection.normalize_review_draft(prompt,draft)
    assert draft['source_scopes'][0]['scope']=='full'


def test_complete_semantic_selection_before_review_keeps_skips_separate_from_review_gaps(tmp_path):
    core,_,root,plans,template,original=setup(tmp_path,scope='full')
    calls=[]
    async def generate(payload,schema,correction):
        calls.append(deepcopy(payload));return reply(payload)
    selected=call(core,root,template,original,generate)
    assert selected==['p-1','p-3'] and len(calls)>1
    assert any(item['kind']=='table_cell' for batch in calls for item in batch['fields'] if 'kind' in item)
    assert calls[1]['previous_derived_context']['notes']
    goal=plans.get(root['id'])['goal_requirements']
    assert goal['source_scopes'][0]['target_ids']==selected
    with core._connect() as db:
        payload=json.loads(db.execute('SELECT payload_json FROM task_goal_execution').fetchone()[0])
        assert db.execute('SELECT count(*) FROM task_review_proofs').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM task_work_items').fetchone()[0]==0
    proof=payload['_review_selection']
    assert proof['complete'] and set(proof['decisions'])=={f['id'] for f in original}
    assert proof['decisions']['p-0']['decision']=='context'
    assert proof['decisions']['p-2']['decision']=='exclude'
    assert proof['decisions']['p-4']['decision']=='exclude'
    assert payload['_first_result_id']=='original-result'
    async def no_more_model(*args):raise AssertionError('Complete matching proof should be reusable')
    before=goal['revision']
    assert call(core,root,template,original,no_more_model,target_ids=['p-3'])==['p-3']
    assert plans.get(root['id'])['goal_requirements']['revision']==before


def test_selected_goal_without_private_complete_proof_cannot_bypass_selection(tmp_path):
    core,_,root,_,template,original=setup(tmp_path,scope='selected')
    calls=[]
    async def generate(payload,schema,correction):calls.append(True);return reply(payload)
    with pytest.raises(CoreDataError) as caught:
        call(core,root,template,original,generate,target_ids=['p-2'])
    assert caught.value.code=='document_review_scope_invalid' and calls
    with core._connect() as db:assert db.execute('SELECT count(*) FROM task_review_proofs').fetchone()[0]==0


def test_interrupted_selection_resumes_only_uncovered_positions(tmp_path):
    core,_,root,_,template,original=setup(tmp_path)
    calls=[]
    async def interrupt(payload,schema,correction):
        calls.append([f['id'] for f in payload['fields']])
        if len(calls)==2:raise RuntimeError('synthetic process interruption')
        return reply(payload)
    with pytest.raises(RuntimeError):call(core,root,template,original,interrupt)
    completed=set(calls[0]);resumed=[]
    async def generate(payload,schema,correction):resumed.extend(f['id'] for f in payload['fields']);return reply(payload)
    assert call(core,root,template,original,generate)==['p-1','p-3']
    assert not completed & set(resumed)


def test_missing_or_duplicate_decisions_are_bounded_and_never_commit_partial_goal(tmp_path):
    core,_,root,plans,template,original=setup(tmp_path)
    calls=[]
    async def incomplete(payload,schema,correction):
        calls.append(correction);value=reply(payload);value['decisions'].pop();return value
    gaps=[]
    assert call(core,root,template,original,incomplete,scope_gaps=gaps)==[]
    assert calls and all(attempt==0 for attempt in calls) and gaps
    assert plans.get(root['id'])['goal_requirements']['source_scopes'][0]['scope']=='metadata'


@pytest.mark.parametrize('decision,code',[('uncertain','document_selection_uncertain'),('exclude','document_selection_empty')])
def test_uncertain_or_empty_selection_never_falls_back_to_full(tmp_path,decision,code):
    core,_,root,plans,template,original=setup(tmp_path)
    async def generate(payload,schema,correction):return reply(payload,decision)
    gaps=[]
    assert call(core,root,template,original,generate,scope_gaps=gaps)==[]
    assert bool(gaps)==(decision=='uncertain')
    assert plans.get(root['id'])['goal_requirements']['source_scopes'][0]['scope']=='metadata'
    with core._connect() as db:assert db.execute('SELECT count(*) FROM task_review_proofs').fetchone()[0]==0


def test_source_hash_change_and_stale_fence_fail_closed(tmp_path):
    core,_,root,plans,template,original=setup(tmp_path)
    async def never(*args):raise AssertionError('Changed source must fail before model')
    with pytest.raises(CoreDataError) as caught:call(core,root,{**template,'sha256':'different'},original,never)
    assert caught.value.code=='task_source_changed'
    async def supersede(payload,schema,correction):
        with core._connect() as db:db.execute('UPDATE execution_wait_tasks SET revision=revision+1 WHERE id=?',(root['id'],))
        return reply(payload)
    with pytest.raises(CoreDataError) as caught:call(core,root,template,original,supersede)
    assert caught.value.code=='task_plan_stale'
    assert plans.get(root['id'])['goal_requirements']['revision']==1


def test_goal_and_cache_final_commit_rollback_together(tmp_path):
    core,_,root,plans,template,original=setup(tmp_path)
    with core._connect() as db:db.execute('''CREATE TRIGGER reject_selection_commit BEFORE UPDATE ON task_goal_execution
        WHEN NEW.goal_revision>OLD.goal_revision BEGIN SELECT RAISE(ABORT,'interrupted commit'); END''')
    async def generate(payload,schema,correction):return reply(payload)
    with pytest.raises(sqlite3.IntegrityError):call(core,root,template,original,generate)
    assert plans.get(root['id'])['goal_requirements']['revision']==1
    with core._connect() as db:
        assert db.execute('SELECT goal_revision FROM task_goal_execution').fetchone()[0]==1
        db.execute('DROP TRIGGER reject_selection_commit')
    async def no_more_model(*args):raise AssertionError('Completed batch proofs survive failed final commit')
    assert call(core,root,template,original,no_more_model)==['p-1','p-3']


def test_unfiltered_full_remains_available_but_read_full_cannot_authorize_review(tmp_path):
    core,_,root,_,template,original=setup(tmp_path,scope='full')
    async def never(*args):raise AssertionError('Unfiltered full review needs no semantic classifier')
    result=asyncio.run(selection.resolve_review_selection(core,root['id'],template,'Review the entire document.',original,
        generate=never,fits=lambda *_:True,fence=(root['revision'],root['attempt']),model_identity={},target_ids=None))
    assert result is None
    with core._connect() as db:
        raw=json.loads(db.execute('SELECT requirements_json FROM task_goal_requirements').fetchone()[0])
        raw['source_scopes'][0]['operation']='read'
        db.execute('UPDATE task_goal_requirements SET requirements_json=?',(json.dumps(raw),))
    with pytest.raises(CoreDataError) as caught:
        asyncio.run(selection.resolve_review_selection(core,root['id'],template,'Review the entire document.',original,
            generate=never,fits=lambda *_:True,fence=(root['revision'],root['attempt']),model_identity={}))
    assert caught.value.code=='document_review_scope_invalid'


def test_user_confirmed_selected_upper_bound_is_not_expanded_by_classifier(tmp_path):
    core,_,root,plans,template,original=setup(tmp_path,scope='selected')
    with core._connect() as db:db.execute("UPDATE task_goal_requirements SET provenance='user_clarification'")
    seen=[]
    async def generate(payload,schema,correction):seen.extend(f['id'] for f in payload['fields']);return reply(payload)
    assert call(core,root,template,original,generate)==['p-1']
    assert set(seen)=={'p-1'}
    assert plans.get(root['id'])['goal_requirements']['source_scopes'][0]['target_ids']==['p-1']


def test_model_goal_private_selection_cache_is_rejected_at_actual_admission():
    draft=example_goal('source');draft['_review_selection']={'complete':True,'selected_ids':['forged']}
    with pytest.raises(CoreDataError) as caught:normalize_goal(draft,[{'id':'source'}])
    assert caught.value.code=='task_goal_invalid'


@pytest.mark.parametrize('prompt',['Read the entire document.','Do not review the document, only extract its title.'])
def test_no_goals_does_not_authorize_legacy_review_of_read_only_input(tmp_path,prompt):
    core,_,root,_,template,original=setup(tmp_path,scope='full')
    with core._connect() as db:db.execute('DELETE FROM task_goal_requirements')
    async def never(*args):raise AssertionError('Read-only must fail before classifier')
    with pytest.raises(CoreDataError) as caught:
        asyncio.run(selection.resolve_review_selection(core,root['id'],template,prompt,original,
            generate=never,fits=lambda *_:True,fence=(root['revision'],root['attempt']),model_identity={}))
    assert caught.value.code=='document_review_scope_invalid'


def test_authoritative_pending_condition_from_later_user_scope_is_executable(tmp_path):
    core,_,root,plans,template,original=setup(tmp_path)
    with core._connect() as db:
        raw=json.loads(db.execute('SELECT requirements_json FROM task_goal_requirements').fetchone()[0])
        raw['source_scopes'][0]['selector']['condition']='Exclude examples; review formal content.'
        db.execute("UPDATE task_goal_requirements SET requirements_json=?,provenance='user_clarification'",(json.dumps(raw),))
    calls=[]
    async def generate(payload,schema,correction):calls.append(payload['request_condition']);return reply(payload)
    result=asyncio.run(selection.resolve_review_selection(core,root['id'],template,'Review the document.',original,
        generate=generate,fits=lambda payload,schema:len(payload['fields'])<=2,
        fence=(root['revision'],root['attempt']),model_identity={}))
    assert result==['p-1','p-3'] and calls
    assert all('Exclude examples' in condition for condition in calls)


@pytest.mark.parametrize("prompt",["请检查附件中的每个现有位置", "不要检查附件，只读取标题。", "请检查附件已填写的正式内容，跳过示例。"])
def test_explicit_check_intent_preserves_legacy_full_and_conditional_guard(tmp_path,prompt):
    core,_,root,_,template,original=setup(tmp_path,scope='full')
    with core._connect() as db:db.execute('DELETE FROM task_goal_requirements')
    async def never(*args):raise AssertionError('No classifier needed for full or rejected read')
    if prompt.startswith('不要'):
        with pytest.raises(CoreDataError) as caught:
            asyncio.run(selection.resolve_review_selection(core,root['id'],template,prompt,original,
                generate=never,fits=lambda *_:True,fence=(root['revision'],root['attempt']),model_identity={}))
        assert caught.value.code=='document_review_scope_invalid'
    elif '跳过' in prompt:
        assert selection.selection_condition(prompt)==prompt
    else:
        assert asyncio.run(selection.resolve_review_selection(core,root['id'],template,prompt,original,
            generate=never,fits=lambda *_:True,fence=(root['revision'],root['attempt']),model_identity={})) is None
