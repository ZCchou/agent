from agent.execution import selection_roles

def roles():return selection_roles

def test_editorial_words_and_inline_examples_are_source_spans_not_decisions():
 r=roles();fields=[{'id':'a','text':'本节说明拟填写的设备功能和参数。','kind':'paragraph','chapter_path':['A']},
 {'id':'b','text':'本节描述安装要求，比如设备应模拟实际装机进行连接。','kind':'paragraph','chapter_path':['A']},
 {'id':'c','text':'示例：某设备的连接方式如下。','kind':'paragraph','chapter_path':['A']}]
 v=r.source_role_observations(fields)
 assert v['a']['editorial_cue_spans'] and not v['a'].get('obligation_marker_spans')
 assert v['b']['editorial_cue_spans'] and v['b']['obligation_marker_spans'] and v['b']['inline_example_spans']
 assert v['b']['following_example_anchor_ids']==['c'] and not v['b'].get('whole_example_anchor_ids')
 assert all('decision' not in value and 'include' not in value for value in v.values())
 assert r.needs_role_review(fields[0],{'decision':'include','basis_code':'I:mixed'},v['a'])
 assert r.needs_role_review(fields[1],{'decision':'exclude','basis_code':'E:example'},v['b'])


def cell(identity,text,row,column):
 return {'id':identity,'text':text,'kind':'table_cell','position':{'row':row,'column':column},
  'table_grid':{'row_start':row,'row_end':row+1,'column_start':column,'column_end':column+1,'grid_span':1,'row_span':1}}


def test_numeric_cells_retain_actual_header_and_observed_row_context_without_ownership():
 r=roles();fields=[cell('t-r0-c0','序号',0,0),cell('t-r0-c1','设备名称',0,1),cell('t-r1-c0','1',1,0),cell('t-r1-c1','',1,1),
  cell('v-r0-c0','次数',0,0),cell('v-r0-c1','对象',0,1),cell('v-r1-c0','1',1,0),cell('v-r1-c1','实际设备A',1,1)]
 v=r.source_role_observations(fields)
 assert v['t-r1-c0']['numeric_token'] and v['t-r1-c0']['same_recorded_row_nonempty_ids']==[]
 assert v['t-r1-c0']['same_recorded_row_empty_count']==1
 assert 't-r0-c0' in v['t-r1-c0']['nearby_original_anchor_ids']
 assert v['v-r1-c0']['same_recorded_row_nonempty_ids']==['v-r1-c1']
 assert r.needs_role_review(fields[6],{'decision':'include','basis_code':'I:req'},v['v-r1-c0'])
 assert 'owner' not in str(v)
