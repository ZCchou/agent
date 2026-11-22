"""Present public review prose from authorized target fields and batch-scoped excerpts."""
import re
import unicodedata

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


def _single_verdict(block):
    original = block.splitlines(keepends=True)
    while original and not original[-1].strip():
        original.pop()
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
    last = original[-1].strip() if decisions and decisions[-1] == len(original)-1 else None
    indices = set(decisions)
    evidence = ''.join(line for i,line in enumerate(original) if i not in indices).strip('\r\n')
    # Lines quoted as examples or inside a fenced code block do not commit a verdict.
    return last, evidence, [original[i].strip() for i in decisions]


def _literal_field(text, names):
    pattern = r'^[ \t]{0,3}(?:[-*] )?(?:\*\*)?(?:' + names + r')(?:\*\*)?\s*[：:]\s*(.+)$'
    m = re.search(pattern, text, re.M | re.I)
    if not m:
        return ''
    return _MARKER.sub('',m[1]).strip()[:350]


def _uncommitted_prose(text):
    def demote(line):
        return re.sub(r'^([ \t]{0,3})(?:\*\*)?(判断|判定|结论|Verdict)(?:\*\*)?\s*[：:]',
                      lambda m: m[1]+('Model wording: ' if m[2].lower()=='verdict' else '模型原文用语：'),
                      line, flags=re.I)
    return _map_prose(text, lambda s:s, demote)


def _contract_field(text, names):
    match = re.search(r'^[ \t]*(?:[-*] )?(?:\*\*)?(?:' + names +
                      r')(?:(?:\*\*)?[ \t]*[：:](?:\*\*)?[ \t]*|(?:\*\*)?[ \t]*(?=\r?$))([^\r\n]*)',
                      text, re.M | re.I)
    if not match:
        return ''
    lines = [match[1].strip()] if match[1].strip() else []
    for line in text[match.end():].splitlines()[1:]:
        if not line.strip() or line.lstrip().startswith(('**', '#')):
            break
        if re.match(r'^(?:问题|建议|判断|依据摘录|原文摘录|对比量值|已查范围|finding|recommendation|verdict)\s*[:：]', line, re.I):
            break
        lines.append(line.strip())
    return '\n'.join(lines)


def _quote_text(value):
    value = unicodedata.normalize('NFC', _MARKER.sub('', value)).strip()
    for left, right in (('“', '”'), ('「', '」'), ('"', '"'), ('‘', '’'), ('`', '`')):
        if value.startswith(left) and value.endswith(right):
            value = value[1:-1]
            break
    return ' '.join(value.split())


def _normative_wording(text):
    # Only normalize equivalent obligations. Actors, actions, dates, objects,
    # polarity and exceptions must still match literally.
    text = text.strip().rstrip('。.')
    text = re.sub(r'严禁|禁止|不得', '不得', text)
    return re.sub(r'\b(?:shall not|must not)\b', 'must not', text, flags=re.I)


def _contains_excerpt(source, text):
    if not text:
        return False
    # PDF line wraps often insert whitespace between a Han character and a unit.
    def unwrap(value):
        return re.sub(r'(?<=[\u3400-\u9fff])\s+|\s+(?=[\u3400-\u9fff])', '', value)
    source, text = unwrap(source), unwrap(text)
    left = r'(?<![0-9A-Za-z_])' if re.match(r'[a-zA-Z]', text) else ''
    if re.match(r'\d', text):
        left = r'(?<![0-9A-Za-z_.,+−±－-])'
    right = r'(?![0-9a-zA-Z_])' if re.search(r'[0-9A-Za-z]$', text) else ''
    return re.search(left + re.escape(text) + right, source) is not None


_OMISSION = re.compile(r'缺少|缺失|遗漏|漏填|未(?:明确|规定|说明|列出|列明|给出|记录|填写|提供|覆盖)'
                       r'|\b(?:miss(?:ing|es)|omitted|absent|fails? to specify)\b', re.I)
_DOCUMENT_WIDE = re.compile(r'(?:整份|整个|全篇|全文|全套|整套)|\b(?:entire|whole)\s+(?:document|procedure|manual)\b', re.I)
_TENTATIVE_VIOLATION = re.compile(r'(?:可能|潜在|疑似|或许)(?:存在|构成|与[^。；]{1,24})?\s*(?:不符|违反|违规|矛盾|冲突)'
                                  r'|(?:缺乏|尚无|缺少)(?:直接|明确|足够|充分)?(?:依据|证据)'
                                  r'|(?:无法|不能|尚待)(?:确定|确认|证实|判定)'
                                  r'|\b(?:potential(?:ly)?|may|might)\s+(?:be\s+)?(?:conflict|violate|inconsistent|noncompliant)'
                                  r'|\b(?:insufficient evidence|cannot (?:confirm|determine))\b', re.I)
_NUMERIC_TERM = re.compile(r'数值|量值|限值|上限|下限|时限|时长|速率|时间界限|阈值|取值|换算|区间|'
                           r'\b(?:value|limit|rate|duration|range|threshold)s?\b', re.I)
_MISMATCH = re.compile(r'不一致|不符|不同|超出|超过|冲突|矛盾|偏低|偏高|低于|高于|'
                       r'\b(?:inconsistent|exceeds?|different|conflicts?|below|above|mismatch)\b', re.I)
_QUALITATIVE_DIFFERENCE = re.compile(
    r'(?:起算(?:点|事件)?|起点|阶段|对象|极性|动作|先后顺序)(?:的?定义)?(?:不一致|不同|错误|相反)'
    r'|\b(?:different|wrong|opposite)\s+(?:start(?:ing)? (?:point|event)|phase|actor|polarity|action)\b', re.I)
_BARE_CONCLUSION = re.compile(r'(?:本项|该项|原文)?(?:不符合(?:要求|标准)?|(?:要求|条件)未(?:得到)?满足|'
                              r'不满足(?:所引|标准)?要求|存在(?:问题|差异|冲突)|待补充)'
                              r'|(?:non[- ]?compliant|requirement not met|does not conform|needs information)', re.I)
_PREVENTED_ACTION = re.compile(r'(?:防止|避免|预防|禁止|严禁|不得|防|\b(?:avoid|prevent|prohibit|must not|shall not)\s+)'
                               r'\s*([^，。；;,.！？!?()（）\n]{2,80})', re.I)
_AFFIRMATIVE_ATTRIBUTION = re.compile(r'(?:(?:原文|附件|待审文件)(?:中)?(?:明确)?(?:要求|规定|允许)'
                                      r'|\b(?:the )?(?:source|document|original) (?:requires|allows)\s+)'
                                      r'\s*([^，。；;,.！？!?\n]{2,120})', re.I)


def _reversed_attribution(excerpt, finding):
    # A model cannot attribute the *same literal action* to the source as
    # required/allowed when that source asks for its prevention. Keep other
    # actions and scopes independent; this check does not establish compliance.
    actions = [m[1].strip() for m in _PREVENTED_ACTION.finditer(excerpt)]
    for match in _AFFIRMATIVE_ATTRIBUTION.finditer(finding):
        assertion = match[1].strip(' “「"')
        if any(assertion.startswith(action) for action in actions):
            return True
    return False


def _quantity_rows(compared, excerpt, requirement):
    from agent.review_quantities import grounded_operand, quantitative_subset
    rows = []
    for line in compared.splitlines():
        label, _, pair = re.sub(r'^[-*]\s+', '', line).partition('：')
        if not pair:
            label, _, pair = line.partition(':')
        if not pair:
            label, pair = '', line
        values = [_quote_text(v) for v in re.split(r'\s*[|｜]\s*', pair)]
        relation = None
        grounded = []
        if len(values) == 2:
            grounded = [grounded_operand(value, source) for value, source in zip(values, (excerpt, requirement))]
            if all(grounded):
                relation = quantitative_subset(*grounded)
        rows.append(dict(label=label.strip(), operands=grounded if all(grounded) else values,
                         relation={True:'within',False:'outside',None:'unverified'}[relation]))
    return rows


def _assess_adverse(record, checked, original, available_ids, reference_quotes, english):
    """Check provenance and the stated reason, without certifying applicability or blocking the batch."""
    from agent.review_quantities import quantities, quantitative_subset, single_quantitative_constraint

    excerpt = _quote_text(_contract_field(checked, '原文摘录|original excerpt'))
    requirement_line = _contract_field(checked, '依据摘录|requirement excerpt')
    requirement = _quote_text(requirement_line)
    record['model_verdict'] = record['verdict']

    def hold(reason, explanation):
        reason = explanation if english else reason
        record.update(verdict='无法判断', technical_status='needs_evidence', finding=reason, recommendation='')
        return reason

    if not _contains_excerpt(_quote_text(original), excerpt):
        return hold('待审原文摘录尚未定位到本项。', 'The target excerpt could not be located in this item.')
    if not any(_contains_excerpt(_quote_text(quote), requirement) for quote in reference_quotes(requirement_line)):
        return hold('待核实适用要求及支持该要求的本项引文。', 'Verify the requirement against a passage cited for this item.')
    if not record['finding'] or _BARE_CONCLUSION.fullmatch(record['finding'].strip().rstrip('.。')):
        return hold('需要说明原文与适用要求的具体差异。', 'Describe the specific discrepancy with the applicable requirement.')
    finding = _QUOTED_TEXT.sub('', record['finding'])
    if _reversed_attribution(excerpt, record['finding']):
        return hold('问题说明把原文要求防止的动作写成了应执行的动作，两者方向相反。请复核原意。',
                    'The finding attributes a preventive instruction to the source as a required action. Check its polarity.')
    if _TENTATIVE_VIOLATION.search(finding):
        return hold('该项仅提出可能的冲突或明确指出证据不足，仍需核实。', 'The finding states a possible conflict or insufficient evidence. Verify the discrepancy.')
    if record['verdict'] == '待补充' or _OMISSION.search(record['finding']):
        scope = _contract_field(checked, '已查范围|checked scope')
        scope_ids = list(dict.fromkeys(_POSITION.findall(scope)))
        if record['target_id'] not in scope_ids or not set(scope_ids).issubset(available_ids):
            return hold('缺项判断所依据的检查范围尚未定位。', 'The scope used to establish an omission could not be located.')
        record['checked_scope_ids'] = scope_ids
        if _DOCUMENT_WIDE.search(record['finding']):
            return hold('当前检查范围仅覆盖所列位置，尚不能确认整份文件缺项。', 'Only the listed positions were checked. Verify the omission across the document.')
    compared = _contract_field(checked, '对比量值|compared values')
    values = None
    if compared:
        comparisons = _quantity_rows(compared, excerpt, requirement)
        record['quantity_comparisons'] = comparisons
        outside = [entry for entry in comparisons if entry['relation'] == 'outside']
        if not outside:
            if any(entry['relation'] == 'unverified' for entry in comparisons):
                return hold('需在双方摘录中定位所比较的量值和边界。', 'Locate the compared values and bounds in both excerpts.')
            return hold('所列原文量值处于所引要求范围内，量值本身不足以证明冲突。', 'The values are within the cited constraints. Verify any other discrepancy.')
        if len(outside) < len(comparisons):
            reasons = [' → '.join(entry['operands']) for entry in outside]
            record['finding'] = ('Confirmed numeric differences (original → required): ' if english else
                                 '已定位的量值差异（原文 → 要求）：') + '；'.join(reasons)
            record['recommendation'] = ('Review the listed differences. Other proposed differences require verification.'
                if english else '请核对并修正所列量值差异；其余比较项另行复核。')
    elif (_MISMATCH.search(finding) and (_NUMERIC_TERM.search(finding) or len(quantities(finding)) >= 2)
          and not _QUALITATIVE_DIFFERENCE.search(finding)):
        values = [single_quantitative_constraint(value) for value in (excerpt, requirement)]
        if not all(values):
            return hold('请指明本项数值差异对应的两项量值及其约束。', 'Identify the two values and bounds that establish the stated numeric discrepancy.')
    if values:
        relation = quantitative_subset(*values)
        if relation is None:
            return hold('尚不能核对所列量值的单位、范围或边界，请复核。', 'Verify the units, range and bounds of the compared values.')
        if relation:
            return hold('所列原文量值处于所引要求范围内，量值本身不足以证明冲突。', 'The stated value falls within the cited constraint. Verify any other basis for a discrepancy.')
    if _normative_wording(excerpt) == _normative_wording(requirement):
        return hold('双方摘录表达同一要求，需核实另有何项适用要求构成冲突。', 'The excerpts state the same obligation. Verify any other basis for a discrepancy.')
    record.update(technical_status='evidence_linked', original_quote=excerpt, requirement_quote=requirement)
    return ''


def _review_projection(section, labels, source_offset, source_fields):
    raw = section['body']
    replacements, conflicts = [], []
    fields = {f['id']: f for f in source_fields}
    default = section.get('review_layout') == 'default'
    records = {}
    by_key, by_number = {}, {}
    quotes = {}
    for number, citation in enumerate(section.get('verified_source_quotes', []), source_offset+1):
        by_key[citation['id'], citation['quote_id']] = number
        quotes[number] = citation['quote']
        if citation.get('citation_number'):
            by_number[str(citation['citation_number'])] = number
    for key in section.get('target_ids', []):
        excerpt = (fields.get(key, {}).get('text') or '').strip()[:300]
        records[key] = dict(target_id=key, title=labels.get(key,key), source_excerpt=excerpt, verdict=None,
                            verdict_status='unmapped' if default else 'custom', technical_status='unverified',
                            finding='', recommendation='', source_numbers=[])

    def source_numbers(clean):
        numbers = []
        def find_sources(s):
            for marker in _MARKER.finditer(s):
                if marker['extra']:
                    continue
                n = (by_number.get(marker['num']) if marker['num'] and marker['num']==marker['dest']
                     else by_key.get((marker['source'],marker['quote'])))
                if n and n not in numbers:
                    numbers.append(n)
            return s
        _map_prose(clean, find_sources)
        return numbers

    def inspect_item(key, text):
        clean = re.sub(r'(?ms)^ {0,3}(`{3,}|~{3,})[^\n]*\n.*?(?:^ {0,3}\1[ \t]*(?:\n|$)|\Z)', '', text)
        records[key]['source_numbers'] = source_numbers(clean)
        return clean

    # Custom tables can map a row without ID-only headings. Only associate unshared,
    # exact raw-text spans with their actual target, never a batch-wide reference list.
    ranges = {}
    for item in section.get('target_delivery', []):
        if item['target_id'] not in records or item.get('status') != 'delivered':
            continue
        for span in item.get('body_ranges', []):
            start,end = span['start'],span['end']
            if 0 <= start < end <= len(raw) and raw[start:end] == span.get('text'):
                ranges.setdefault((start,end),set()).add(item['target_id'])
    for key in records:
        pieces = [raw[start:end] for (start,end), owners in ranges.items() if owners == {key} and not any(
            start < other_end and other_start < end and other_owners != {key}
            for (other_start,other_end),other_owners in ranges.items())]
        if pieces:
            inspect_item(key,'\n'.join(pieces))
    for _, start, end, key in _review_blocks(section):
        block = raw[start:end]
        checked = inspect_item(key, block)
        values = _verdict_lines(checked,key)
        record = records[key]
        if len(values) < 2:
            if not default:
                continue
            last, evidence, labels_seen = _single_verdict(block)
            english = any(_EN_DECISION.fullmatch(line.replace('**','')) for line in labels_seen) or (
                not labels_seen and not re.search(r'[\u3400-\u9fff]', checked))
            if last and section.get('public_output_state') == 'present' and len(values) == 1:
                record.update(verdict=next(iter(values)), verdict_status='submitted')
                record['finding'] = _literal_field(checked,'问题|发现|finding|issue')
                record['recommendation'] = _literal_field(checked,'修改建议|建议|recommendation|suggestion')
                if not record['recommendation']:
                    action = re.search(r'(?:^|[。！？]\s*)((?:建议(?:核对|补充|检查|修改)|需核实|需查阅)[^。！？\n]{1,200})', checked,re.M)
                    if action:
                        record['recommendation'] = _MARKER.sub('',action[1]).strip()
                hold_reason = ''
                if section.get('review_contract_version') == 1 and record['verdict'] in ('不符合', '待补充'):
                    hold_reason = _assess_adverse(
                        record, checked, fields.get(key, {}).get('text', ''),
                        section.get('checked_scope_ids', {}).get(key, ()),
                        lambda line: [quotes[n] for n in source_numbers(line)], english)
                elif section.get('review_contract_version') == 1 and record['verdict'] == '符合' and not record['source_numbers']:
                    hold_reason = ('Locate an applicable source passage for this item before concluding conformity.' if english
                                   else '本项未定位到可核对的依据摘录。请补充适用条款后再判断是否符合。')
                    record.update(model_verdict='符合', verdict='无法判断', technical_status='needs_evidence',
                                  finding=hold_reason, recommendation='')
                if hold_reason:
                    last = '**Verdict**: Undetermined' if english else '**判断**：无法判断'
                    evidence = hold_reason + ('\n\nModel observation (pending review):\n\n' if english else
                                              '\n\n模型观察（待复核）：\n\n') + evidence
                elif record.get('quantity_comparisons') and any(
                        part['relation'] != 'outside' for part in record['quantity_comparisons']):
                    refs = _contract_field(checked, '依据摘录|requirement excerpt')
                    evidence = (record['finding'] + '\n\n' + record['recommendation'] + '\n\n' + refs)
                elif record['verdict'] == '不符合':
                    warning = ('This is the model’s assessment. Verify the cited requirement and the specific conflict.'
                               if english else '这是模型判断。请复核适用依据和具体冲突。')
                    evidence += '\n\n'+warning
                presented = '\n'+last+'\n\n'+evidence+'\n\n'
            else:
                partial = section.get('public_output_state') != 'present'
                record.update(verdict='无法判断', verdict_status='partial' if partial else 'missing_final')
                reason = (('This item was interrupted.' if partial else 'No final verdict was submitted.')
                          if english else ('本项输出未完成。' if partial else '未收到该位置末行提交的有效判断。'))
                header = '**Verdict**: Undetermined' if english else '**判断**：无法判断'
                # Preserve explanations and literal examples, independently of an uncommitted label.
                presented = '\n'+header+'\n\n'+reason+'\n\n'+_uncommitted_prose(evidence)+'\n\n'
            replacements.append((start,end,presented))
            continue
        record.update(verdict='无法判断', verdict_status='conflict')
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
    for record in records.values():
        if not record['finding'] and record['source_excerpt']:
            record['finding'] = '附件原文：' + record['source_excerpt']
    return raw, conflicts, list(records.values())


def present_section(section, labels, source_offset=0, *, conflict_ids=None, source_fields=(), review_items=None):
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
    body, conflicts, records = _review_projection(section, labels, source_offset, source_fields)
    if conflict_ids is not None:
        conflict_ids.extend(conflicts)
    if review_items is not None:
        review_items.extend(records)
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


def prepare_public_section(section, labels, source_offset, *, source_fields=()):
    conflicts, records = [], []
    section['presented_body'] = present_section(section, labels, source_offset, conflict_ids=conflicts,
                                               review_items=records, source_fields=source_fields)
    section['verdict_conflicts'] = conflicts
    section['review_items'] = records
