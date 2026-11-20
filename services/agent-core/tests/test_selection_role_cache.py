import asyncio,copy,json
import pytest
from test_task_plans import service
from agent.execution.source_inventory import SourceInventory
from agent.storage import CoreDataError
from agent.execution import document_review_selection as new

REQUEST='审阅已填写的正式内容，跳过空白、占位和示例。'


def fields():
 return [{'id':'a','kind':'paragraph','text':'本节主要说明需编写的功能参数。','chapter_path':['A']},
  {'id':'b','kind':'paragraph','text':'设备应模拟装机连接，比如使用试验支架。','chapter_path':['B']},
  {'id':'c','kind':'paragraph','text':'设备必须接地。','chapter_path':['B']},
  {'id':'t-r0-c0','kind':'table_cell','text':'序号','chapter_path':['B']},
  {'id':'t-r1-c0','kind':'table_cell','text':'1','chapter_path':['B']},
  {'id':'t-r1-c1','kind':'table_cell','text':'','chapter_path':['B']}]


def output(codes):return {'decisions':codes,'context_refs':[],'context_notes':'','context_indices':[]}

async def old_complete(tmp_path,base_codes=None,allow_uncertain=False):
 core,monitor,root,plans=service(tmp_path);values=fields();template={'id':'template','sha256':'original-bytes','manifest':{'authorized_source_id':'source'}}
 SourceInventory(core).register(root['id'],'source','template',template['sha256'],values,complete=True)
 goals=plans.capture_requirements(root['id'],{'source_scopes':[{'source_id':'source','operation':'review','scope':'selected','target_ids':[],
  'selector':{'kind':'selection_pending','condition':REQUEST},'reason':REQUEST}], 'deliverables':[],'success_criteria':['Respect user condition']},expected_revision=0)
 with core._connect() as db:db.execute('INSERT INTO task_goal_execution VALUES(?,?,?,?)',(root['id'],goals['revision'],goals['request_digest'],json.dumps({'_first_result_id':'read'})))
 codes=base_codes or {'a':'I:mixed','b':'E:example','c':'I:req','t-r0-c0':'C:label','t-r1-c0':'I:req'}
 async def generate(payload,schema,correction):return output([codes[f['id']] for f in payload['fields']])
 chosen=[field for field in values if field['text'].strip()]
 raw=new.validate_decisions(output([codes[field['id']] for field in chosen]),chosen,set(),source_indices={field['id']:i for i,field in enumerate(values)})
 raw[values[-1]['id']]={'decision':'exclude','reason':'原位置为空白。','context_ids':[],'text_sha256':new.sha('')}
 identity=new._proof_identity(template,'source',REQUEST,values,{},goals['request_digest']);identity.pop('role_review_contract_sha256');identity['version']=3
 state={'identity':identity,'decisions':raw,'batches':[{'target_ids':[field['id'] for field in chosen],
  'decision_sha256':new.sha({field['id']:raw[field['id']] for field in chosen}),'source_sha256':template['sha256'],'corrections':0}],
  'context_notes':'','context_ids':[]}
 selected=[field['id'] for field in values if raw[field['id']]['decision']=='include']
 if not any(v['decision']=='uncertain' for v in raw.values()):
  requirements={key:copy.deepcopy(goals[key]) for key in ('source_scopes','deliverables','success_criteria')}
  requirements['source_scopes'][0].update(scope='selected',target_ids=selected);requirements['source_scopes'][0].pop('selector')
  goals=plans.capture_requirements(root['id'],requirements,expected_revision=goals['revision'],reconciled=True)
  state.update(complete=True,selected_ids=selected)
 with core._connect() as db:
  payload={'_first_result_id':'read','_review_selection':state}
  db.execute('UPDATE task_goal_execution SET payload_json=?,goal_revision=? WHERE task_id=?',(json.dumps(payload),goals['revision'],root['id']))
 with core._connect() as db:
  payload=json.loads(db.execute('SELECT payload_json FROM task_goal_execution').fetchone()[0]);payload['_prepared_review']={'artifact_id':'old-report'}
  db.execute('UPDATE task_goal_execution SET payload_json=?',(json.dumps(payload),))
 return core,monitor,root,plans,template,values


def test_trusted_complete_v3_only_rechecks_risks_and_updates_derived_selected_scope(tmp_path):
 async def exercise():
  core,_,root,plans,template,values=await old_complete(tmp_path);seen=[]
  async def generate(payload,schema,correction):
   assert payload['selection_phase']=='source_role_review','Never repeat the trusted whole-source base selection'
   seen.extend(f['id'] for f in payload['fields'])
   answers={'a':'C:guidance','b':'I:mixed','t-r1-c0':'C:label'}
   return output([answers[f['id']] for f in payload['fields']])
  result=await new.resolve_review_selection(core,root['id'],template,REQUEST,values,generate=generate,fits=lambda *_:True,
   fence=(root['revision'],root['attempt']),model_identity={})
  assert set(seen)=={'a','b','t-r1-c0'} and result==['b','c']
  assert plans.goals.get(root['id'])['source_scopes'][0]['target_ids']==result
  with core._connect() as db:payload=json.loads(db.execute('SELECT payload_json FROM task_goal_execution').fetchone()[0])
  state=payload['_review_selection'];assert state['identity']['version']==4 and state['complete']
  assert state['base_decisions']['a']['basis_code']=='I:mixed' and state['decisions']['a']['basis_code']=='C:guidance'
  assert len(state['source_role_reviews'])==3 and '_prepared_review' not in payload
  async def never(*_):raise AssertionError('A current recheck proof must be reusable')
  assert await new.resolve_review_selection(core,root['id'],template,REQUEST,values,generate=never,fits=lambda *_:True,
   fence=(root['revision'],root['attempt']),model_identity={})==result
 asyncio.run(exercise())


def test_source_cues_never_mechanically_override_model_request_decision(tmp_path):
 async def exercise():
  core,_,root,_,template,values=await old_complete(tmp_path)
  async def generate(payload,schema,correction):return output(['I:req']*len(payload['fields']))
  result=await new.resolve_review_selection(core,root['id'],template,REQUEST,values,generate=generate,fits=lambda *_:True,
   fence=(root['revision'],root['attempt']),model_identity={})
  assert set(result)=={'a','b','c','t-r1-c0'},'The source-cue detector may request review, never choose a code itself'
 asyncio.run(exercise())


def test_legacy_batch_digest_corruption_never_reuses_unproved_decisions(tmp_path):
 async def exercise():
  core,_,root,_,template,values=await old_complete(tmp_path)
  with core._connect() as db:
   payload=json.loads(db.execute('SELECT payload_json FROM task_goal_execution').fetchone()[0]);payload['_review_selection']['batches'][0]['decision_sha256']='corrupt'
   db.execute('UPDATE task_goal_execution SET payload_json=?',(json.dumps(payload),))
  phases=[]
  async def generate(payload,schema,correction):phases.append(payload.get('selection_phase','base'));return output(['C:guidance']*len(payload['fields']))
  assert await new.resolve_review_selection(core,root['id'],template,REQUEST,values,generate=generate,fits=lambda *_:True,
   fence=(root['revision'],root['attempt']),model_identity={})==[]
  assert 'base' in phases
 asyncio.run(exercise())


def test_remaining_uncertainty_is_source_bound_gap_not_discarded_or_counted_as_reviewed(tmp_path):
 async def exercise():
  core,_,root,plans,template,values=await old_complete(tmp_path);gaps=[]
  async def generate(payload,schema,correction):
   assert payload['selection_phase']=='source_role_review'
   codes={'a':'C:guidance','b':'U:ambiguous','t-r1-c0':'C:label'}
   return output([codes[f['id']] for f in payload['fields']])
  result=await new.resolve_review_selection(core,root['id'],template,REQUEST,values,generate=generate,fits=lambda *_:True,
   fence=(root['revision'],root['attempt']),model_identity={},scope_gaps=gaps)
  assert result==['c'] and [g['target_id'] for g in gaps]==['b']
  assert gaps[0]['text_sha256']==new.sha(values[1]['text'])
  with core._connect() as db:state=json.loads(db.execute('SELECT payload_json FROM task_goal_execution').fetchone()[0])['_review_selection']
  assert state['classification_complete'] and state['complete'] and not state['scope_resolved'] and state['has_scope_gaps']
  assert state['scope_uncertainties']==gaps and plans.goals.get(root['id'])['source_scopes'][0]['target_ids']==['c']
  async def never(*_):raise AssertionError('Gap-inclusive current proofs are reusable without inference')
  again=[{'stale':'value'}]
  assert await new.resolve_review_selection(core,root['id'],template,REQUEST,values,generate=never,fits=lambda *_:True,
   fence=(root['revision'],root['attempt']),model_identity={},scope_gaps=again)==['c']
  assert again==gaps
 asyncio.run(exercise())


def test_all_uncertain_scope_never_defaults_empty_targets_to_full(tmp_path):
 async def exercise():
  codes={f['id']:'U:ambiguous' for f in fields() if f['text']}
  core,_,root,plans,template,values=await old_complete(tmp_path,base_codes=codes,allow_uncertain=True);gaps=[]
  async def generate(payload,schema,correction):
   assert payload['selection_phase']=='source_role_review';return output(['U:ambiguous']*len(payload['fields']))
  assert await new.resolve_review_selection(core,root['id'],template,REQUEST,values,generate=generate,
   fits=lambda *_:True,fence=(root['revision'],root['attempt']),model_identity={},scope_gaps=gaps)==[]
  assert len(gaps)==5
  assert plans.goals.get(root['id'])['source_scopes'][0]['scope']=='metadata'
  with core._connect() as db:state=json.loads(db.execute('SELECT payload_json FROM task_goal_execution').fetchone()[0])['_review_selection']
  assert state['classification_complete'] and state['complete'] and not state['scope_resolved'] and state['selected_ids']==[]
 asyncio.run(exercise())
