import json

from agent.document_review import review_request
from agent.review_presentation import batch_references, present_section, review_target_labels


def test_numbered_and_legacy_citations_use_only_the_current_batch_and_leave_examples_alone():
    source = {'id': 'rag-real', 'title': '标准.pdf', 'locator': {'page': 18}, 'clause_number': '4.6.2',
              'quote_options': [{'quote_id': 'q-real', 'text': '保持条件。'}]}
    batch = {'fields': [{'id': 'p-1', 'text': '原文', 'chapter_path': ['减压试验', '试验目的']}],
             'freeform_review': True, 'request': '审阅原文'}
    messages, schema = review_request(batch, {'evidence': [source]})
    assert schema is None
    payload = json.loads(messages[-1]['content'])
    assert payload['sources']['evidence'][0]['quote_options'][0]['citation_number'] == 1
    assert 'citation_number' not in source['quote_options'][0]
    raw = ('## p-1\n**判断**：待补充。 [1](#cite-1) [rag-real|q-real]\n'
           '不匹配 [1](#cite-2) [rag-other|q-real] [9](#审阅出处-9) [rag-real|q-real|d]\n'
           '`[2](#cite-2)`\n```text\n[3](#cite-3)\n```')
    refs = batch_references(raw, [source])
    assert len(refs) == 1 and refs[0]['citation_number'] == 1
    result = present_section({'body': raw, 'verified_source_quotes': refs}, review_target_labels(batch), 5)
    assert '## 减压试验 / 试验目的 · 段落 2' in result
    assert result.count('[6](#审阅出处-6)') == 2
    assert result.count('##### 审阅出处 6') == 1
    assert '> 保持条件。' in result and 'PDF 第 18 页' in result
    assert result.count('〔引用待核〕') == 4
    assert '`[2](#cite-2)`' in result and '```text\n[3](#cite-3)\n```' in result


def test_restore_legacy_batch_keeps_facts_without_admitting_unknown_or_example_citations():
    old = {'id': 'rag-old', 'quote_id': 'q-a', 'quote': 'old excerpt', 'title': '旧文档',
           'clause': None, 'page': None}
    out = present_section({'body': '原始正文 [rag-old|q-a] [1](#cite-1)', 'verified_source_quotes': [old]}, {})
    assert '[1](#审阅出处-1)' in out and '〔引用待核〕' in out and '> old excerpt' in out
    assert 'PDF 第' not in out
    assert batch_references('`[1](#cite-1)`', []) == []


def test_inline_locations_use_only_the_current_fields_and_preserve_authored_examples_and_links():
    batch = {'fields': [{'id': 'p-55', 'text': '升温', 'chapter_path': ['温度变化']},
                        {'id': 'p-63', 'text': '减压', 'chapter_path': ['压力']},
                        {'id': 't-2-r0-c0', 'text': '单位'}], 'request': '审阅'}
    lines = [
        '附件 p-55 所述、附件p-63提及，参见 t-2-r0-c0。p-550 与 p-88 属于其他位置。',
        '表格列 | p-55 |；英文见 p-55. 原有文件 p-55.pdf 和 p-55-extra。',
        '保留 [p-55](docs/(sub)/p-63.md) 与 [p-55][p-63]。https://example.org/?key=p-55',
        '保留 `p-55`、``p-63 与 `p-55` 示例`` 及“原文 p-55”。',
        '> 引用段落 p-55',
        '示例：附件 p-55 所述。',
        '```md\np-55\n```',
        '    code p-55',
        '[位置]: docs/p-55.md',
    ]
    section = {'body': '\n'.join(lines), 'verified_source_quotes': []}
    result = present_section(section, review_target_labels(batch))
    assert '附件 温度变化 · 段落 56 所述、附件压力 · 段落 64提及，参见 表 3 · 第 1 行第 1 列。' in result
    assert 'p-550 与 p-88' in result
    assert '| 温度变化 · 段落 56 |' in result and '见 温度变化 · 段落 56.' in result
    assert 'p-55.pdf 和 p-55-extra' in result and all(line in result for line in lines[2:])
    assert present_section(section, review_target_labels({**batch, 'request':'保留位置ID'})) == section['body']
