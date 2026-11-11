"""Task evidence decisions from user-authored intent and saved task state."""
import re

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


def resolve_attachment_roles(attachments, requested=None):
    requested = {} if requested is None else requested
    selected = {item['id'] for item in attachments}
    if not isinstance(requested, dict) or set(requested) - selected or any(not isinstance(role, str) or role not in {'template', 'reference'} for role in requested.values()):
        raise CoreDataError('attachment_purpose_invalid', '附件用途须对应本轮已授权的附件。')
    return {item['id']: requested.get(item['id'], item.get('purpose') or ('template' if item['name'].lower().endswith('.docx')
        and re.search(r'模板|template', item['name'], re.I) else 'reference')) for item in attachments}


def decide_evidence(text, *, mode='auto', previous_evidence=False, pending_evidence=False, skill_required=False):
    if mode not in {'auto', 'required'}:
        raise CoreDataError('invalid_rag_mode', 'Choose auto or required RAG mode.')
    normalized = text.strip().rstrip('。.!！?？').strip()
    export = bool(EXPORT.search(text) or re.search(r'(?:填写|回填|填好).{0,30}模板', text))
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
            'task_type': 'document_export' if export else 'evidence_answer' if required else 'chat'}
