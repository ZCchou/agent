import asyncio,importlib.util,json,sys
from pathlib import Path
import pytest
from agent.storage import CoreDataError
from test_document_review_selection import setup,CONDITION
from agent.execution import document_review_selection as selection

def compact_reply(payload):
 return {'decisions':['I:req' if f['text'].startswith('Formal') else 'E:example' if f['text'].startswith('Example') else 'C:heading' for f in payload['fields']],
  'context_refs':[],'context_notes':'','context_indices':[]}

@pytest.mark.parametrize('case,issue',[('missing','coverage'),('extra','coverage'),('foreign_code','code'),('duplicate_ref','duplicate_context_ref'),('context','context'),('shape','shape')])
def test_compact_contract_rejects_exact_structural_defect(case,issue):
 fields=[{'id':'p-a','text':'A'},{'id':'p-b','text':'B'}];indexes={'p-a':4,'p-b':8,'ctx':2}
 value={'decisions':['I:req','I:req'],'context_refs':[],'context_notes':'','context_indices':[]}
 if case=='missing':value['decisions'].pop()
 if case=='extra':value['decisions'].append('I:req')
 if case=='foreign_code':value['decisions'][1]='UNTRUSTED-CODE-SENTINEL'
 if case=='duplicate_ref':value['context_refs']=[{'index':4,'anchors':[2]},{'index':4,'anchors':[2]}]
 if case=='context':value['context_indices']=[99]
 if case=='shape':value['context_notes']='x'*161
 with pytest.raises(CoreDataError) as caught:selection.validate_decisions(value,fields,{'ctx'},source_indices=indexes)
 assert caught.value.code=='document_selection_invalid'
 assert caught.value.details['selection_issue']==issue
 assert caught.value.details['expected_indices']==[4,8]
 assert '99' not in json.dumps(caught.value.details) and 'UNTRUSTED' not in json.dumps(caught.value.details)


def test_compact_selector_maps_authoritative_ids_and_reuses_complete_proof(tmp_path):
 core,_,root,plans,template,fields=setup(tmp_path)
 seen=[]
 async def generate(payload,schema,correction):seen.append(payload);return compact_reply(payload)
 result=asyncio.run(selection.resolve_review_selection(core,root['id'],template,CONDITION,fields,generate=generate,
   fits=lambda payload,schema:len(payload['fields'])<=2,fence=(root['revision'],root['attempt']),model_identity={}))
 assert result==['p-1','p-3'] and len(seen)==3
 assert plans.get(root['id'])['goal_requirements']['source_scopes'][0]['target_ids']==result
 with core._connect() as db:state=json.loads(db.execute('SELECT payload_json FROM task_goal_execution').fetchone()[0])['_review_selection']
 assert state['complete'] and set(state['decisions'])=={f['id'] for f in fields}
 assert state['identity']['version']==4
 assert state['identity']['role_review_contract_sha256']==selection.role_review_contract()
 assert state['decisions']['p-1']['context_ids']==[]


def test_feedback_preserves_exact_missing_duplicate_and_failure_metadata(tmp_path):
 core,_,root,_,template,fields=setup(tmp_path);seen=[]
 async def generate(payload,schema,correction):
  seen.append(payload)
  reply=compact_reply(payload)
  if correction==0:reply['decisions'].pop()
  else:
   feedback=payload['correction'];assert feedback['selection_issue']=='coverage'
   assert feedback['missing_count']==1 and feedback['expected_count']==len(payload['fields'])
  return reply
 result=asyncio.run(selection.resolve_review_selection(core,root['id'],template,CONDITION,fields,generate=generate,
   fits=lambda *_:True,fence=(root['revision'],root['attempt']),model_identity={}))
 assert result==[] and len(seen)==1
 assert 'correction' not in seen[0]
 with core._connect() as db:state=json.loads(db.execute('SELECT payload_json FROM task_goal_execution').fetchone()[0])['_review_selection']
 assert state['validation_failures'][0]['selection_issue']=='coverage'
 assert state['validation_failure_count']==1


def test_measured_capacity_failure_reduces_batch_without_truncating_original_fields(tmp_path):
 core,_,root,_,template,fields=setup(tmp_path);seen=[]
 async def generate(payload,schema,correction):
  seen.append(payload)
  if len(payload['fields'])>2:raise CoreDataError('provider_context_limit_exceeded','Measured too large')
  return compact_reply(payload)
 result=asyncio.run(selection.resolve_review_selection(core,root['id'],template,CONDITION,fields,generate=generate,
   fits=lambda *_:True,fence=(root['revision'],root['attempt']),model_identity={}))
 assert result==['p-1','p-3'] and any(len(p['fields'])>2 for p in seen)
 original={f['id']:f['text'] for f in fields}
 assert all(f['text']==original[f['id']] for p in seen for f in p['fields'])


def test_far_example_anchor_annotation_rehydrates_real_source_context(tmp_path):
 core,_,root,_,template,fields=setup(tmp_path)
 fields[4]['selection_context']={'example_context':{'anchor_id':'p-0','relation':'candidate_section_context'}}
 seen=[]
 async def generate(payload,schema,correction):
  reply=compact_reply(payload)
  if any(f['id']=='p-3' for f in payload['fields']):
   contexts={f['id']:f for f in payload['source_context_candidates']}
   assert contexts['p-0']['text']==fields[0]['text']
   reply['context_refs']=[{'index':4,'anchors':[0]}];seen.append(True)
  return reply
 result=asyncio.run(selection.resolve_review_selection(core,root['id'],template,CONDITION,fields,generate=generate,
   fits=lambda payload,schema:len(payload['fields'])<=2,fence=(root['revision'],root['attempt']),model_identity={}))
 assert result==['p-1','p-3'] and seen
 with core._connect() as db:state=json.loads(db.execute('SELECT payload_json FROM task_goal_execution').fetchone()[0])['_review_selection']
 assert state['decisions']['p-3']['context_ids']==['p-0']


def test_source_frame_sent_once_and_context_refs_can_point_to_same_batch(tmp_path):
 core,_,root,_,template,fields=setup(tmp_path)
 async def generate(payload,schema,correction):
  supplied=[f['id'] for f in payload['fields']]+[f['id'] for f in payload['source_context_candidates']]
  assert len(supplied)==len(set(supplied))
  result=compact_reply(payload);result['context_refs']=[{'index':1,'anchors':[2]}];result['context_indices']=[2]
  return result
 result=asyncio.run(selection.resolve_review_selection(core,root['id'],template,CONDITION,fields,generate=generate,
  fits=lambda *_:True,fence=(root['revision'],root['attempt']),model_identity={}))
 assert result==['p-1','p-3']
 with core._connect() as db:state=json.loads(db.execute('SELECT payload_json FROM task_goal_execution').fetchone()[0])['_review_selection']
 assert state['decisions']['p-1']['context_ids']==['p-2']
