"""Conservative, deterministic references to the immediately preceding answer."""
import re


FOLLOWUP = re.compile(
    r'(?:请|请你|帮我|麻烦)?(?:把|将)?(?:上面|上述|以上|这段|这个|它|原文)?'
    r'(?:翻译|解释|说明|总结)(?:一下|下)?(?:成|为|到)?(?:中文|英文|汉语|英语)?(?:一下|下)?'
    r'|(?:please\s+)?(?:translate|explain|summarize)(?:\s+(?:that|this|it|the above))?'
    r'(?:\s+into\s+(?:chinese|english))?'
    r'|(?:请)?(?:好的?[，, ]*)?继续(?:填写|下一节|下一项)?'
    r'|(?:它|这(?:个|项|种)?|该设备|上述).{0,24}(?:通过|符合|满足|适用|是否|能否).{0,20}'
    r'|(?:请)?(?:把|将)?(?:刚才|上面|上述|这段|这个回答).{0,40}(?:保存|导出|写成).{0,160}', re.IGNORECASE)
EVIDENCE_LINK = re.compile(r'\]\(rag-evidence:([A-Za-z0-9_-]{1,128})\)')


def followup_context(messages, user_message_id):
    position = next(index for index, message in enumerate(messages) if message['id'] == user_message_id)
    current = messages[position]['content']
    if not FOLLOWUP.fullmatch(current.strip().rstrip('。.!！?？').strip()):
        return [], current
    history = [message for message in messages[:position] if message.get('role') != 'system_event']
    answer_position = len(history) - 1
    while answer_position >= 0 and history[answer_position].get('role') == 'user':
        unanswered = history[answer_position]['content'].strip().rstrip('。.!！?？').strip()
        if not FOLLOWUP.fullmatch(unanswered):
            return [], current
        answer_position -= 1
    if answer_position < 0 or history[answer_position].get('role') != 'assistant':
        return [], current
    answer = history[answer_position]['content']
    question = next((item['content'] for item in reversed(history[:answer_position]) if item.get('role') == 'user'), '')
    ids = list(dict.fromkeys(EVIDENCE_LINK.findall(answer)))[:8]
    query = '\n'.join((question[:700], answer[:1000], current[:200]))
    return ids, query
