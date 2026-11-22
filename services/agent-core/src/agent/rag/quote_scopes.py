"""Exact note/caption units and explicit illustration locator checks."""
import re
import unicodedata

_DASHES = str.maketrans({char: '-' for char in '‐‑‒–—−－'})
_NUMBER = r'(?:[0-9]+|[A-Z])(?:-[0-9]+)+'
_REFERENCE = re.compile(r'(图|表|fig(?:ure)?\.?|table)\s*(' + _NUMBER + r')(?![A-Za-z0-9-])', re.I)
_CAPTION = re.compile(r'^(图|表)\s*(' + _NUMBER + r')\s+([^，；。,:;\r\n]{1,160})$', re.I)
_FIRST_NOTE = re.compile(r'(?m)^\s*注\s*:\s*1[)）]')
_LOCAL_PREFIX = re.compile(r'(?:模板|本大纲)(?:中|的|所示|内|自有|\s)*$|\btemplate(?:\s|\x27s)*$', re.I)


def _normalized(text):
    return unicodedata.normalize('NFKC', text).translate(_DASHES)


def illustration_references(text, *, skip_local=False):
    normalized = _normalized(text)
    labels = []
    for match in _REFERENCE.finditer(normalized):
        if skip_local and _LOCAL_PREFIX.search(normalized[max(0, match.start()-40):match.start()]):
            continue
        kind = '图' if match[1].lower().startswith('fig') else '表' if match[1].lower() == 'table' else match[1]
        label = kind + match[2].upper()
        if label not in labels:
            labels.append(label)
    return labels


def caption_lines(text):
    result, offset = [], 0
    for line in text.splitlines(keepends=True):
        normalized = _normalized(line).strip()
        match = _CAPTION.fullmatch(normalized)
        if match and len(illustration_references(normalized)) == 1:
            result.append((offset, offset + len(line), match[1] + match[2].upper()))
        offset += len(line)
    return result


def note_caption_groups(text):
    """Return complete contiguous units, never infer front-caption ownership."""
    captions = caption_lines(text)
    if len(captions) < 2 or len({item[2] for item in captions}) != len(captions):
        return []
    result, start = [], 0
    for index, (caption_start, end, label) in enumerate(captions):
        body = _normalized(text[start:caption_start])
        first = _FIRST_NOTE.search(body)
        if first is None or len(re.findall(r'(?m)^\s*注\s*:', body)) != 1:
            return []
        prefix = [line.strip() for line in body[:first.start()].splitlines() if line.strip()]
        if prefix and (index or any(not re.fullmatch(r'\d+(?:\s*-\s*\d+)?', line) for line in prefix)):
            return []
        notes = body[first.start():]
        if re.search(r'(?m)^\s*\d+(?:\.\d+)+\s+\S', notes):
            return []
        numbers = [1, *(int(value) for value in re.findall(r'(?m)^\s*(\d+)[)）]', body[first.end():]))]
        if numbers != list(range(1, len(numbers)+1)):
            return []
        result.append((start, end, label))
        start = end
    if text[start:].strip():
        return []
    result[-1] = (result[-1][0], len(text), result[-1][2])
    return result


def validate_illustration_locators(text, metadata):
    claimed = illustration_references(text, skip_local=True)
    if not claimed:
        return
    available = set()
    for source in metadata:
        scope = source.get('quote_scope')
        if isinstance(scope, dict) and scope.get('kind') == 'numbered_notes_before_caption':
            for label in scope.get('illustration_labels', []):
                available.update(illustration_references(str(label)))
            continue  # A cross-reference inside this note is not another owner.
        available.update(illustration_references(str(source.get('quote') or '')))
        locator = source.get('locator')
        labels = locator.get('illustration_labels', []) if isinstance(locator, dict) else []
        # A page-wide multi-caption label list does not identify the selected
        # note group. Scoped citations carry one label; exact quote text can
        # independently contain more than one in a deliberate comparison.
        if isinstance(labels, list) and len(labels) == 1:
            available.update(illustration_references(str(labels[0])))
    missing = [label for label in claimed if label not in available]
    if missing:
        from agent.storage import CoreDataError
        raise CoreDataError('rag_citation_invalid',
            '图/表号与所选摘录范围不一致：' + '、'.join(missing[:8]) + '；请核对各图自己的注释。',
            {'locator_issue': 'illustration_scope_mismatch', 'claimed': claimed[:16], 'available': sorted(available)[:16]})
