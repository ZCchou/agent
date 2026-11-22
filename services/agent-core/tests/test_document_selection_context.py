import io

from docx import Document
from agent.document_inventory import inventory


def observe(document):
    stream = io.BytesIO()
    document.save(stream)
    fields = inventory(stream.getvalue(), include_selection_context=True)['fields']
    return {f['id']: f['selection_context'] for f in fields if 'selection_context' in f}, fields


def test_italic_continuation_stops_at_normal_prose():
    d = Document(); d.add_heading('Scope', 1)
    p = d.add_paragraph(); p.add_run('示例：').bold = True; p.add_run('sample').italic = True
    d.add_paragraph().add_run('continuation').italic = True
    d.add_paragraph('设备必须保留故障记录。')
    d.add_paragraph().add_run('formal italic requirement').italic = True
    a, _ = observe(d)
    assert a['p-2']['example_context']['anchor_id'] == 'p-1'
    assert 'example_context' not in a.get('p-3', {})
    assert 'example_context' not in a['p-4']


def test_standalone_anchor_across_subheading_table_but_not_next_main_heading():
    d = Document(); d.add_heading('References', 1); d.add_paragraph('示例：')
    d.add_heading('Regulations', 2); d.add_table(rows=1, cols=1).cell(0, 0).text = 'CCAR-29'
    d.add_heading('Industry', 2); d.add_table(rows=1, cols=1).cell(0, 0).text = 'DO-160G'
    d.add_heading('Actual plan', 1); d.add_table(rows=1, cols=1).cell(0, 0).text = 'formal'
    a, _ = observe(d)
    assert a['t-0-r0-c0']['example_context']['anchor_id'] == 'p-1'
    assert a['t-1-r0-c0']['example_context']['relation'] == 'candidate_section_context'
    assert 'example_context' not in a.get('t-2-r0-c0', {})


def test_inline_for_example_never_opens_section():
    d = Document(); d.add_heading('Tests', 1)
    d.add_paragraph('例如：可用A装置，但设备必须记录实际温度。')
    d.add_table(rows=1, cols=1).cell(0, 0).text = 'formal value'
    a, _ = observe(d)
    assert all('example_context' not in x for x in a.values())


def test_observations_do_not_change_text_ids_or_decisions():
    d = Document(); d.add_heading('Scope', 1); d.add_paragraph('示例：设备必须记录温度；正式要求见下文。')
    a, fields = observe(d)
    assert fields[1]['text'] == d.paragraphs[1].text
    assert set(a) <= {x['id'] for x in fields}
    assert not any('decision' in x for x in a.values())


def test_nested_table_retains_existing_identity():
    d = Document(); d.add_heading('Scope', 1); d.add_paragraph('Example:')
    cell = d.add_table(rows=1, cols=1).cell(0, 0)
    cell.add_table(rows=1, cols=1).cell(0, 0).text = 'sample'
    a, fields = observe(d)
    assert 't-0-r0-c0-t0-r0-c0' in a
    assert all(f['position'].get('cell_path', f['id']) == f['id'] for f in fields)


def test_mixed_format_has_exact_bounded_ranges_and_no_cell_flat_offsets():
    d = Document(); p = d.add_paragraph('正式要求。'); p.add_run('示例片段').italic = True
    d.add_table(rows=1, cols=1).cell(0, 0).text = '正式表格'
    a, _ = observe(d)
    assert a['p-0']['explicit_run_spans'] == [{'start': 5, 'end': 9, 'italic': True, 'bold': False}]
    assert 'example_context' not in a['p-0']
    assert 'explicit_run_spans' not in a.get('t-0-r0-c0', {})


def test_empty_positions_get_no_added_payload():
    d = Document(); d.add_paragraph(''); d.add_paragraph('正式内容')
    a, _ = observe(d)
    assert 'p-0' not in a


def test_default_inventory_and_all_source_fields_remain_identical():
    d = Document(); d.add_heading('Scope', 1); d.add_paragraph('示例：')
    d.add_table(rows=2, cols=2).cell(0, 0).text = 'example table'
    stream = io.BytesIO(); d.save(stream); payload = stream.getvalue()
    before = inventory(payload)
    after = inventory(payload, include_selection_context=True)
    assert all('selection_context' not in f for f in before['fields'])
    stripped = {**after, 'fields': [{k:v for k,v in f.items() if k != 'selection_context'} for f in after['fields']]}
    assert stripped == before


def test_neutral_nonempty_prose_adds_no_metadata():
    d = Document(); d.add_paragraph('实际设备应记录所有故障。')
    a, _ = observe(d)
    assert not a


def test_run_spans_are_bounded_with_truncation():
    d = Document(); p = d.add_paragraph()
    for _ in range(12):
        p.add_run('A').italic = True
    a, _ = observe(d)
    assert len(a['p-0']['explicit_run_spans']) == 8
    assert a['p-0']['run_spans_truncated'] is True


def test_useful_context_omits_zero_null_and_default_style_payload():
    d = Document(); d.add_paragraph().add_run('observed italic').italic = True
    a, _ = observe(d)
    assert 'heading_level' not in a['p-0']
    assert 'explicit_bold_chars' not in a['p-0']
    assert 'paragraph_styles' not in a['p-0']
