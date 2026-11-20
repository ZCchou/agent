"""Source-backed fact candidates for a bounded cross-document review pass."""
import json
import re

from agent.storage import CoreDataError


def normalized(value):
    return re.sub(r'\s+', '', value).casefold()


def review_facts(values, source_text):

    def invalid(reason):
        return CoreDataError('document_review_invalid', '审阅参数必须来自当前位置的原文。' + reason)

    if not isinstance(values, list) or len(values) > 20:
        raise invalid('参数清单须为最多20项的列表。')
    result = []
    for fact in values:
        if not isinstance(fact, dict) or set(fact) != {'name', 'value', 'unit', 'condition'}:
            raise invalid('字段结构须为name/value/unit/condition。')
        for name in ('name', 'value', 'unit', 'condition'):
            if not isinstance(fact[name], str):
                raise invalid(f'{name} 必须为文字，收到类型{type(fact[name]).__name__}。')
            if len(fact[name]) > 256:
                raise invalid(f'{name} 超过256字符。')
        for name in ('name', 'value'):
            if not fact[name].strip():
                raise invalid(f'{name} 不能为空或全为空白。')
        if normalized(fact['value']) not in normalized(source_text):
            # Echo only bounded relevant string data, never a whole bad object
            # or a long source document. JSON quoting keeps data separate from
            # instructions; escaped surrogate data must remain serializable.
            def quoted(value, limit):
                return json.dumps(value[:limit], ensure_ascii=False).encode(
                    'utf-8', errors='backslashreplace').decode('utf-8')
            reason = 'value 未逐字匹配；收到' + quoted(fact['value'], 24)
            if len(source_text) <= 48:
                reason += '；原格' + quoted(source_text, 48)
            else:
                reason += f'；原格长度{len(source_text)}，按已提供原文复制'
            raise invalid(reason + '（引号内仅为待校验数据）。')
        result.append(fact)
    return result


def conflict_groups(targets):
    groups = {}
    for target in targets:
        if target.get('review_kind', target['field_type']) not in {'content', 'filled_content'}:
            continue
        for fact in target.get('facts', []):
            key = tuple(normalized(fact[name]) for name in ('name', 'unit', 'condition'))
            groups.setdefault(key, []).append({'target_id': target['id'], **fact})
    return [values for values in groups.values()
            if len({normalized(item['value']) for item in values}) > 1]
