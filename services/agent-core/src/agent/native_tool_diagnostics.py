"""Bounded structural native-tool diagnostics; never retain argument values."""
import json
from agent.tool_contracts import TOOL_DEFINITIONS

CERTIFICATIONS={'certified_single','certified_local_batch','missing_terminal','batch_limit','missing_identity',
                'duplicate_identity','unknown_tool','native_tool','not_fully_drained','semantic_forwarded'}
STAGES={'fragment','arguments_json','arguments_schema'}
REASONS={'invalid_call','invalid_index','invalid_function','invalid_id_metadata','invalid_name_metadata',
         'conflicting_metadata','invalid_arguments_type','invalid_json','invalid_schema'}
TYPES={'null','boolean','integer','number','string','array','object','other'}
BLOCKS={'eligible','not_certified','tools_disabled','structured_transport','already_corrected_round',
        'attempt_limit','pending_tool_calls','native_side_effect'}
RULES={
 'Tool arguments contain unsupported fields.':'unsupported_fields',
 'Tool call id is invalid.':'call_identity','Tool name is invalid.':'tool_name',
 'The requested tool is not supported.':'unsupported_tool','Tool arguments must be an object.':'argument_object',
 '文档工具参数缺失或包含未知字段。':'document_fields','文档标识或路径无效。':'document_identity',
 '审阅位置清单无效。':'document_targets','审阅报告格式无效。':'document_format',
 '文档读取游标无效。':'document_cursor','文档读取范围无效。':'document_range',
 '请选择一个模板附件、模板标识或项目路径，并省略另外两个参数。':'template_identity',
 '模板分页范围无效。':'template_range','模板填写项无效。':'template_fields',
 '模板位置或正文无效。':'template_field_content','模板证据或缺项清单无效。':'template_evidence',
 '模板证据编号无效。':'template_citation','请选择文档产物或提供正文。':'export_source',
 '导出内容或格式无效。':'export_content','RAG query is invalid.':'rag_query',
 'RAG filter is invalid.':'rag_filter','RAG result limit is invalid.':'rag_limit',
 'RAG evidence range is invalid.':'rag_range'}
SCHEMAS={x['function']['name']:set(x['function'].get('parameters',{}).get('properties',{})) for x in TOOL_DEFINITIONS}
ALIASES={k.replace('.','_'):k for k in SCHEMAS}


def value_type(value):
    return {type(None):'null',bool:'boolean',int:'integer',float:'number',str:'string',list:'array',dict:'object'}.get(type(value),'other')


def sanitize(value):
    """Re-allowlist at persistence, even if caller supplied forged details."""
    if not isinstance(value,dict):return {}
    result={}
    for key,allowed in [('stage',STAGES),('reason',REASONS),('validation_rule',set(RULES.values())|{'other_schema'}),('certification_reason',CERTIFICATIONS)]:
        if isinstance(value.get(key),str) and value[key] in allowed:result[key]=value[key]
    for key in ('call_count','call_index','unknown_key_count','json_error_position','json_error_line','json_error_column'):
        if type(value.get(key)) is int and 0<=value[key]<=100000000:result[key]=value[key]
    name=value.get('tool_name')
    if isinstance(name,str) and name in SCHEMAS:
        result['tool_name']=name
        types=value.get('argument_types')
        if isinstance(types,dict):result['argument_types']={k:v for k,v in types.items()
            if isinstance(k,str) and k in SCHEMAS[name] and isinstance(v,str) and v in TYPES}
    if type(value.get('unknown_tool')) is bool:result['unknown_tool']=value['unknown_tool']
    return result


def build(calls,index,stage,reason,error=None):
    result={'stage':stage,'reason':reason,'call_count':len(calls)}
    if type(index) is int and index>=0:result['call_index']=index
    raw=calls.get(index,{}) if type(index) is int else {}
    name=raw.get('name')
    canonical=ALIASES.get(name,name) if isinstance(name,str) else None
    if canonical in SCHEMAS:
        result['tool_name']=canonical
        try:arguments=json.loads(raw.get('arguments') or '{}')
        except (ValueError,TypeError,RecursionError):arguments=None
        if isinstance(arguments,dict):
            result['argument_types']={k:value_type(v) for k,v in arguments.items() if k in SCHEMAS[canonical]}
            result['unknown_key_count']=sum(k not in SCHEMAS[canonical] for k in arguments)
    elif name:result['unknown_tool']=True
    if isinstance(error,json.JSONDecodeError):
        result.update(json_error_position=error.pos,json_error_line=error.lineno,json_error_column=error.colno)
    elif error is not None and stage=='arguments_schema':
        result['validation_rule']=RULES.get(getattr(error,'message',None),'other_schema')
    return sanitize(result)


def runtime_blocked_reason(*,eligible,tools_enabled,structured,already_corrected,attempt_limit,pending_calls,native_activity):
    if not eligible:return 'not_certified'
    if not tools_enabled:return 'tools_disabled'
    if structured:return 'structured_transport'
    if already_corrected:return 'already_corrected_round'
    if attempt_limit:return 'attempt_limit'
    if pending_calls:return 'pending_tool_calls'
    if native_activity:return 'native_side_effect'
    return 'eligible'


def batch_correction_certification(calls,finish_reason,*,forwarded_semantic=False):
    """Certify only a fully drained, unforwarded, unambiguous local batch."""
    if forwarded_semantic:return False,'semantic_forwarded'
    if finish_reason not in {'stop','tool_calls'}:return False,'missing_terminal'
    # Preserve the established single-call correction contract.
    if len(calls)==1:return True,'certified_single'
    if not 2<=len(calls)<=32:return False,'batch_limit'
    identities=[]
    for raw in calls.values():
        identity=raw.get('id')
        if not isinstance(identity,str) or not 1<=len(identity)<=200:return False,'missing_identity'
        identities.append(identity)
    if len(set(identities))!=len(identities):return False,'duplicate_identity'
    for raw in calls.values():
        name=raw.get('name')
        if name in {'$web_search','native.web_search'}:return False,'native_tool'
        if not isinstance(name,str) or ALIASES.get(name,name) not in SCHEMAS:return False,'unknown_tool'
    return True,'certified_local_batch'
