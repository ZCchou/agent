import asyncio
from agent.execution import document_review_selection as selection
from test_document_review_selection import setup,CONDITION,reply

def run(core,root,template,fields,generate,gaps,**kwargs):
 return asyncio.run(selection.resolve_review_selection(core,root['id'],template,CONDITION,fields,generate=generate,
  fits=kwargs.get('fits',lambda p,s:len(p['fields'])<=2),fence=(root['revision'],root['attempt']),model_identity={},scope_gaps=gaps))

def test_bad_position_keeps_exact_valid_peers_and_continues_remaining_batches_once(tmp_path):
 core,_,root,_,template,fields=setup(tmp_path);calls=[];gaps=[]
 async def generate(payload,schema,correction):
  calls.append([f['id'] for f in payload['fields']]);value=reply(payload)
  if 'p-0' in calls[-1]:value['decisions'][calls[-1].index('p-0')]='INVALID'
  return value
 result=run(core,root,template,fields,generate,gaps)
 assert result==['p-1','p-3'] and gaps[0]['target_id']=='p-0'
 assert sum('p-0' in batch for batch in calls)==1 and any('p-3' in batch for batch in calls)

def test_single_oversized_source_is_explicit_gap_and_does_not_stop_others(tmp_path):
 core,_,root,_,template,fields=setup(tmp_path);gaps=[]
 async def generate(payload,*_):return reply(payload)
 result=run(core,root,template,fields,generate,gaps,fits=lambda p,s:len(p['fields'])<=2 and all(f['id']!='p-0' for f in p['fields']))
 assert result==['p-1','p-3'] and gaps[0]['target_id']=='p-0'

def test_no_includes_returns_explicit_empty_coverage_never_full(tmp_path):
 core,_,root,plans,template,fields=setup(tmp_path);gaps=[]
 async def generate(payload,*_):return reply(payload,'uncertain')
 assert run(core,root,template,fields,generate,gaps)==[]
 assert gaps and plans.goals.get(root['id'])['source_scopes'][0]['scope']=='metadata'


def test_continuous_example_inclusion_rechecks_real_anchor_without_overriding_model():
 from agent.execution.selection_roles import source_role_observations,role_review_reasons
 fields=[{'id':'intro','kind':'paragraph','text':'示例：某设备的试验计划。','chapter_path':['A']},
  {'id':'continuation','kind':'paragraph','text':'同一示例的后续说明。','chapter_path':['A'],
   'selection_context':{'example_context':{'anchor_id':'intro','relation':'candidate_continuation'}}}]
 facts=source_role_observations(fields)
 assert facts['continuation']['whole_example_anchor_ids']==['intro']
 assert 'source_example_include' in role_review_reasons(fields[1],{'decision':'include','basis_code':'I:req'},facts['continuation'])


def test_empty_selection_report_is_explicit_zero_review_not_full(tmp_path):
 import json
 core,_,root,_,template,fields=setup(tmp_path);gaps=[]
 async def generate(payload,*_):return reply(payload,'uncertain')
 assert run(core,root,template,fields,generate,gaps)==[]
 with core._connect() as db:state=json.loads(db.execute('SELECT payload_json FROM task_goal_execution').fetchone()[0])['_review_selection']
 result=selection.empty_selection_report(template,fields,state)
 assert result['report']['coverage']['total']==0 and result['report']['selection_coverage']['total']==len(fields)
 assert '不是符合性结论' in result['markdown']
