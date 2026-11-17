"""Small model-planned document goals; no keyword business routing."""
import json
from agent.storage import CoreDataError


INSTRUCTION = (
    'Understand the original user request and choose its document scope and first read. '
    'Return only the required JSON. An attachment filename is not a task instruction. '
    'Respect negation: no export/review means do not request those actions. '
    'For the first N nonempty positions use selected with prefix_nonempty count N and empty target_ids. '
    'requested_count independently records the number explicitly requested by the USER, not the page size or an example. '
    'It must equal selector.count for prefix_nonempty. Use null when the user did not request a fixed number. '
    'Inventory is not an obligation to review the whole file. '
    'Use read for extraction, metadata, summaries and comparisons; review only for an explicitly requested review. '
    'evidence_required is a suggestion about project-library evidence only. '
    'Use false for extracting, proofreading or summarizing provided text, including standards. '
    'metadata_keys total requires the complete position inventory, not reading every body. '
    'Use exactly one source_scopes entry per source and operation: combine metadata_keys with the selected or full scope. '
    'Never create separate entries for metadata of the same source. selector must be null unless scope is selected. '
    'For DOCX position counts or ordered paragraphs/cells, prefer document.inspect_template as the first action. '
    'If chapter/section positions are not known yet, use selected, empty target_ids, '
    'and selector {"kind":"selection_pending","condition":"the requested chapter/section"}; '
    'inspect first and then resolve to actual stable IDs. Never replace an unknown selection with full. '
    'A file deliverable is required only if the user requests one. '
    'The first action only reads an authorized attachment. Do not invent source IDs or output paths.')


def example_goal(source_id):
    """Illustrate the protocol, not a classification rule for the actual request."""
    return {'source_scopes': [{'source_id': source_id, 'operation': 'read', 'scope': 'full',
        'target_ids': [], 'selector': None, 'requested_count': None,
        'metadata_keys': ['template_id', 'total'], 'reason': 'Example only: summarize a complete document.'}],
        'deliverables': [], 'success_criteria': ['Summarize the complete document.'],
        'evidence_required': False, 'answer_kind': 'synthesis',
        'first_action': {'tool': 'document.inspect_template', 'attachment_id': source_id, 'limit': 10}}


def goal_schema(attachments):
    identity={'type':'string','enum':[a['id'] for a in attachments]}
    def obj(properties):return {'type':'object','additionalProperties':False,'properties':properties,'required':list(properties)}
    selector={'anyOf':[{'type':'null'},obj({'kind':{'type':'string','enum':['prefix_nonempty']},'count':{'type':'integer','minimum':1,'maximum':10000}}),
        obj({'kind':{'type':'string','enum':['selection_pending']},'condition':{'type':'string','minLength':1,'maxLength':2000}})]}
    scope=obj({'source_id':identity,'operation':{'type':'string','enum':['read','review']},
        'scope':{'type':'string','enum':['metadata','selected','full','excluded']},
        'target_ids':{'type':'array','maxItems':10000,'items':{'type':'string'}},'selector':selector,
        'requested_count':{'type':['integer','null'],'minimum':1,'maximum':10000},
        'metadata_keys':{'type':'array','maxItems':2,'uniqueItems':True,'items':{'type':'string','enum':['template_id','total']}},'reason':{'type':'string'}})
    return obj({'source_scopes':{'type':'array','minItems':1,'maxItems':8,'items':scope},
        'deliverables':{'type':'array','maxItems':100,'items':obj({'path':{'type':['string','null']},'format':{'type':['string','null']}})},
        'success_criteria':{'type':'array','minItems':1,'items':{'type':'string'}},
        'evidence_required':{'type':'boolean'},
        'answer_kind':{'type':'string','enum':['extract','synthesis','review','modify']},
        'first_action':obj({'tool':{'type':'string','enum':['document.inspect_template','document.read_attachment']},
            'attachment_id':identity,'limit':{'type':'integer','minimum':1,'maximum':100}})})


def normalize_goal(value, attachments):
    from agent.execution.goal_requirements import validate_goal_requirements
    if not isinstance(value,dict) or set(value)!=set(goal_schema(attachments)['properties']):
        raise CoreDataError('task_goal_invalid','目标规划未返回完整结构。')
    result={key:value[key] for key in ('source_scopes','deliverables','success_criteria')}
    result['source_scopes'] = [dict(scope) for scope in result['source_scopes']]
    for scope in result['source_scopes']:
        requested_count = scope.pop('requested_count',None)
        if scope.get('selector') and scope['selector'].get('kind') == 'selection_pending':
            if requested_count is not None:
                if type(requested_count) is not int or not 1 <= requested_count <= 10000:
                    raise CoreDataError('task_goal_invalid','待解析范围的要求数量无效。')
                scope['selector'] = {**scope['selector'], 'count': requested_count}
        elif scope.get('selector') and scope['selector'].get('kind') == 'prefix_nonempty':
            if type(requested_count) is not int or scope['selector'].get('count') != requested_count:
                raise CoreDataError('task_goal_invalid','用户要求的数量与范围选择器不一致，请按原请求修正。')
        elif requested_count is not None and (type(requested_count) is not int or scope['scope'] != 'selected'
                or len(scope['target_ids']) != requested_count):
            raise CoreDataError('task_goal_invalid','明确要求的数量尚未对应到选定位置，不能扩大为全文。')
        if scope.get('selector') is None:scope.pop('selector',None)
    validate_goal_requirements(result)
    authorized={a['id'] for a in attachments}
    if {s['source_id'] for s in result['source_scopes']}-authorized:
        raise CoreDataError('task_goal_invalid','目标包含未授权附件。')
    first=value['first_action']
    if (not isinstance(first,dict) or first.get('attachment_id') not in authorized
            or first.get('tool') not in {'document.inspect_template','document.read_attachment'}
            or type(first.get('limit')) is not int or not 1<=first['limit']<=100):
        raise CoreDataError('task_goal_invalid','首次读取计划无效。')
    if type(value['evidence_required']) is not bool or value['answer_kind'] not in {'extract','synthesis','review','modify'}:
        raise CoreDataError('task_goal_invalid','目标操作无效。')
    if value['answer_kind'] == 'review' and not any(scope['operation'] == 'review' and scope['scope'] != 'excluded' for scope in result['source_scopes']):
        raise CoreDataError('task_goal_invalid','专业审阅目标必须登记 review 操作，不能仅登记 read 读取义务。')
    if value['answer_kind'] == 'extract' and any(scope['operation'] != 'read' for scope in result['source_scopes']):
        raise CoreDataError('task_goal_invalid','原文提取与专业审阅必须分别声明，不能使用纯提取收尾审阅任务。')
    return result


def final_packet(repository, task_id, requirements):
    from agent.execution.source_inventory import SourceInventory
    store=SourceInventory(repository)
    sources=[store.material(task_id,scope) for scope in requirements['source_scopes'] if scope['scope']!='excluded']
    return {'sources':[s for s in sources if s], 'success_criteria':requirements['success_criteria']}


def verified_extraction(text, packet):
    """Verify the final extraction against read proofs, not a model assertion."""
    try:
        value=json.loads(text.strip().removeprefix('```json').removeprefix('```').removesuffix('```').strip())
        expected=packet['sources']
        values=value.get('sources') if isinstance(value,dict) and 'sources' in value else [value]
        if not isinstance(values,list) or len(values)!=len(expected):raise ValueError()
        for answer,source in zip(values,expected):
            if any(answer.get(k)!=source[k] for k in ('template_id','total')):raise ValueError()
            if [(f['id'],f['text']) for f in answer['fields']] != [(f['id'],f['text']) for f in source['fields']]:raise ValueError()
        return value
    except (ValueError,TypeError,KeyError,AttributeError) as error:
        raise CoreDataError('task_final_answer_invalid','最终回答遗漏或改写了要求提取的原文。',
            {'required_sources': packet['sources'], 'instruction': 'Copy text character-for-character, including punctuation and whitespace.'}) from error


def resolve_extraction(text, packet):
    """Resolve explicit position references; never repair an invented quotation."""
    try:
        value = json.loads(text.strip().removeprefix('```json').removeprefix('```').removesuffix('```').strip())
        values = value.get('sources') if isinstance(value,dict) and 'sources' in value else [value]
        if not isinstance(values,list) or len(values) != len(packet['sources']):
            raise ValueError()
        for answer, source in zip(values, packet['sources']):
            if any(answer.get(key) != source[key] for key in ('template_id','total')):
                raise ValueError()
            answer['fields'] = [{'id': field} if isinstance(field,str) else field for field in answer['fields']]
            if [field['id'] for field in answer['fields']] != [field['id'] for field in source['fields']]:
                raise ValueError()
            for field, original in zip(answer['fields'],source['fields']):
                if 'text' in field and field['text'] != original['text']:
                    raise ValueError()
                field['text'] = original['text']
        resolved = json.dumps(value,ensure_ascii=False,indent=2)
        verified_extraction(resolved, packet)
        return resolved
    except (ValueError,TypeError,KeyError,AttributeError) as error:
        raise CoreDataError('task_final_answer_invalid','最终回答的位置引用或原文不匹配。') from error
