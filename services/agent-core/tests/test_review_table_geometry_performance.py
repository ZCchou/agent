import pytest

from agent.review_table_context import table_contexts


class CountedField(dict):
    visits = 0
    def __getitem__(self, key):
        type(self).visits += 1
        return super().__getitem__(key)
    def get(self, key, default=None):
        type(self).visits += 1
        return super().get(key, default)


@pytest.mark.parametrize('geometry', [False, True])
@pytest.mark.parametrize('size', [256, 1024])
def test_bulk_neighbors_use_bounded_field_accesses_instead_of_target_table_scans(geometry, size):
    fields = []
    for column in range(size):
        field = CountedField(id=f't-0-r1-c{column}', kind='table_cell', text=f'label {column}')
        if geometry:
            field['table_grid'] = {'row_start': 1, 'row_end': 2,
                'column_start': column * 100, 'column_end': column * 100 + 1,
                'row_span': 1, 'grid_span': 1}
        fields.append(field)
    header = CountedField(id='t-0-r0-c0', kind='table_cell', text='wide original header')
    if geometry:
        header['table_grid'] = {'row_start': 0, 'row_end': 1,
            'column_start': 0, 'column_end': size * 100,
            'row_span': 1, 'grid_span': size * 100}
    fields.append(header)
    identities = [f't-0-r1-c{column}' for column in range(size)]
    CountedField.visits = 0
    result = table_contexts(fields, identities)
    visits = CountedField.visits
    assert visits <= 180 * (len(fields) + len(identities)), visits
    assert len(result) == size
    for column in (1, size // 2, size - 2):
        relation = result[identities[column]]['nearest_nonempty_anchor_ids']
        assert relation['left'] == identities[column - 1]
        assert relation['right'] == identities[column + 1]
        if geometry:
            assert relation['above'] == header['id']


@pytest.mark.parametrize('geometry', [False, True])
def test_batch_adjacency_matches_physical_rectangle_oracle(geometry):
    from random import Random
    from agent.review_table_context import _nearest_anchors_for_table
    random = Random(7821)
    occupied, candidates = set(), []
    for row in range(18):
        for column in range(24):
            if (row, column) in occupied:
                continue
            width = random.randint(1, min(4, 24 - column)) if geometry else 1
            height = random.randint(1, min(3, 18 - row)) if geometry else 1
            if any((r, c) in occupied for r in range(row, row + height)
                   for c in range(column, column + width)):
                width = height = 1
            occupied.update((r, c) for r in range(row, row + height) for c in range(column, column + width))
            field = {'id': f't-0-r{row}-c{column}', 'kind': 'table_cell',
                     'text': '' if random.randrange(5) == 0 else f'original {row}:{column}'}
            if geometry:
                field['table_grid'] = {'row_start': row, 'row_end': row + height,
                    'column_start': column, 'column_end': column + width,
                    'row_span': height, 'grid_span': width}
            candidates.append((field, ('t-0', row, column)))
    actual = _nearest_anchors_for_table(candidates, candidates)
    for target, (_, row, column) in candidates:
        expected = {}
        choices = {'left': [], 'above': [], 'right': []}
        target_grid = target.get('table_grid', {'row_start': row, 'column_start': column, 'column_end': column + 1})
        for source, (_, other_row, other_column) in candidates:
            if source['id'] == target['id'] or not source['text']:
                continue
            grid = source.get('table_grid', {'row_start': other_row, 'row_end': other_row + 1,
                'column_start': other_column, 'column_end': other_column + 1})
            if grid['row_start'] <= row < grid['row_end']:
                if grid['column_end'] <= column:
                    choices['left'].append((column - grid['column_end'], source['id']))
                if grid['column_start'] >= target_grid['column_end']:
                    choices['right'].append((grid['column_start'] - target_grid['column_end'], source['id']))
            if grid['row_end'] <= row and grid['column_start'] <= column < grid['column_end']:
                choices['above'].append((row - grid['row_end'], source['id']))
        for direction, values in choices.items():
            if values:
                expected[direction] = min(values)[1]
        assert actual[target['id']] == expected
