import copy
import io

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from agent.document_inventory import inventory
from agent.review_table_context import table_contexts


def fields_of(document):
    data = io.BytesIO()
    document.save(data)
    return inventory(data.getvalue())['fields']


def test_merged_role_neighbors_are_physical_anchors_and_presented_first():
    document = Document()
    table = document.add_table(rows=4, cols=12)
    table.cell(0, 0).merge(table.cell(0, 11)).text = 'Form title'
    for column, label in [(0, 'Analyst'), (4, 'Technical responsible'), (8, 'Quality responsible')]:
        end = column + (1 if column == 8 else 0)
        table.cell(2, column).merge(table.cell(3, end)).text = label
        signature_column = end + 1
        table.cell(2, signature_column).text = 'Signature'
        table.cell(3, signature_column).text = 'Date'
    fields = fields_of(document)
    original = copy.deepcopy(fields)
    for identity, left, above in [('t-0-r2-c5', 't-0-r2-c4', 't-0-r0-c0'),
                                  ('t-0-r3-c5', 't-0-r2-c4', 't-0-r2-c5')]:
        context = table_contexts(fields, [identity])[identity]
        assert context['nearest_nonempty_anchor_ids']['left'] == left
        assert context['nearest_nonempty_anchor_ids']['above'] == above
        assert context['cells'][0]['id'] == left
        retained = {cell['id']: cell for cell in context['cells']}
        assert retained[left]['grid'] == {'row_start': 2, 'row_end': 4,
            'column_start': 4, 'column_end': 5, 'grid_span': 1, 'row_span': 2}
        assert context['relation_semantics'] == 'physical_neighbors_not_ownership'
        assert context['target_grid']['column_start'] == 5
    assert fields == original


def test_grid_before_and_nested_tables_keep_existing_ids_but_expose_true_grid():
    document = Document()
    table = document.add_table(rows=2, cols=4)
    table.cell(0, 2).text = 'Header'
    row = table.rows[1]
    row._tr.remove(row._tr.tc_lst[0])
    before = OxmlElement('w:gridBefore')
    before.set(qn('w:val'), '1')
    row._tr.get_or_add_trPr().append(before)
    row.cells[0].text = 'Left'
    row.cells[1].text = 'Target'
    nested = row.cells[2].add_table(rows=1, cols=2)
    nested.cell(0, 0).text = 'Nested left'
    nested.cell(0, 1).text = 'Nested target'
    fields = fields_of(document)
    target = next(f for f in fields if f['text'] == 'Target')
    assert target['id'] == 't-0-r1-c1'  # existing enumeration is deliberately preserved
    assert target['position']['column'] == 1
    context = table_contexts(fields, [target['id']])[target['id']]
    assert context['target_grid']['column_start'] == 2
    assert context['nearest_nonempty_anchor_ids']['above'] == 't-0-r0-c2'
    nested_target = next(f for f in fields if f['text'] == 'Nested target')
    nested_context = table_contexts(fields, [nested_target['id']])[nested_target['id']]
    assert all(c['id'].startswith('t-0-r1-c2-t0-') for c in nested_context['cells'])
    assert nested_context['cells'][0]['text'] == 'Nested left'


def test_unverified_merge_geometry_falls_back_without_invented_coverage():
    document = Document()
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = 'Original left'
    table.cell(0, 1).text = 'Target'
    # A grid inconsistent with row widths cannot prove any merged coverage.
    table._tbl.tblGrid.append(OxmlElement('w:gridCol'))
    fields = fields_of(document)
    context = table_contexts(fields, ['t-0-r0-c1'])['t-0-r0-c1']
    assert 'target_grid' not in context
    assert not any('grid' in cell for cell in context['cells'])
    assert context['nearest_nonempty_anchor_ids']['left'] == 't-0-r0-c0'
    assert context['relation_semantics'] == 'physical_neighbors_not_ownership'


def test_legacy_horizontal_merge_is_not_misrepresented_as_verified_grid():
    document = Document()
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = 'Left'
    table.cell(0, 1).text = 'Target'
    merge = OxmlElement('w:hMerge')
    merge.set(qn('w:val'), 'restart')
    table.cell(0, 0)._tc.get_or_add_tcPr().append(merge)
    fields = fields_of(document)
    assert not any('table_grid' in field for field in fields)
    context = table_contexts(fields, ['t-0-r0-c1'])['t-0-r0-c1']
    assert 'target_grid' not in context
    assert context['nearest_nonempty_anchor_ids']['left'] == 't-0-r0-c0'
