"""Literal source-role observations for bounded selection rechecks, never verdicts."""
import re
from agent.review_table_context import table_contexts

OBSERVATION_VERSION='source-role-observations-v11'
EDITORIAL=re.compile(r'^\s*(?:(?:本(?:章(?:节)?|节|部分|段))|(?:this|the)\s+(?:section|chapter|paragraph))[^。；;\n]{0,48}?(?:阐述|描述|说明|介绍|列出|给出|填写|规划|describ(?:es|e)|explain|list|provide)',re.I)
INLINE_EXAMPLE=re.compile(r'比如|例如|譬如|\b(?:such as|for example)\b|e\.g\.',re.I)
OBLIGATION=re.compile(r'(?<![响对反适相回])应(?:当|该|尽可能|尽量)?|必须|不得|\b(?:shall(?:\s+not)?|must|should)\b',re.I)
EXAMPLE_PREFIX=re.compile(r'^\s*(?:示例|举例|example|例如|比如)\s*[:：]',re.I)
NUMBER=re.compile(r'\d+[.)、]?')
CELL=re.compile(r'^(.+)-r(\d+)-c(\d+)$')
TABLE_ID=r'(?:[\d一二三四五六七八九十]+(?:[.．-][\d一二三四五六七八九十]+)*|[Xx]+)'
TABLE_REFERENCE=re.compile(r'(?:如|见|参见)\s*(?:下表|表\s*('+TABLE_ID+r'))|(?:如下表所示|listed in the following table)',re.I)
TABLE_CAPTION=re.compile(r'^\s*(?:表|table)\s*('+TABLE_ID+r')',re.I)
MEASURE=re.compile(r'\d\s*(?:°|℃|%|[μµa-zA-Z]+\b|秒|分钟|小时|千帕|摄氏度)',re.I)
FORMAL_CONTENT=re.compile(r'(?:已经|已)?填写(?:了)?的正式内容|仅(?:审阅|检查)正式内容')
FORMAT_TOPIC_LIST=re.compile(
    r'^本(?:章|节|部分)建议(?:采用|用|以)(?:表格|列表|清单)(?:的)?形式'
    r'(?:具体|分别|详细)?(?:阐述|列出|说明|描述)([\u3400-\u9fff、]+)等[。.]?$')
TOPIC_ENDINGS=('组成','状态','位置','等级','说明','类别','名称','数量','条件','参数','范围','要求','方法','信息','情况')
FIGURE_PLACEHOLDER=re.compile(r'(?:图|表)\s*[Xx]+[。.]?')
EDITORIAL_ONLY=re.compile(r'本(?:节|章)(?:描述|说明|介绍)(?:本试验的?)?(?:试验)?(?:要求|目的|设备|步骤|参数和剖面)[。.]?')
NAVIGATION=re.compile(r'本(?:章节|节|章)(?:主要)?(?:阐述|介绍|概述)(?:本|该)?(?:试验大纲|报告|文件|文档)的目的(?:和|及|与)(?:试验大纲|报告|文件|文档)?(?:内容|章节)的(?:规划|安排|组织)[。.]?')
ORDINAL_HEADER=re.compile(r'^(?:序号|行号|行编号|顺序号|no\.?|row\s*(?:no\.?|number))$',re.I)

# Restrict both the object of the editorial verb and every following sentence
# to unfilled categories. A model number, condition or filled clause falls through.
FORM_PREFIX=r'本(?:节|章|部分)(?:应|可|建议)?(?:用|以|采用)(?:图表|表格|列表|清单|示意图)(?:的?形式)?(?:具体|分别|详细)?(?:描述|列出|展示|说明|给出)'
FORM_NOUN=r'(?:(?:本试验|环境鉴定试验|性能检测)(?:中|所|的|用到|用于|所用到)*|所有|以及|备用|试验设备|激励|测量设备|设备|试验参数|参数|剖面|条件|范围|时长|单位|名称|型号|和|及|含|[（），、()])'
FORM_INSTRUCTION=re.compile(FORM_PREFIX+FORM_NOUN+r'+[。.]?(?:(?:剖面图|示意图|图中|表中)(?:还)?(?:应|需|须)(?:标注|注明|列出)(?:具体|相应|各项)?(?:试验参数|参数名称|参数单位|量值和单位)[。.]?)?')
CAPTION_PLACEHOLDER=re.compile(r'图\s*[Xx]+\s+[\u3400-\u9fff]{2,40}(?:剖面|示意图|布置图|接线图)')


def unfilled_caption(text):
    return bool(CAPTION_PLACEHOLDER.fullmatch(text) and not re.search(r'应|需|必须|不得|达到|采用|保持|超过|位于|安装在',text))


def _editorial_following_sentences(text):
    sentences=[part.strip() for part in re.split(r'[。；;\n]',text) if part.strip()]
    if len(sentences)<2 or not EDITORIAL_ONLY.fullmatch(sentences[0]):
        return []
    if re.search(r'示例|例如|假设|比如|填写|建议|[Xx]{2,}|待填|\[|【',text):
        return []
    return sentences[1:]


def filled_applicability(text):
    return any(re.fullmatch(r'本(?:节|章|试验|条款)(?:仅)?适用于(?:安装在|安装于|位于|用于|具有|处于)[^。；;]+(?:设备|系统|部件|组件|装置)',part)
               for part in _editorial_following_sentences(text))


def filled_test_purpose(text):
    """A named test states what happens to equipment under a specified condition."""
    for part in _editorial_following_sentences(text):
        if not re.match(r'^[\u3400-\u9fffA-Za-z0-9 -]{1,24}试验', part):
            continue
        if (not re.search(r'考核|验证|确定|检验',part) or not re.search(r'设备|系统|部件|组件|装置|试验件',part)
                or not re.search(r'在|经历|经受|处于|条件下|过程中',part)
                or re.search(r'撰写|[需应请](?:当)?(?:描述|说明|填写|列举|给出)|什么|何种|相关条件|某种|待定|视情况|可写为|可表述为',part)):
            continue
        capability = re.search(r'是否能够|能否|是否可以|能不能|(?:仍(?:然)?能|继续|恢复).{0,32}(?:工作|运行|通信|采集|输出|保持功能)',part)
        failure = re.search(r'(?:不会|不应|不得|不发生|不出现|不).{0,28}(?:损坏|损伤|失效|脱离|脱落|分离|断裂|停机|复位)',part)
        if capability or failure or re.search(r'(?:工作|运行)是否正常',part):
            return True
    return False


def equipment_rule_after_guidance(text):
    """A subsequent clause regulates the physical equipment rather than the diagram or author."""
    if not EDITORIAL.match(text):
        return False
    if re.search(r'示例|例如|假设|比如|可写为|建议填写|待填|[Xx]{2,}',text):
        return False
    for clause in re.split(r'[，,。；;\n]',text)[1:]:
        if (re.match(r'^\s*(?:受试|被测|参试)?(?:试验件|设备|试样|组件|部件|传感器|样机|样件)',clause)
                and re.search(r'(?:应当?|必须|须)与[^，。；;]{1,48}(?:保持一致|相同|一致|相符)',clause)
                and not re.search(r'图中|表中|图例|举例|文字|撰写|填写|注释|标注',clause)):
            return True
    return False


def operational_condition(text):
    """A stated operating rule or permission, separate from instructions to write it down."""
    if re.search(r'示例|例如|假设|比如|建议填写|应填写|可填写|待填|[Xx]{2,}',text):
        return False
    if equipment_rule_after_guidance(text):
        return True
    conditional=re.search(r'(?:若|当|如果|除非)[^。；;\n]{2,120}(?:应满足|必须符合|不得|应按照)',text)
    clause=re.search(r'(?:第\s*\d+(?:\.\d+)+\s*[章节条]|§\s*\d+)',text)
    permission=re.search(r'(?:可以|允许)使用(?:不同|多个)[^，。；;]{1,24}[，,]按照[^。；;]{1,32}执行试验',text)
    definition=re.search(r'^本(?:试验)?(?:大纲|文件|规范|方案|规程|标准)(?:中)?(?:所定义|所称|定义)的?[^，。；;]{1,36}(?:为|是指)[^，。；;]+[，。；;]\s*(?:其|该)[^。；;]{0,40}(?:应(?:当)?(?:符合|满足|具备)|必须(?:符合|满足|具备))[^。；;]+',text)
    return bool((conditional and clause) or permission or definition)


def _has_review_scope_limit(request):
    if re.search(r'(?:仅限|只限|限于|只看|仅看|仅针对|只针对|关于|仅审阅其中|仅审查其中|[pt]-\d+)'
                 r'|第\s*[\d一二三四五六七八九十.]+\s*[章节条]|正式内容(?:中|里)(?:有关|涉及|关于)',request,re.I):
        return True
    for match in re.finditer(r'(?:跳过|排除|忽略|不要审阅|不审阅|不检查)\s*([^，。；;\n]+)',request):
        terms = re.split(r'[、和及与或]',match[1].strip())
        if any(term.strip() not in {'空白','空白项','空项','占位','占位符','示例','案例','未填写内容'} for term in terms):
            return True
    return False


def reviewed_role_disposition(field,decision,fact,request):
    from agent.execution.document_goal_runtime import _positive_request
    if fact.get('whole_example_anchor_ids') or decision.get('selection_issue'):
        return None
    substantive=any(fact.get(key) for key in ('explicit_applicability_statement','explicit_test_purpose','operational_condition'))
    broad_formal=_positive_request(FORMAL_CONTENT,request) and not _has_review_scope_limit(request)
    if substantive and broad_formal:
        return 'I:mixed'
    if decision.get('basis_code') in {'C:example','E:example'}:
        return 'U:ambiguous'
    return None


def editorial_disposition(field, request, observations=None):
    from agent.execution.document_goal_runtime import _positive_request
    if not _positive_request(FORMAL_CONTENT, request) or re.search(r'指导|编写建议|指导语',request):
        return None
    text=field.get('text','').strip()
    readable=field.get('field_type')!='unreadable' and field.get('extraction_status') not in {'unreadable','requires_ocr_or_visual_review'}
    if readable and FIGURE_PLACEHOLDER.fullmatch(text) and re.search('跳过[^。；;]{0,30}占位',request):
        return 'E:placeholder'
    if (observations or {}).get('sequence_header_id'):
        return 'C:label'
    if unfilled_caption(text):
        return 'C:label'
    if FORM_INSTRUCTION.fullmatch(text):
        return 'C:guidance'
    if EDITORIAL_ONLY.fullmatch(text) or NAVIGATION.fullmatch(text):
        return 'C:guidance'
    # A description of guidance illustrated by an example does not assert its implementation.
    if (re.fullmatch(r'本(?:节|章)描述[^。；;\n]{1,90}提出的(?:通用|一般)要求[，,](?:比如|例如)[^。\n]+[；;]以及[^。；;\n]+等[。.]?',text)
        and not re.search(r'已(?:经|按|实施|安装)|实际采用|本设备采用',text)):
        return 'C:guidance'
    match=FORMAT_TOPIC_LIST.fullmatch(text)
    if not match or OBLIGATION.search(match[1]) or re.search(r'实际|规定|采用|确定为|设为|位于|安装在|分别为|已(?:经|完成)|不得|禁止',match[1]):
        return None
    topics=match[1].split('、')
    if len(topics)>1 and all(topic.endswith(TOPIC_ENDINGS) for topic in topics):
        return 'C:guidance'
    return None


EXCLUDE_EXAMPLES = re.compile(
    r'(?:跳过|忽略|排除)\s*(?:(?:空白|空项|占位(?:符)?|未填写内容)[、，,和及与或\s]*)*(?:示例|案例)'
    r'|\b(?:skip|exclude|omit)\s+(?:(?:blank(?:s)?|placeholders?|and|or)[,\s]+)*examples?\b', re.I)


def explicit_example_disposition(field, sources, request, observations=None):
    from agent.execution.document_goal_runtime import _positive_request
    if not _positive_request(EXCLUDE_EXAMPLES, request):
        return None
    if ((observations or {}).get('explicit_example_table_reference_id')
        or (observations or {}).get('inherited_example_anchor_id')):
        return 'E:example'
    text = field.get('text', '').strip()
    boundary = (field.get('selection_context') or {}).get('example_context') or {}
    anchor = sources.get(boundary.get('anchor_id'), {})
    if EXAMPLE_PREFIX.match(text):
        # Multiple statements may resume normative content; retain the semantic decision.
        if not re.search(r'[。；;!?！？\n].*\S|(?:[,，]|\bbut\b)\s*(?:但|然而|实际|正式|本项目|本设备|the actual|the required)', text, re.S):
            return 'E:example'
    elif (boundary.get('relation') == 'candidate_italic_continuation'
          and EXAMPLE_PREFIX.match(anchor.get('text', ''))
          and anchor.get('chapter_path') == field.get('chapter_path')):
        shared = set(re.findall(r'[A-Za-z][A-Za-z0-9-]*\d[A-Za-z0-9-]*', text)).intersection(
            re.findall(r'[A-Za-z][A-Za-z0-9-]*\d[A-Za-z0-9-]*', anchor['text']))
        if shared and re.search(r'X{2,}', text) and re.search(r'X{2,}', anchor['text']):
            return 'E:example'
        return 'U:ambiguous'
    return None


def source_role_observations(fields, *, shared_contexts=None):
    by_id={field['id']:field for field in fields};indexes={field['id']:i for i,field in enumerate(fields)}
    rows={};locations={};table_first={};numeric=[]
    for field in fields:
        match=CELL.fullmatch(field['id']) if field.get('kind','').startswith('table_') else None
        if match:
            table,row,column=match.group(1),int(match.group(2)),int(match.group(3))
            locations[field['id']]=(table,row,column);rows.setdefault((table,row),[]).append(field)
            table_first.setdefault(table,indexes[field['id']])
            if NUMBER.fullmatch(field.get('text','').strip()):numeric.append(field['id'])
    numeric_set=set(numeric)
    context_ids=list(dict.fromkeys([*numeric,*(key for key,(_,row,_) in locations.items() if row==0)]))
    table_details=shared_contexts(context_ids) if shared_contexts else table_contexts(fields,context_ids)
    table_example={};explicit_tables={};recent=[]
    for field in fields:
        if field['id'] in locations:
            table=locations[field['id']][0]
            if table not in table_example:
                table_example[table]=[prior['id'] for prior in recent if EXAMPLE_PREFIX.match(prior.get('text',''))]
                preceding=list(recent)
                caption=TABLE_CAPTION.match(preceding[-1].get('text','')) if preceding else None
                if caption:
                    preceding.pop()
                if preceding:
                    prior=preceding[-1]
                    reference=TABLE_REFERENCE.search(prior.get('text',''))
                    reference_id=reference.group(1) if reference else None
                    caption_id=caption.group(1) if caption else None
                    same_table=(not reference_id or (caption_id and
                        (reference_id==caption_id or (set(reference_id.lower())=={'x'} and set(caption_id.lower())=={'x'}))))
                    if reference and caption and not same_table:
                        table_example[table]=[key for key in table_example[table] if key!=prior['id']]
                    if (EXAMPLE_PREFIX.match(prior.get('text','')) and reference and same_table
                        and prior.get('chapter_path')==field.get('chapter_path')
                        and not re.search(r'[。；;!?！？\n].*\S', prior['text'], re.S)):
                        explicit_tables[table]=prior['id']
            recent=[]
        elif field.get('text','').strip():recent=[*recent[-1:],field]
    result={};serial={};open_example=None
    for field in fields:
        identity=field['id'];text=field.get('text','');index=indexes[identity]
        fact={'version':OBSERVATION_VERSION,'authority':'source_observation_only'}
        level=(field.get('selection_context') or {}).get('heading_level')
        style=field.get('selection_context') or {}
        association=style.get('example_context') or {}
        section_ids=field.get('section_ids') or []
        if open_example:
            root_id,anchor_id=open_example
            formal_boundary=re.search(r'正式(?:内容|条款|要求|记录)|本项目(?:实际|采用)|实测结果|非示例|不是示例',text)
            if root_id not in section_ids or formal_boundary:
                open_example=None
            elif not level and text.strip():
                italic=style.get('explicit_italic_chars',0)==style.get('run_text_chars',0)>0
                if italic and association=={'anchor_id':anchor_id,'relation':'candidate_section_context'}:
                    fact['inherited_example_anchor_id']=anchor_id
                else:
                    open_example=None
        if association.get('relation')=='explicit_prefix' and association.get('anchor_only') and section_ids:
            open_example=(section_ids[-1],identity)
        if type(level) is int and level>0:fact['source_heading_level']=level
        editorial=EDITORIAL.search(text)
        if FORM_INSTRUCTION.fullmatch(text) or unfilled_caption(text):
            fact['unfilled_form_or_caption']=True
        if filled_applicability(text):fact['explicit_applicability_statement']=True
        if filled_test_purpose(text):fact['explicit_test_purpose']=True
        if operational_condition(text):fact['operational_condition']=True
        if FIGURE_PLACEHOLDER.fullmatch(text.strip()):fact['figure_number_placeholder']=True
        if editorial:fact['editorial_cue_spans']=[{'start':editorial.start(),'end':editorial.end()}]
        inline=[{'start':m.start(),'end':m.end()} for m in INLINE_EXAMPLE.finditer(text)]
        if inline:fact['inline_example_spans']=inline[:8]
        obligation=[{'start':m.start(),'end':m.end()} for m in OBLIGATION.finditer(text)]
        if obligation:fact['obligation_marker_spans']=obligation[:8]
        anchors=[fact['inherited_example_anchor_id']] if fact.get('inherited_example_anchor_id') else []
        if EXAMPLE_PREFIX.match(text):anchors.append(identity)
        associated=(field.get('selection_context') or {}).get('example_context',{}).get('anchor_id')
        if (associated in by_id and indexes[associated]<=index and EXAMPLE_PREFIX.match(by_id[associated].get('text',''))
            and association.get('relation')!='candidate_section_context'
            and by_id[associated].get('chapter_path',[])==field.get('chapter_path',[])):
            anchors.append(associated)
        if identity in locations:
            table,row,column=locations[identity]
            if table in explicit_tables:fact['explicit_example_table_reference_id']=explicit_tables[table]
            if row>0 and MEASURE.search(text):
                fact['measured_table_value']=True
                fact['nearby_original_anchor_ids']=[other['id'] for other in rows[(table,row)]
                    if other['id']!=identity and other.get('text','').strip()][:8]
            anchors.extend(key for key in table_example[table]
                if tuple(by_id[key].get('chapter_path',[]))==tuple(field.get('chapter_path',[])))
            if identity in numeric_set or row==0:
                fact['recorded_first_row']=row==0
                peers=[other for other in rows[(table,row)] if other['id']!=identity]
                fact.update(numeric_token=identity in numeric_set,
                    same_recorded_row_nonempty_ids=[other['id'] for other in peers if other.get('text','').strip()][:8],
                    same_recorded_row_nonempty_count=sum(bool(other.get('text','').strip()) for other in peers),
                    same_recorded_row_empty_count=sum(not other.get('text','').strip() for other in peers))
                context=table_details.get(identity,{})
                above=(context.get('nearest_nonempty_anchor_ids') or {}).get('above')
                header=by_id.get(above,{})
                coord=locations.get(above)
                if (identity in numeric_set and row>0 and column==0 and fact['same_recorded_row_nonempty_count']
                    and coord and coord[0]==table and coord[2]==column and re.fullmatch(r'[1-9]\d*',text.strip())):
                    prior=serial.get(above)
                    if coord[1]==0 and ORDINAL_HEADER.fullmatch(header.get('text','').strip()) and text.strip()=='1':
                        serial[identity]=(above,1)
                    elif prior and str(prior[1]+1)==text.strip():
                        serial[identity]=(prior[0],prior[1]+1)
                    if identity in serial:fact['sequence_header_id']=serial[identity][0]
                fact['nearby_original_anchor_ids']=list(dict.fromkeys(
                    [cell['id'] for cell in context.get('cells',[])]+fact['same_recorded_row_nonempty_ids'][:4]))
                if field.get('table_grid'):fact['target_grid']=dict(field['table_grid'])
                fact['row_coordinate']=row
        if anchors:fact['whole_example_anchor_ids']=list(dict.fromkeys(anchors))
        following=[other['id'] for other in fields[index+1:index+3]
            if not other.get('kind','').startswith('table_') and EXAMPLE_PREFIX.match(other.get('text',''))
            and other.get('chapter_path',[])==field.get('chapter_path',[])]
        if following:fact['following_example_anchor_ids']=following
        if len(fact)>2:result[identity]=fact
    return result


def role_review_reasons(field,decision,fact):
    reasons=[]
    if fact.get('unfilled_form_or_caption') and decision.get('decision')=='include':
        reasons.append('drafting_form_or_caption_only')
    if fact.get('explicit_applicability_statement') and decision.get('decision')!='include':
        reasons.append('specific_applicability_after_editorial_sentence')
    if fact.get('explicit_test_purpose') and decision.get('decision')!='include':
        reasons.append('specific_test_purpose_after_editorial_sentence')
    if fact.get('operational_condition') and decision.get('decision')!='include':
        reasons.append('operational_condition_or_permission')
    if fact.get('figure_number_placeholder') and decision.get('decision')=='include':
        reasons.append('standalone_figure_number_placeholder')
    if (fact.get('measured_table_value') and not fact.get('whole_example_anchor_ids')
        and decision.get('basis_code') in ('C:label','C:example','E:example')):
        reasons.append('measured_value_without_local_example_boundary')
    if decision.get('decision')=='include' and fact.get('editorial_cue_spans'):reasons.append('editorial_include')
    if decision.get('decision')=='include' and fact.get('whole_example_anchor_ids'):reasons.append('source_example_include')
    if decision.get('basis_code') in {'E:example','C:example'} and not fact.get('whole_example_anchor_ids'):reasons.append('whole_example_without_preceding_boundary')
    if decision.get('decision')=='include' and fact.get('numeric_token'):reasons.append('numeric_table_include')
    if decision.get('decision')=='include' and (fact.get('source_heading_level') or fact.get('recorded_first_row')):reasons.append('structural_position_include')
    if decision.get('decision')=='uncertain':reasons.append('uncertain_membership')
    return reasons


def needs_role_review(field,decision,fact):
    return bool(role_review_reasons(field,decision,fact))
