"""Task evidence decisions from user-authored intent and saved task state."""
import re
import json

from agent.storage import CoreDataError
from agent.rag.followup import FOLLOWUP


GREETING = re.compile(r'(?:你好|您好|嗨|哈[喽啰]|早上好|下午好|晚上好|谢谢(?:你)?|再见|hello|hi|thanks|thank you)', re.I)
BUSINESS = re.compile(r'适航|条款|符合性|规章|鉴定|DO\s*[-－]?\s*160|CCAR|CS[-－]?\d|\b(?:FAR|RTCA|airworthiness)\b|振动试验|试验目的|(?:^|\s)(?:2[13579]|3[135])\.\d{3,}|依据.{0,20}(?:资料|标准|法规)|(?:填写|回填).{0,15}模板|(?:资料库|知识库).{0,10}(?:内容|要求|条款)|(?:查询|查找|检索).{0,10}(?:资料库|知识库)', re.I)
CONTINUATION = re.compile(r'^(?:好的?|是的?|继续|接着|确认|同意|按上述|沿用|下一节|下一项|继续填写|yes|ok|continue)[。.!！?？\s]*$', re.I)
EXPORT = re.compile(r'(?:保存|导出|输出|生成|写入|写成|制作|另存|save|export|generate).{0,100}(?:word|docx|markdown|txt|文档|文件|\.md|\.txt)', re.I)
PROFESSIONAL = re.compile(r'合格判据|环境试验|温度试验|冲击试验|试验.{0,8}(?:量级|类别|条件|程序|判据)|机载设备|失效.{0,20}(?:安全|要求|分析)|certification|compliance|vibration\s+test', re.I)
SOFTWARE_CONTEXT = re.compile(r'智能体|这个软件|应用|界面|模型|资料库|知识库|app\b', re.I)
SOFTWARE_ACTION = re.compile(r'启用|导入|加载|卸载|按钮|设置|配置|创建资料库', re.I)
PROFESSIONAL_ACTION = re.compile(r'符合性|条款.{0,12}(?:要求|依据)|试验目的|填写|回填|合格判据')


def previous_evidence_task(messages):
    answer = next((item for item in reversed(messages) if item.get('role') == 'assistant'), None)
    return bool(answer and 'rag-evidence:' in answer.get('content', ''))


def continuation_task(run, messages, plan):
    from agent.context_engine.attachment_references import validate_saved_selection
    current = next(item for item in messages if item['id'] == run['user_message_id'])
    decision = run.get('rag_decision') or {}
    selection = validate_saved_selection(plan, plan['resolved_intent']) if plan else []
    if current.get('attachments') and not selection:
        raise CoreDataError('attachment_reference_selection_changed', '原任务缺少可核验的附件选择，请重新选择模板附件后继续。')
    return {'request': decision.get('query_context') or current['content'],
            'task_type': decision.get('task_type', 'evidence_answer'),
            'roles': decision.get('attachment_roles', {}),
            'references': [{'attachment_id': item['current_attachment_id'],
                **{key: item[key] for key in ('source_start', 'source_end') if key in item}} for item in selection]}


def resolve_attachment_roles(attachments, requested=None):
    requested = {} if requested is None else requested
    selected = {item['id'] for item in attachments}
    if not isinstance(requested, dict) or set(requested) - selected or any(not isinstance(role, str) or role not in {'template', 'reference'} for role in requested.values()):
        raise CoreDataError('attachment_purpose_invalid', '附件用途须对应本轮已授权的附件。')
    # Stored purposes and old clients supply compatibility hints only.
    return {item['id']: role for item in attachments
            if (role := requested.get(item['id'], item.get('purpose'))) in {'template', 'reference'}}


def decide_evidence(text, *, mode='auto', previous_evidence=False, pending_evidence=False, skill_required=False):
    if mode not in {'auto', 'required'}:
        raise CoreDataError('invalid_rag_mode', 'Choose auto or required RAG mode.')
    normalized = text.strip().rstrip('。.!！?？').strip()
    def positive_hint(pattern):
        return any(not re.search(r'(?:不要|无需|不必|禁止|不需要|不做|不)(?:再|进行)?\s*$', text[max(0, match.start()-8):match.start()])
                   for match in pattern.finditer(text))
    export = positive_hint(EXPORT) or positive_hint(re.compile(r'(?:填写|回填|填好).{0,30}模板'))
    continuation = bool(CONTINUATION.fullmatch(text.strip()) or FOLLOWUP.fullmatch(normalized))
    business = bool(BUSINESS.search(text) or PROFESSIONAL.search(text))
    software_help = bool(SOFTWARE_CONTEXT.search(text) and SOFTWARE_ACTION.search(text) and not PROFESSIONAL_ACTION.search(text))
    contextual_question = bool(re.match(r'^(?:它|这(?:个|项|种)?|该设备|上述).{0,24}(?:通过|符合|满足|适用|是否|能否)', normalized))
    if mode == 'required':
        required, reason = True, 'user_required'
    elif skill_required:
        required, reason = True, 'skill_required'
    elif GREETING.fullmatch(normalized):
        required, reason = False, 'greeting'
    elif software_help:
        required, reason = False, 'software_help'
    elif business:
        required, reason = True, 'business_evidence'
    elif (previous_evidence or pending_evidence) and (continuation or export or contextual_question):
        required, reason = True, 'evidence_continuation'
    else:
        required, reason = False, 'ordinary_request'
    refers_to_answer = bool(re.search(r'刚才|上面|上述|这段|这个回答|previous|above|that answer', text, re.I))
    return {'requested_mode': mode, 'required': required, 'reason': reason,
            'reuse_evidence': required and previous_evidence and (continuation or contextual_question or (export and refers_to_answer and not business)),
            'operation_hint': 'export' if export else None,
            'task_type': 'evidence_answer' if required else 'chat'}


def explicit_document_scope(text):
    """Retain literal user range declarations while leaving other intent to the model."""
    original, separator, supplement = text.partition('\n\n补充信息\n')
    if separator:
        try:
            answers = json.loads(supplement)
        except (ValueError, TypeError):
            answers = dict(line.split(' ', 1) for line in supplement.splitlines() if ' ' in line)
        choices = {'整份模板': 'full', '整份': 'full', '指定章节': 'section', '具体章节': 'section'}
        scope_keys = {'scope', 'task_scope', 'document_scope', 'fill_scope', 'template_scope'}
        scope_answers = [value.strip() for key, value in answers.items()
                         if key.casefold() in scope_keys and isinstance(value, str)] \
            if isinstance(answers, dict) else []
        if scope_answers:
            selected = {choices.get(value) for value in scope_answers}
            return next(iter(selected)) if len(selected) == 1 else None
    patterns = [
        (r'(?:填写|回填|填好)\s*(?:此|该|这个|这份|本|整份|整个|完整的?)?(?:附件|模板|文档|《[^》\r\n]{1,100}》)'
         r'\s*(?:中|内)[^\r\n]{0,160}[”"」』]\s*一[章节]', 'section'),
        (r'(?:填写|回填|填好|完成)\s*(?:(?:整份|整个|完整的?)\s*(?:模板|文档|试验大纲)|所有章节|全部章节)', 'full'),
    ]
    matches = []
    for pattern, scope in patterns:
        for match in re.finditer(pattern, original):
            prefix = original[max(0, match.start() - 4):match.start()].strip()
            if not prefix.endswith(('不要', '无需', '禁止', '不必', '不是', '不需要')):
                matches.append((match.start(), scope))
    return min(matches, key=lambda item: item[0])[1] if matches else None


def merge_evidence_intent(decision, intent, *, error_code=None):
    """Apply descriptive model intent while retaining executor evidence obligations."""
    result = {**decision, 'source': 'rules'}
    if intent is None:
        result.update(source='fallback', advisor_error_code=error_code,
                      needs_confirmation=not decision['required'])
        return result
    requirement = intent.get('rag_requirement', 'uncertain')
    result['source'] = 'advisor'
    result['semantic_requirement'] = requirement
    protected = (decision['reason'] in {'user_required', 'skill_required'}
                 or decision.get('reuse_evidence') or decision.get('continued_run_id'))
    if not protected:
        if requirement == 'uncertain':
            result['needs_confirmation'] = True
        else:
            result.update(required=requirement == 'required', reason='semantic_evidence' if requirement == 'required' else 'semantic_ordinary')
            result['reuse_evidence'] = bool(result['required'] and decision.get('reuse_evidence'))
    if 'document_export' in intent.get('task_kinds', []):
        result['task_type'] = 'document_export'
    elif 'attachment_extract' in intent.get('task_kinds', []):
        result['task_type'] = 'attachment_extract'
    elif decision['task_type'] != 'document_export':
        result['task_type'] = 'evidence_answer' if result['required'] else 'chat'
    for key in ('read_attachment_ids', 'read_requests', 'task_scope', 'retrieval_scope', 'allow_simulated_data', 'clarification', 'document_workflow'):
        if key in intent:
            result[key] = intent[key]
    if intent.get('task_scope_source') == 'user_explicit':
        result['task_scope_source'] = 'user_explicit'
    if intent.get('document_workflow') == 'review_nonempty' and intent.get('task_scope') == 'full':
        result.update(template_review=True, required=True, reason='semantic_evidence')
        if 'document_export' not in intent.get('task_kinds', []):
            result['task_type'] = 'evidence_answer'
    return result


def routing_intent_schema(attachment_ids, *, pending_action=False):
    """The existing classifier contract, constrained to this request's authority."""
    from agent.document_contracts import DOCUMENT_DEFINITIONS
    def choice(values):
        return {'type': 'string', 'enum': list(values)}
    def object_schema(properties):
        return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}
    identity = {'type': 'string', 'minLength': 1, 'maxLength': 200}
    if attachment_ids:
        identity['enum'] = list(attachment_ids)
    read = next(item['function']['parameters'] for item in DOCUMENT_DEFINITIONS
                if item['function']['name'] == 'document.read_attachment')
    read_properties = {key: {**value, 'type': [value['type'], 'null']}
                       for key, value in read['properties'].items() if key not in {'attachment_id', 'cursor'}}
    read_properties = {'attachment_id': identity, **read_properties}
    question = object_schema({'id': {'type': 'string', 'minLength': 1, 'maxLength': 80},
        'question': {'type': 'string', 'minLength': 1, 'maxLength': 160},
        'options': {'type': 'array', 'maxItems': 4, 'items': {'type': 'string', 'minLength': 1, 'maxLength': 100}}})
    clarification = object_schema({'summary': {'type': 'string', 'minLength': 1, 'maxLength': 200},
        'questions': {'type': 'array', 'minItems': 1, 'maxItems': 3, 'items': question}})
    return object_schema({
        'files': choice(('none', 'read', 'write', 'available')),
        'tools': choice(('none', 'preferred', 'required', 'available')),
        'web': choice(('off', 'optional', 'required')), 'web_evidence': choice(('optional', 'required')),
        'reasoning': choice(('standard', 'preferred')),
        'task_kinds': {'type': 'array', 'minItems': 1, 'maxItems': 4, 'items': choice((
            'vision', 'research', 'file_work', 'reasoning', 'general', 'airworthiness',
            'document_export', 'attachment_summary', 'attachment_extract', 'capability_query'))},
        'explanation_code': {'type': 'string', 'minLength': 1, 'maxLength': 80, 'pattern': '^[a-z0-9_.-]{1,80}$'},
        'follow_up': choice(('none', 'confirm', 'reject') if pending_action else ('none',)),
        'rag_requirement': choice(('required', 'not_needed', 'uncertain')),
        'read_attachment_ids': {'type': 'array', 'maxItems': min(8, len(attachment_ids)), 'items': identity},
        'read_requests': {'type': 'array', 'maxItems': min(8, len(attachment_ids)), 'items': object_schema(read_properties)},
        'task_scope': choice(('full', 'section', 'unspecified')), 'retrieval_scope': choice(('focused', 'broad')),
        'document_workflow': choice(('none', 'review_nonempty')),
        'allow_simulated_data': {'type': 'boolean'},
        'clarification': {'anyOf': [{'type': 'null'}, clarification]},
    })


def intent_tokens(value):
    """Conservative mixed-language estimate when no provider tokenizer is exposed."""
    from agent.context_engine.policy import estimate_tokens
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    return max(estimate_tokens(text), (len(text.encode('utf8')) + 2) // 3)


def intent_payload(content, attachments, recent_messages, conversation_kind, *, budget_bytes=None,
                   budget_tokens=None, pending_task=None):
    """Bound classifier data independently from attachment bodies and generation history."""
    def clip(text, limit):
        return str(text).encode('utf8')[:limit].decode('utf8', errors='ignore')

    measure = intent_tokens if budget_tokens is not None else lambda text: len(text.encode('utf8'))
    budget = budget_tokens if budget_tokens is not None else budget_bytes
    value = {'request': '', 'request_truncated': False, 'attachments': [],
             'work_kind': conversation_kind, 'recent_conversation': []}
    scope = explicit_document_scope(content)
    if scope:
        value['task_scope_constraint'] = scope
    if pending_task:
        value['pending_task'] = {'task_type': pending_task.get('task_type'), 'required': bool(pending_task.get('required')),
                                 'request': clip(pending_task.get('request', ''), 256)}
    def render():
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'))

    if pending_task:
        while measure(render()) > budget - 32 and value['pending_task']['request']:
            value['pending_task']['request'] = value['pending_task']['request'][:-16]

    for attachment in attachments:
        entry = {'id': attachment.get('id', ''),
                 'name': attachment.get('name', 'attachment')}
        for key in ('media_type', 'byte_size', 'purpose'):
            if key in attachment:
                entry[key] = attachment[key]
        value['attachments'].append(entry)
    value['request'] = content
    if measure(render()) > budget:
        from agent.execution.input_sources import input_descriptor
        value.update(request='', request_truncated=True,
                     input_source={**input_descriptor(content), 'defer_interpretation': True,
                                   'routing_hint': {'files': 'available', 'tools': 'available', 'reason': 'raw_input_read_required'}})
        # A projection is insufficient to classify all requirements or scope.
        value.pop('task_scope_constraint', None)
        if measure(render()) > budget:
            raise CoreDataError('routing_intent_context_too_small',
                '当前模型上下文不足以保留附件标识与原始输入回读信息，请扩大上下文或减少附件后重试。')
        request, separator, answers = content.partition('\n\n补充信息\n')
        essential_tail = separator + answers
        retained = len(content)
        while True:
            if essential_tail and len(essential_tail) < retained // 2:
                preview = request[:retained - len(essential_tail)] + '\n[original input requires reading]\n' + essential_tail
            else:
                half = retained // 2
                preview = content[:half] + '\n[original input requires reading]\n' + content[-(retained - half):] if retained else ''
            value['request'] = preview
            if measure(render()) <= budget:
                break
            retained //= 2
    for message in reversed(recent_messages[-6:]):
        entry = {'role': message['role'], 'content': clip(message.get('content', ''), 256)}
        value['recent_conversation'].insert(0, entry)
        if measure(render()) > budget:
            value['recent_conversation'].pop(0)
            break
    return render()

_ATTACHMENT_OPERATION = re.compile(
    r'提取|摘录|抄录|转录|摘要|概括|总结|改写|翻译|校对|错别字|格式转换|列出.{0,12}(?:非空|字段|标题)'
    r'|\b(?:extract|transcribe|summari[sz]e|translate|proofread|convert|reformat)\b', re.I)
_PROJECT_SOURCE = re.compile(r'资料库|知识库|项目证据|project (?:evidence|library)|knowledge base', re.I)
_SOURCE_NEGATION = re.compile(
    r'(?:不要|无需|不必|禁止|不需要|不使用|不查|不依赖)[^，。；、,.;]{0,20}$'
    r'|\b(?:do not|don.t|without|no need to)\b[^,.;]{0,35}$', re.I)
_LOCAL_SOURCE = re.compile(
    r'(?:仅|只|仅限)(?:能|可以|需|使用|根据|依据|基于|参考|读取|处理|提取|审阅|校对|对照|\s){0,5}(?:本|这份|当前|所附|提供的)?(?:附件|模板|原文)'
    r'|\b(?:only|solely)\b[^;.!?]{0,45}\b(?:attach(?:ed|ment)|provided document)\b', re.I)
_EXTERNAL_CHECK = re.compile(
    r'(?:依据|按照|根据|对照|参照|按)[^，。；、,.;]{0,50}(?:条款|法规|标准|规章|DO\s*[-－]?\s*160|CCAR|RTCA)'
    r'|\b(?:against|according to)\b[^,.;]{0,65}(?:standard|regulation|DO\s*-?\s*160|CCAR|RTCA)', re.I)


def project_evidence_policy(decision, request, *, has_attachments, confirmed=False):
    """Decide the role of project evidence from the request and trusted settings."""
    reason = decision.get('reason')
    hint_reason = (decision.get('rule_hint') or {}).get('reason')
    if decision.get('requested_mode') == 'required' or 'user_required' in {reason, hint_reason}:
        return 'required'

    def requested(pattern):
        return any(not _SOURCE_NEGATION.search(request[max(0, match.start()-40):match.start()])
                   for match in pattern.finditer(request))

    from agent.rag.retrieval import explicit_clause_numbers

    local_only = bool(_LOCAL_SOURCE.search(request)) and has_attachments
    project_mentioned = bool(_PROJECT_SOURCE.search(request))
    explicit_project = requested(_PROJECT_SOURCE)
    if local_only or (project_mentioned and not explicit_project):
        return 'attachment_only' if has_attachments else 'none'
    if 'skill_required' in {reason, hint_reason}:
        return 'required'
    attachment_task = has_attachments and bool(_ATTACHMENT_OPERATION.search(request))
    external_requirement = bool(explicit_clause_numbers(request)) or requested(_EXTERNAL_CHECK)
    if explicit_project or external_requirement:
        return 'mixed' if attachment_task else 'required'
    if attachment_task:
        return 'attachment_only'
    if 'evidence_continuation' in {reason, hint_reason} and (
        decision.get('reuse_evidence') or decision.get('continued_run_id')
        or CONTINUATION.fullmatch(request.strip()) or FOLLOWUP.fullmatch(request.strip())
    ):
        return 'required'
    if reason == 'semantic_evidence' and not has_attachments:
        return 'required'
    if reason in {'business_evidence', 'semantic_evidence'} or hint_reason == 'business_evidence':
        return 'required' if confirmed and not has_attachments else 'optional'
    return 'none'
