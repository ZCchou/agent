import asyncio
import json

from agent.execution.document_review_selection import resolve_review_selection
from agent.execution.source_inventory import SourceInventory
from test_task_plans import service


def test_review_roles_reconcile_with_same_scope_source_observations(tmp_path):
    request='结合项目资料库，审阅附件中已经填写的正式内容，跳过空白、占位和示例。'
    purpose='本节描述试验目的。高度试验是考核机载设备在最低压力条件下是否能够正常工作。'
    applicable='本节描述试验目的。本节适用于安装在加压区域内的设备。'
    permission='本节应明确所有累积试验要求，可以使用多个试验件，按照任何顺序执行试验。'
    clause='特别注意的是，若有累积试验要求，应满足RTCA DO-160G 第3.2节要求。'
    cases=[('p-79','示例：上一章的设备清单。','old','E:example','E:example'),
           ('p-83',permission,'new','C:example','C:example'),
           ('p-84',clause,'new','E:example','E:outside'),
           ('p-85','示例：本试验使用多个试验件。','new','E:example','E:example'),
           ('p-407',purpose,'alt','I:mixed','E:outside'),
           ('p-408','示例：下面列出通用试验目标。','alt','E:example','E:example'),
           ('p-484',applicable,'pressure','C:label','E:placeholder'),
           ('p-611',purpose,'vibration','C:guidance','C:example'),
           ('p-620','设备描述暂未明确。','unbound','C:example','C:example'),
           ('p-621','示例：此位置用于描述设备。','unbound','E:example','E:example'),
           ('p-700','本节描述试验目的。','writing','I:mixed','C:guidance'),
           ('p-2','本章节主要阐述本试验大纲的目的和试验大纲内容的规划。','intro','I:req','I:req'),
           ('p-110','本节描述为保证试验有效性对安装提出的通用要求，比如试验件应尽可能模拟实际装机情况进行安装；以及试验件在试验环境的安装要求及位置关系等。','installation','I:mixed','I:mixed'),
           ('t-1-r0-c0','No.','parts','C:label','C:label'),
           ('t-1-r0-c1','设备型号','parts','C:label','C:label'),
           ('t-1-r1-c0','1','parts','I:req','I:req'),('t-1-r1-c1','001','parts','I:req','I:req'),
           ('t-1-r2-c0','2','parts','I:req','I:req'),('t-1-r2-c1','2','parts','I:req','I:req'),
           ('p-73','本试验大纲所定义陪试件为完成本符合性验证试验所必须的其它装机件产品，其构型也应符合设计构型且可以支持本符合性验证试验的有效性。','definition','C:example','U:ambiguous'),
           ('p-74','建议填写本文件所定义试验件为设备，其构型应符合设计要求。','definition','C:example','C:guidance'),
           ('p-222','本节用图表的形式描述试验参数和剖面。剖面图应标注具体试验参数。','draft','I:mixed','I:mixed'),
           ('p-234','本节用表格的形式描述本试验用到的试验设备（含备用），以及环境鉴定试验中的性能检测所用到的所有激励和测量设备。','draft','I:req','I:req'),
           ('p-240','本节以图表展示试验参数。','draft','I:req','I:req'),
           ('p-241','本节以图表展示试验参数。','draft','C:guidance','I:req'),
           ('p-242','本节以图表展示试验参数。受试设备在15 kPa下必须继续工作。','filled','I:mixed','I:mixed'),
           ('p-243','本节用表格的形式描述试验设备振动台AX-123。','filled','I:mixed','I:mixed'),
           ('p-381','图 X 地面贮存高温和短时工作高温试验剖面','caption','I:req','I:req'),
           ('p-900','示例：','refs','E:example','E:example'),
           ('p-901','行业标准','refs/names','C:heading','C:heading'),
           ('t-7-r0-c0','标准名称','refs/names','I:req','I:req'),
           ('p-902','正式要求','refs/formal','C:heading','C:heading'),
           ('t-8-r0-c0','标准适用范围待确认','refs/formal','C:example','C:example')]
    fields=[dict(id=k,text=text,kind='table_cell' if k.startswith('t-') else 'paragraph',chapter_path=[chapter]) for k,text,chapter,_,_ in cases]
    for f in fields[-5:]:
        f['section_ids']=f['chapter_path'][0].split('/')
        if f['id']=='p-900':
            f['selection_context']={'example_context':{'relation':'explicit_prefix','anchor_id':f['id'],'anchor_only':True}}
        elif f['kind']=='paragraph':
            f['selection_context']={'heading_level':2}
        else:
            f['selection_context']={'explicit_italic_chars':len(f['text']),'run_text_chars':len(f['text']),
                                    'example_context':{'relation':'candidate_section_context','anchor_id':'p-900'}}
    fields[1]['selection_context']={'example_context':{'anchor_id':'p-79'}}
    first={k:v for k,_,_,v,_ in cases};second={k:v for k,_,_,_,v in cases}
    core,_,root,plans=service(tmp_path)
    template={'id':'template','sha256':'original','manifest':{'authorized_source_id':'source'}}
    SourceInventory(core).register(root['id'],'source','template','original',fields,complete=True)
    goals=plans.capture_requirements(root['id'],{'source_scopes':[{'source_id':'source','operation':'review',
        'scope':'selected','target_ids':[],'selector':{'kind':'selection_pending','condition':request},'reason':request}],
        'deliverables':[],'success_criteria':[request]},expected_revision=0)
    with core._connect() as db:
        db.execute('INSERT INTO task_goal_execution VALUES(?,?,?,?)',(root['id'],goals['revision'],goals['request_digest'],'{}'))
    async def generate(payload,schema,correction):
        codes=second if payload.get('selection_phase')=='source_role_review' else first
        return {'decisions':[codes[f['id']] for f in payload['fields']], 'context_refs':[], 'context_notes':'','context_indices':[]}
    gaps=[]
    selected=asyncio.run(resolve_review_selection(core,root['id'],template,request,fields,generate=generate,
        fits=lambda *_:True,fence=(root['revision'],root['attempt']),model_identity={},scope_gaps=gaps))
    assert set(selected)=={'p-73','p-83','p-84','p-407','p-484','p-611','p-242','p-243','t-1-r1-c1','t-1-r2-c1'}
    assert {g['target_id'] for g in gaps}=={'p-620','t-8-r0-c0'}
    with core._connect() as db:
        s=json.loads(db.execute('SELECT payload_json FROM task_goal_execution WHERE task_id=?',(root['id'],)).fetchone()[0])['_review_selection']
    assert s['base_decisions']['p-407']['basis_code']=='I:mixed'
    assert s['source_role_reviews']['p-407']['after_basis_code']=='I:mixed'
    assert s['decisions']['p-85']['basis_code']=='E:example'
    assert s['decisions']['p-110']['basis_code']=='C:guidance'
    for key in ('p-222','p-234','p-240','p-241'):
        assert s['decisions'][key]['basis_code']=='C:guidance'
    assert 'p-241' not in s['source_role_reviews']
    assert s['decisions']['p-381']['basis_code']=='C:label'
    assert s['decisions']['t-1-r1-c0']['basis_code']=='C:label'
    assert s['decisions']['t-7-r0-c0']['basis_code']=='E:example'
