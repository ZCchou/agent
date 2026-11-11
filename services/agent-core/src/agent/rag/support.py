"""Conservative excerpt checks, with professional interpretation left for review."""
import re

CONCEPTS = {
    '结构完整性': r'结构完整性|structural\s+integrity',
    '耐久性': r'耐久|endurance|durability',
    '强化振动': r'强化(?:振动|试验)|robust\s+vibration',
    '短时高量级': r'短时高量级|short.duration.high.level',
}


def assess_support(text, quotes):
    source = '\n'.join(quotes)
    uncovered = [label for label, pattern in CONCEPTS.items()
                 if re.search(pattern, text, re.I) and not re.search(pattern, source, re.I)]
    normalized = lambda value: re.sub(r'\s+', '', value)
    exact = bool(text.strip()) and normalized(text) in normalized(source)
    return {'status': 'exact_excerpt' if exact and not uncovered else 'needs_review',
            'uncovered_concepts': uncovered}


def review_issues(metadata):
    issues = []
    uncovered = sorted({concept for item in metadata for concept in item.get('support', {}).get('uncovered_concepts', [])})
    if uncovered:
        issues.append('所选摘录尚未覆盖 ' + '、'.join(uncovered) + '，条款支撑关系待核实。')
    elif any(item.get('support', {}).get('status') == 'needs_review' for item in metadata):
        issues.append('说明与所引条款的支持关系待专业复核。')
    if any(not item.get('revision') for item in metadata):
        issues.append('所引资料的修订元数据未标注，请核对受控版本。')
    return issues
