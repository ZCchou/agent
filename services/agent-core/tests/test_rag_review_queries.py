import asyncio
from types import SimpleNamespace
import pytest
from agent.rag.review_queries import review_queries,search_review_units
from agent.storage import CoreDataError

def batch(*pairs):return {'chapter_path':[],'fields':[{'id':str(i),'text':text,'chapter_path':path} for i,(path,text) in enumerate(pairs)]}

def test_short_word_filename_supported_by_body_becomes_primary_topic():
 b=batch(([],'按第9.1节进行验证'),([],'降至短时工作低温并稳定2小时'),([],'恢复温度以避免凝露'))
 q=review_queries('审阅附件',b,'短时工作低温试验步骤.docx',1000)
 assert 1<=len(q)<=3 and '短时工作低温' in q[0]
 assert all('第9.1节' not in x for x in q)

def test_local_chapters_override_global_filename_and_each_other():
 b=batch((['低温试验'],'低温保持'),(['振动试验'],'振动频率'),(['电源试验'],'供电中断'))
 q=review_queries('审阅附件',b,'低温试验.docx',1000)
 assert len(q)==3
 assert '低温' in q[0] and '低温' not in q[1] and '低温' not in q[2]
 assert '振动' in q[1] and '电源' in q[2]

def test_mislabeled_filename_does_not_override_body():
 q=review_queries('审阅',batch(([],'湿热试验相对湿度95%')),'低温试验.docx',1000)
 assert '低温' not in q[0]

def test_queries_are_bounded_unique_and_keep_local_phase():
 b=batch(*[(['温度试验'],t) for t in ['低温保持稳定','恢复温度避免凝露','工作性能记录','其他要求']])
 q=review_queries('审阅',b,'温度试验.docx',90)
 assert len(q)<=3 and len(set(q))==len(q) and all(len(x)<=90 for x in q)
 assert any('恢复' in x for x in q)

@pytest.mark.parametrize('change',['generation','project'])
def test_unit_search_never_softens_project_or_generation_failures(change):
 async def run():
  async def search(query):
   if change=='project':raise CoreDataError('rag_evidence_invalid','Denied')
   return {'evidence':[],'generation':9}
  with pytest.raises(CoreDataError):await search_review_units(['q'],search,generation=2)
 asyncio.run(run())

def test_multiple_queries_round_robin_dedup_by_real_source_identity():
 async def run():
  def item(i,text):return {'id':i,'index_id':'idx','version_id':'v','locator':{'block_id':text},'text':text}
  async def search(q):return {'generation':2,'evidence':[item(q,'shared'),item(q+'own',q)]}
  result=await search_review_units(['low','recovery'],search,generation=2)
  assert [x['text'] for x in result['evidence']]==['shared','low','recovery']
 asyncio.run(run())


def test_numeric_target_preserves_its_verified_immediate_table_anchor():
 b=batch(([],'30'))
 b['fields'][0]['table_context']={'table_id':'t-1','cells':[{'id':'t-1-r1-c0','text':'低温保持时间'}],'nearest_nonempty_anchor_ids':{'left':'t-1-r1-c0'}}
 assert '低温保持时间' in review_queries('审阅',b,'generic.docx',1000)[0]


def test_empty_queries_do_not_trigger_unbounded_expansion():
 async def run():
  seen=[]
  async def search(q):seen.append(q);return {'generation':1,'evidence':[]}
  assert (await search_review_units(['a','a','b','c','d'],search,generation=1))['evidence']==[]
  assert seen==['a','b','c']
 asyncio.run(run())


def test_same_text_in_distinct_adopted_versions_is_not_deduplicated():
 async def run():
  async def search(q):return {'generation':1,'evidence':[{'id':q,'index_id':'idx','version_id':q,'text':'same','locator':{'page':1}}]}
  result=await search_review_units(['v1','v2'],search,generation=1)
  assert len(result['evidence'])==2
 asyncio.run(run())


def test_cached_review_reuses_exact_query_and_matching_source_in_same_chapter_only():
 async def run():
  seen=[]
  async def search(q):
   seen.append(q)
   return {'generation':2,'evidence':[{'id':q,'locator':{'heading_path':[q.partition('\n')[0]]},'text':'短时工作低温下保持两小时，记录测量结果。'}]}
  cache={}
  for q in ['温度试验\n低温保持条件','温度试验\n低温保持条件','温度试验\n\n短时工作低温下保持两小时','振动试验\n短时工作低温下保持两小时','温度试验\n短时工作低温下保持三小时']:
   result=await search_review_units([q],search,generation=2,cache=cache)
   assert result['evidence']
  assert len(seen)==3
  with pytest.raises(CoreDataError):
   await search_review_units([seen[0]],search,generation=3,cache=cache)
 asyncio.run(run())


def test_chapter_candidate_pool_independently_selects_verified_conditions():
 from agent.rag.review_queries import _cached_review_evidence
 def item(i,clause,text):
  return {'id':i,'index_id':'idx','version_id':'v','text':text,'clause_number':clause,'locator':{'heading_path':['低温试验']},'document_number':'DO-160F','score':0.01}
 real=item('a','4.5.1','温度降低到-15 °C后维持两小时，恢复到标准大气条件。')
 unrelated=item('b','4.5.2','温度降低到-55 °C后维持两小时，恢复到标准大气条件。')
 hot={**real,'id':'hot','locator':{'heading_path':['高温试验']}}
 cache={(2,'低温试验\n第一批条件'):[unrelated,hot],(2,'低温试验\n下一批条件'):[real]}
 assert [x['id'] for x in _cached_review_evidence(cache,2,'低温试验\n降低到-15 °C，恢复到标准大气条件')]==['a']
 for changed in ['降低到+15 °C，恢复到标准大气条件','降低到-15 °C，维持五小时','DO-160G §4.5.1，恢复到标准大气条件','DO-160F §4.5.2，降低到-15 °C']:
  assert _cached_review_evidence(cache,2,'低温试验\n'+changed) is None
 assert _cached_review_evidence(cache,3,'低温试验\n降低到-15 °C，恢复到标准大气条件') is None
 assert _cached_review_evidence(cache,2,'湿热试验\n降低到-15 °C，恢复到标准大气条件') is None


def test_named_external_clauses_remain_distinct_from_attachment_cross_references():
 first=review_queries('审阅',batch((['温度试验'],'按 DO-160F 第4.5.1节，参照第9.1节')),'',2000)
 second=review_queries('审阅',batch((['温度试验'],'按 DO-160F 第4.5.2节，参照第9.1节')),'',2000)
 assert '4.5.1' in first[0] and '4.5.2' in second[0] and '9.1' not in first[0]

def test_attachment_section_owner_is_independent_of_reference_pdf_and_query_hints():
 from agent.rag.review_queries import review_reference_scope
 from agent.rag.retrieval import explicit_clause_numbers
 request='请读取附件《示例环境试验大纲.docx》的正文和表格，结合项目资料库中的《DO-160F-2007-中文.pdf》，审阅第 10.1 节温度变化和第 10.2 节湿热试验计划。'
 external,topics=review_reference_scope(request)
 assert explicit_clause_numbers(external)==()
 assert topics==['温度变化','湿热']
 assert explicit_clause_numbers(review_reference_scope(request+'另外核对 DO-160F 第 10.1 节。')[0])==('10.1',)
 assert explicit_clause_numbers(review_reference_scope('请查资料库第4.5节。')[0])==('4.5',)
 assert explicit_clause_numbers(review_reference_scope('依据DO-160F第5.2节核对附件第10.1节。')[0])==('5.2',)
 queries=review_queries(request,batch((['1 概述'],'适用范围与安装位置')),'a.docx',800)
 assert '温度变化' in ' '.join(queries) and '湿热' in ' '.join(queries)
