"""Index review findings by the original target and its own verified excerpt numbers."""
import html

from agent.rag.grounding import plain_markdown


def issue_rows(sections, fields):
    by_target = {}
    sources = {}
    for section in sections:
        for ref in section.get('verified_source_quotes', []):
            sources[len(sources)+1] = {k:ref.get(k) for k in ('title','clause','page')}
        for item in section.get('review_items', ()):
            by_target.setdefault(item['target_id'], []).append(item)
    rows = []
    for field in fields:
        key = field['id']
        items = by_target.get(key, [])
        submitted = [v for v in items if v['verdict_status'] == 'submitted' and v.get('verdict')]
        item = dict((submitted or items or [{}])[-1])
        decisions = {v['verdict'] for v in submitted}
        if len(decisions) > 1 or any(v['verdict_status'] == 'conflict' for v in items):
            item.update(verdict='无法判断', verdict_status='conflict', finding='', recommendation='',
                        source_numbers=sorted({n for v in items for n in v.get('source_numbers', [])}))
        verdict = item.get('verdict') if item.get('verdict_status') == 'submitted' else '无法判断'
        if verdict == '符合':
            continue
        status = item.get('verdict_status') or 'unmapped'
        reason = {'conflict':'同一位置的判断相互冲突。', 'partial':'本项正文未完整提交。',
                  'missing_final':'未取得可确认的最终判断。', 'unmapped':'未取得能对应此位置的审阅判断。',
                  'custom':'需按原报告的自定义格式复核判断。'}.get(status)
        numbers = list(dict.fromkeys(item.get('source_numbers', [])))
        rows.append({'target_id':key, 'title':item.get('title') or key,
                     'finding':reason or item.get('finding') or '问题说明见本项正文。',
                     'verdict':verdict, 'source_excerpt':field.get('text') or '',
                     'recommendation':item.get('recommendation') if not reason else None,
                     'source_numbers':numbers, 'evidence':[{'number':n, **sources[n]} for n in numbers if n in sources],
                     'verdict_status':status})
    return rows


def issue_table(rows):
    lines = ['## 问题与待核事项汇总', '']
    groups = {'已确认问题': [], '待补充材料': [], '无法判断': []}
    for row in rows:
        group = {'不符合': '已确认问题', '待补充': '待补充材料'}.get(row['verdict'], '无法判断')
        groups[group].append(row)
    lines.append(' · '.join(f'{label} {len(entries)} 项' for label, entries in groups.items()))
    if not rows:
        return '\n'.join(lines + ['', '本次已审阅位置中没有列入汇总的问题或待核事项。'])
    for label, entries in groups.items():
        if entries:
            lines.extend(['', '### ' + label, ''])
            lines.extend(_table_lines(entries))
    return '\n'.join(lines)


def _table_lines(rows):
    def cell(value):
        return plain_markdown(html.escape(' '.join(value.split()), quote=False))
    yield '| 问题项及位置 | 判断 | 原文 | 建议 | 证据 |'
    yield '| --- | --- | --- | --- | --- |'
    for row in rows:
        evidence = {ref['number']:ref for ref in row.get('evidence', [])}
        refs = []
        for n in row['source_numbers']:
            if type(n) is not int or n < 1:
                continue
            ref = evidence.get(n, {})
            label = ' · '.join(str(v) for v in (ref.get('title'), '§' + str(ref['clause']) if ref.get('clause') else None,
                f"PDF 第 {ref['page']} 页" if ref.get('page') else None) if v)
            refs.append((cell(label)+' ' if label else '') + f'[{n}](#审阅出处-{n})')
        recommendation = row['recommendation'] or ('复核本项原文及适用依据后再判定。'
            if row['verdict'] == '无法判断' else '原审阅未给出具体建议。')
        yield '| ' + ' | '.join([cell(row['title'] + ' · ' + row['finding']), cell(row['verdict']),
            cell(row['source_excerpt']) or '未取得原文', cell(recommendation), '；'.join(refs) or '无已核对的本项摘录']) + ' |'
