"""Present public review prose from authorized target fields and batch-scoped excerpts."""
import re

from agent.rag.grounding import plain_markdown


_MARKER = re.compile(r'\[(?P<num>[1-9]\d*)\]\(#cite-(?P<dest>[1-9]\d*)\)|'
                     r'\[(?P<source>[\w-]+)\\?\|(?P<quote>q-[\w-]+)(?P<extra>\\?\|[^\]\n]*)?\]')
_INTERNAL_LINK = re.compile(r'\[[^\]]*\]\((?:rag-evidence:|artifact:|file:|codex:)[^)]*\)', re.I)
_POSITION = re.compile(r'(?<![A-Za-z0-9_/#.:@\\-])(?:p-\d+|t-\d+(?:-r\d+-c\d+-t\d+)*-r\d+-c\d+)'
                       r'(?![A-Za-z0-9_/#:@\\-]|\.[A-Za-z0-9_])')
_LITERAL_SPAN = re.compile(r'(?<!`)(`+)(?!`).*?(?<!`)\1(?!`)|“[^”\n]*”|「[^」\n]*」|'
                           r'\b[a-z][\w+.-]*://[^\s<>，。；]+|<[^>\n]+>|\[', re.I)


def _end_of_group(text, start, opening, closing):
    depth = 1
    index = start + 1
    while index < len(text):
        char = text[index]
        index += 1
        if char == '\\':
            index += 1
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if not depth:
                break
    return min(index, len(text))


def _present_positions(line, labels):
    if not labels or not _POSITION.search(line) or re.match(
            r'^(?: {4}|\t|\s*>|\s*\[[^\]]+\]:|\s*(?:\*\*)?(?:示例|例子|例如|example)\s*[:：])', line, re.I):
        return line

    def substitute(text):
        return _POSITION.sub(lambda m: plain_markdown(labels[m[0]]) if m[0] in labels else m[0], text)

    # Walk literal spans before resolving prose IDs. Markdown labels and destinations
    # may contain nested brackets, escaped delimiters and parentheses.
    parts, start = [], 0
    while match := _LITERAL_SPAN.search(line, start):
        parts.append(substitute(line[start:match.start()]))
        end = match.end()
        if match[0] == '[':
            end = _end_of_group(line, match.start(), '[', ']')
            if line[end:end + 1] == '(':
                end = _end_of_group(line, end, '(', ')')
        parts.append(line[match.start():end])
        start = end
    parts.append(substitute(line[start:]))
    return ''.join(parts)


def numbered_sources(sources):
    index = 0
    numbered = []
    for source in sources:
        options = []
        for option in source.get('quote_options', []):
            index += 1
            options.append({**option, 'citation_number': index})
        numbered.append({**source, 'quote_options': options})
    return numbered


def _map_prose(body, transform, line_transform=None):
    """Leave fenced examples and inline code intact."""
    lines, fence = [], None
    for line in body.splitlines(keepends=True):
        delimiter = re.match(r'^ {0,3}(`{3,}|~{3,})', line)
        if delimiter:
            token = delimiter[1]
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence) and not line[delimiter.end():].strip():
                fence = None
            lines.append(line)
        elif fence:
            lines.append(line)
        else:
            spans = re.split(r'(`+[^`\n]*`+)', line)
            converted = ''.join(transform(part) if i % 2 == 0 else part for i, part in enumerate(spans))
            lines.append(line_transform(converted) if line_transform else converted)
    return ''.join(lines)


def batch_references(body, sources):
    """Resolve only markers actually asserted in prose to options shown to this invocation."""
    by_number, by_key = {}, {}
    for source in numbered_sources(sources):
        for option in source['quote_options']:
            entry = {'id': source['id'], 'quote_id': option['quote_id'], 'quote': option['text'],
                     'title': source.get('title') or source.get('source') or '',
                     'clause': source.get('clause_number') or '', 'page': (source.get('locator') or {}).get('page')}
            by_key[source['id'], option['quote_id']] = entry
            by_number[str(option['citation_number'])] = entry
    found = {}

    def collect(text):
        for marker in _MARKER.finditer(text):
            if marker['extra']:
                continue
            number = marker['num']
            reference = by_number.get(number) if number and number == marker['dest'] else by_key.get(
                (marker['source'], marker['quote']))
            if reference:
                saved = found.setdefault((reference['id'], reference['quote_id']), dict(reference))
                if number:
                    saved['citation_number'] = int(number)
        return text

    _map_prose(body, collect)
    return list(found.values())


def _target_label(field):
    path = field.get('chapter_path') or []
    text = ' '.join((field.get('text') or '').split())
    target = field['id']
    paragraph = re.fullmatch(r'p-(\d+)', target)
    cell = re.fullmatch(r't-(\d+)-r(\d+)-c(\d+)', target)
    if paragraph and path:
        return ' / '.join(' '.join(str(part).split()) for part in path[-2:]) + ' · 段落 ' + str(int(paragraph[1]) + 1)
    if cell:
        table, row, column = (int(value) + 1 for value in cell.groups())
        return f'表 {table} · 第 {row} 行第 {column} 列'
    return (text[:45] + ('…' if len(text) > 45 else '')) or target


def review_target_labels(batch):
    if re.search(r'(?:保留|列出|显示|输出)\s*(?:原始|内部|真实)?(?:位置|目标|段落)?\s*(?:ID|编号)'
                 r'|(?:include|show|keep)\s+(?:the\s+)?(?:raw\s+)?(?:target|position)\s+ids?', batch.get('request', ''), re.I):
        return {field['id']: field['id'] for field in batch['fields']}
    return {field['id']: _target_label(field) for field in batch['fields']}


_VERDICT = r'(不符合|无法判断|待补充|符合)'
_DECISION_LINE = re.compile(r'^(?:最终)?(?:判断|判定|结论)\s*[：:]\s*' + _VERDICT + r'(?=[。；，,;\s]|$)')
_REVISED_DECISION = re.compile(r'^(?:(?:因此|所以|综上所述|综上|更正)[，,:：]\s*)?'
    r'(?:(?P<subject>本项|该项|本段|p-\d+|t-\d+(?:-r\d+-c\d+-t\d+)*-r\d+-c\d+)\s*的?\s*)?'
    r'(?:最终)?(?:判断|判定|结论)\s*(?:应(?:该|当)?[为是]|更正为|改判为|修正为|调整为)\s*'
    + _VERDICT + r'[。.!！\s]*$')
_EXPLICIT_REVISION = re.compile(r'^(?:修正|更正|改判后)(?:判断|判定|结论)\s*[：:]\s*'
                                r'(?:(?:判断|判定|结论)\s*[：:]\s*)?' + _VERDICT + r'[。.!！\s]*$')
_EN_VERDICTS = {'conforms':'符合', 'does not conform':'不符合', 'undetermined':'无法判断',
                'compliant':'符合', 'non-compliant':'不符合',
                'needs information':'待补充', 'unable to determine':'无法判断'}
_EN_LABELS = '|'.join(map(re.escape, _EN_VERDICTS))
_EN_DECISION = re.compile(r'^(?:(?:final|revised|corrected)\s+)?verdict:\s*'
                          r'(' + _EN_LABELS + r')\.?$', re.I)
_QUOTED_TEXT = re.compile(r'“[^”]*(?:”|$)|「[^」]*(?:」|$)|‘[^’]*(?:’|$)|"[^"]*(?:"|$)|`[^`]*(?:`|$)')


def _verdict_lines(body, target):
    verdicts = set()
    for line in body.splitlines():
        # These lines assert the writer's own verdict. Comparisons, conditions,
        # quoted passages, questions and examples are not adjudications.
        clean = line.strip().replace('**', '')
        if clean.startswith(('>', '`', '“', '「', '例如', '示例')):
            continue
        # A correction may end a long paragraph that started as a question or cited source.
        # Look only for an explicit, declarative correction in its own unquoted sentence.
        for sentence in re.split(r'(?<=[。！？!?；;])', _QUOTED_TEXT.sub(' quoted ', clean)):
            revision = _EXPLICIT_REVISION.fullmatch(sentence.strip())
            if revision:
                verdicts.add(revision[1])
        if clean.startswith(('如果', '若')) or re.search(r'[？?]', clean):
            continue
        english = _EN_DECISION.fullmatch(clean)
        if english:
            verdicts.add(_EN_VERDICTS[english[1].lower()])
            continue
        match = _DECISION_LINE.match(clean)
        if match:
            verdicts.add(match[1])
        else:
            match = _REVISED_DECISION.fullmatch(clean)
            if match and match['subject'] in (None, '本项', '该项', '本段', target):
                verdicts.add(match[2])
    return verdicts


def _review_blocks(section):
    """Only partition when distinct, acknowledged source headings account for this batch."""
    raw = section['body']
    wanted = set(section.get('target_ids', []))
    delivered = {row['target_id']: {r['start'] for r in row.get('body_ranges', [])}
                 for row in section.get('target_delivery', []) if row.get('status') == 'delivered'}
    if not wanted or not delivered:
        return []
    boundaries = []
    offset, fence = 0, None
    for line in raw.splitlines(keepends=True):
        if delimiter := re.match(r'^ {0,3}(`{3,}|~{3,})', line):
            token = delimiter[1]
            if not fence:
                fence = token
            elif token.startswith(fence) and not line[delimiter.end():].strip():
                fence = None
        elif not fence:
            text = re.sub(r'^ {0,3}#{1,6}\s+', '', line).strip().strip('*`')
            if _POSITION.fullmatch(text):
                if text not in wanted:
                    return []
                boundaries.append((offset,offset+len(line),text))
        offset += len(line)
    if len(boundaries) != len(wanted) or {row[2] for row in boundaries} != wanted:
        return []
    return [(head, start, boundaries[i+1][0] if i+1 < len(boundaries) else len(raw), key)
            for i,(head,start,key) in enumerate(boundaries) if head in delivered.get(key, set())]


_STANDALONE_VERDICT = re.compile(
    r'^[ \t]{0,3}(?:\*\*(?:判断|判定|结论)\*\*|判断|判定|结论)[：:]\s*'
    r'(?:\*\*)?(?:不符合|符合|待补充|无法判断)(?:\*\*)?[。.]?[ \t]*$'
    r'|^[ \t]{0,3}(?:\*\*Verdict\*\*|Verdict):[ \t]*(?:\*\*)?'
    r'(?:' + _EN_LABELS + r')(?:\*\*)?\.?[ \t]*$', re.I)


def _lead_with_final_verdict(block):
    original = block.splitlines(keepends=True)
    while original and not original[-1].strip():
        original.pop()
    if len(original) < 2 or not _STANDALONE_VERDICT.fullmatch(original[-1].rstrip('\r\n')):
        return block
    decisions = []
    fence = None
    for index, line in enumerate(original):
        if delimiter := re.match(r'^ {0,3}(`{3,}|~{3,})', line):
            if not fence:
                fence = delimiter[1]
            elif delimiter[1].startswith(fence) and not line[delimiter.end():].strip():
                fence = None
        elif not fence and _STANDALONE_VERDICT.fullmatch(line.rstrip('\r\n')):
            decisions.append(index)
    if decisions != [len(original)-1]:
        return block
    evidence = ''.join(original[:-1]).strip('\r\n')
    if not evidence.strip():
        return block
    return '\n' + original[-1].strip() + '\n\n' + evidence + '\n\n'


def resolve_conflicting_verdicts(section):
    raw = section['body']
    replacements, conflicts = [], []
    for _, start, end, key in _review_blocks(section):
        block = raw[start:end]
        # Preserve offsets but remove fenced samples before interpreting verdicts.
        checked = re.sub(r'(?ms)^ {0,3}(`{3,}|~{3,})[^\n]*\n.*?(?:^ {0,3}\1[ \t]*(?:\n|$)|\Z)', '', block)
        if len(_verdict_lines(checked, key)) < 2:
            if section.get('review_layout') == 'default' and section.get('public_output_state') == 'present':
                presented = _lead_with_final_verdict(block)
                if presented != block:
                    replacements.append((start, end, presented))
            continue
        english = any(_EN_DECISION.fullmatch(line.strip().replace('**','')) for line in checked.splitlines())
        note = ('\n**Verdict**: Undetermined\n\nThe model gave conflicting verdicts for this item. '
                'Check the source text and the applicable requirement.' if english else
                '\n**判断**：无法判断\n\n模型对同一位置给出了相互冲突的判断。请对照附件原文与适用条款复核。')
        handles = list(dict.fromkeys(m[0] for m in _MARKER.finditer(checked)))
        if handles:
            note += ('\n\nReferences: ' if english else '\n\n本项引用：') + ' '.join(handles)
        replacements.append((start, end, note+'\n\n'))
        conflicts.append(key)
    for start, end, replacement in reversed(replacements):
        raw = raw[:start] + replacement + raw[end:]
    return raw, conflicts


def present_section(section, labels, source_offset=0, *, conflict_ids=None):
    """Build reader-facing Markdown while leaving raw checkpoint text and offsets untouched."""
    citations = section.get('verified_source_quotes', [])
    by_key = {(c['id'], c['quote_id']): c for c in citations}
    by_number = {str(c['citation_number']): c for c in citations if c.get('citation_number')}
    ordinal = {(c['id'], c['quote_id']): i for i, c in enumerate(citations, source_offset + 1)}

    def prose(text):
        def replace(marker):
            if marker['extra']:
                return '〔引用待核〕'
            number = marker['num']
            citation = by_number.get(number) if number and number == marker['dest'] else by_key.get(
                (marker['source'], marker['quote']))
            if citation is None:
                return '〔引用待核〕'
            index = ordinal[citation['id'], citation['quote_id']]
            return f'[{index}](#审阅出处-{index})'

        # The anchor namespace used by the final report belongs to the renderer.
        text = re.sub(r'\[[^\]\n]*\]\(#审阅出处-[^)]*\)', '〔引用待核〕', text)
        text = re.sub(r'^( {0,3}#{1,6}\s+)审阅出处\s+\d+\s*$', '模型提供的来源说明\n', text)
        heading = re.fullmatch(r'( {0,3}#{1,6}\s+)(\S+)([ \t]*)(\r?\n)?', text)
        if heading and heading[2] in labels:
            text = heading[1] + plain_markdown(labels[heading[2]]) + (heading[4] or '')
        return _MARKER.sub(replace, text)

    # Historical public reports preserved unsafe links literally even inside example blocks.
    body, conflicts = resolve_conflicting_verdicts(section)
    if conflict_ids is not None:
        conflict_ids.extend(conflicts)
    body = _INTERNAL_LINK.sub(lambda m: plain_markdown(m[0]), body)
    parts = [_map_prose(body, prose, lambda line: _present_positions(line, labels))]
    for citation in citations:
        index = ordinal[citation['id'], citation['quote_id']]
        label = ' · '.join(' '.join(str(item).split()) for item in (citation['title'],
            ('§' + str(citation['clause'])) if citation['clause'] else '',
            f"PDF 第 {citation['page']} 页" if citation['page'] is not None else '') if item)
        excerpt = '\n'.join('> ' + plain_markdown(line) for line in citation['quote'].splitlines())
        parts.extend(['', f'##### 审阅出处 {index}', '', '**来源**　' + plain_markdown(label or '已核对摘录'), '', excerpt])
    return '\n'.join(parts)


def prepare_public_section(section, labels, source_offset):
    conflicts = []
    section['presented_body'] = present_section(section, labels, source_offset, conflict_ids=conflicts)
    section['verdict_conflicts'] = conflicts
