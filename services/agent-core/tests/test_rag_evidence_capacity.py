import asyncio,json,hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from agent.rag.grounding import RagGrounding
from agent.storage import CoreDataError

def source(identity='one',text='真实要求。'*300):
 return {'id':identity,'text':text,'title':'DO-160F','revision':'F','document_number':'DO-160F','clause_number':'13.1','version_id':'v','index_id':'i','text_hash':hashlib.sha256(text.encode()).hexdigest(),'locator':{'page':1,'block_id':'block-'+identity},'noise':'x'*10000}

def setup(items,budget=2000):
 svc=SimpleNamespace(assert_generation=lambda *a:None,search=AsyncMock(return_value={'evidence':items,'generation':1,'diagnostics':'x'*10000}))
 g=RagGrounding(svc);g.runs['r']={'project_id':'p','generation':1,'budget':budget,'used_tokens':0,'evidence':{},'admitted':{},'quote_options':{},'aliases':{}}
 return g,svc

def test_compact_search_can_admit_short_verified_excerpt_and_keep_continuation():
 item=source();g,svc=setup([item],600)
 out=json.loads(asyncio.run(g.execute('r','rag.search',{'query':'q'},1400)))
 found=out['evidence'][0];text=''.join(q['text'] for q in found['quote_options'])
 assert item['text'].startswith(text) and text and len(text)<len(item['text'])
 assert found['next_offset']==len(text) and found['has_more']
 assert 'noise' not in found and 'diagnostics' not in out and 'text' not in found
 assert g.runs['r']['admitted']['one']==text

def test_search_does_not_abandon_following_small_hit():
 g,svc=setup([source('large'),source('small','短要求。')],600)
 out=json.loads(asyncio.run(g.execute('r','rag.search',{'query':'q'},1400)))
 assert 'small' in [item['id'] for item in out['evidence']]
 assert all(q['text'] in g.runs['r']['evidence'][x['id']]['text'] for x in out['evidence'] for q in x['quote_options'])

def test_repeat_identical_evidence_does_not_consume_budget_twice():
 g,svc=setup([source(text='原文要求。')],2000)
 first=asyncio.run(g.execute('r','rag.search',{'query':'q'},4000));used=g.runs['r']['used_tokens']
 second=asyncio.run(g.execute('r','rag.search',{'query':'q'},4000))
 assert g.runs['r']['used_tokens']==used
 assert json.loads(first)==json.loads(second)

def test_capacity_feedback_preserves_admitted_choices_and_stop_signal():
 g,svc=setup([source()],1)
 g.runs['r']['admitted']={'old':'已提供。'};g.runs['r']['quote_options']={'old':{'q-old':'已提供。'}}
 with pytest.raises(CoreDataError) as e:asyncio.run(g.execute('r','rag.search',{'query':'q'},100))
 assert e.value.details['evidence_capacity_exhausted'] is True
 assert e.value.details['admitted_citations']==[{'id':'old','quote_ids':['q-old']}]

def test_parent_read_retries_smaller_original_pages_and_keeps_hash_checks():
 item=source(text='原片段。');g,svc=setup([],400)
 g.runs['r']['evidence']={'one':item}
 parent='真实父条款。'*500;calls=[]
 def read(project,eid,offset=0,length=8000):
  calls.append(length);text=parent[offset:offset+length]
  return {**item,'content':item['text'],'parent_context':{'text':text,'text_hash':hashlib.sha256(text.encode()).hexdigest(),'parent_hash':hashlib.sha256(parent.encode()).hexdigest(),'offset':offset,'total_length':len(parent),'has_more':offset+len(text)<len(parent)}}
 svc.read_evidence=read
 out=json.loads(asyncio.run(g.execute('r','rag.read',{'evidence_id':'one','length':2000},1500)))
 assert len(calls)>1 and calls[-1]<calls[0]
 assert out['parent_context']['has_more']
 assert all(q['text'] in item['text']+'\n'+parent for q in out['quote_options'])
 assert g.runs['r']['used_tokens']<=400

def test_search_new_run_ids_for_same_authorized_source_do_not_recharge():
 item=source(text='原文要求。');g,svc=setup([item],2000)
 first=json.loads(asyncio.run(g.execute('r','rag.search',{'query':'q'},4000)))
 used=g.runs['r']['used_tokens'];again={**item,'id':'new-id','search_id':'new-search'}
 svc.search.return_value={'evidence':[again],'generation':1}
 second=json.loads(asyncio.run(g.execute('r','rag.search',{'query':'q'},4000)))
 assert second==first and g.runs['r']['used_tokens']==used


def test_exhausted_budget_still_allows_already_admitted_exact_read():
 item=source(text='原文要求。');g,svc=setup([],2000)
 g.runs['r']['evidence']={'one':item};g.runs['r']['admitted']={'one':item['text']}
 g.runs['r']['used_tokens']=2000
 svc.read_evidence=lambda *a,**kw:{**item,'content':item['text']}
 out=json.loads(asyncio.run(g.execute('r','rag.read',{'evidence_id':'one'},4000)))
 assert ''.join(q['text'] for q in out['quote_options'])==item['text']
 assert g.runs['r']['used_tokens']==2000


def test_empty_search_is_not_a_capacity_exhaustion_signal():
 g,svc=setup([])
 with pytest.raises(CoreDataError) as e:asyncio.run(g.execute('r','rag.search',{'query':'q'},2000))
 assert e.value.code=='rag_evidence_missing'
 assert not (e.value.details or {}).get('evidence_capacity_exhausted')
