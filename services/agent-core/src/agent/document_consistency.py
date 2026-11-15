"""Source-backed fact candidates for a bounded cross-document review pass."""
import re

from agent.storage import CoreDataError


def normalized(value):
    return re.sub(r'\s+', '', value).casefold()


def review_facts(values, source_text):
    if not isinstance(values, list) or len(values) > 20:
        raise CoreDataError('document_review_invalid', '审阅事实清单无效。')
    result = []
    for value in values:
        if (not isinstance(value, dict) or set(value) != {'name', 'value', 'unit', 'condition'}
                or any(not isinstance(item, str) or len(item) > 256 for item in value.values())
                or not value['name'].strip() or not value['value'].strip()
                or normalized(value['value']) not in normalized(source_text)):
            raise CoreDataError('document_review_invalid', '审阅参数必须来自当前位置的原文。')
        result.append(value)
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
