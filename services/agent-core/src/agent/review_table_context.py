"""Bounded, readonly original-cell context; never additional review targets."""
from bisect import bisect_left, bisect_right
from itertools import groupby
import re


_TABLE_DIRECTIONS = (
    ('following', re.compile(r'下表|表格如下|\b(?:following table|table below)\b', re.I)),
    ('preceding', re.compile(r'上表|(?:上述|前述|以上)\s*(?:表格?|步骤|[a-z\d]+\s*(?:至|到|[-–~～])\s*[a-z\d]+)'
                             r'|\b(?:preceding table|table above)\b', re.I)),
)
_CONDITION = re.compile(r'适用|类别|[A-Z]\s*类|参数(?:和剖面)?\s*[：:]|要求\s*[：:]|\b(?:category|applicability)\b', re.I)


def paragraph_table_references(fields, target_ids):
    """Resolve explicit deictic references only within a contiguous source section, in XML order."""
    wanted = set(target_ids)
    result = {}
    for section, section_fields in groupby(fields, key=lambda field: field.get('section_id')):
        if not section:
            continue
        blocks = []
        conditions = []
        refs = []
        for field in section_fields:
            key = field['id']
            table = re.match(r'^(t-\d+)-r\d+-c\d+', key)
            if table:
                table_id = table[1]
                if not blocks or blocks[-1][0] != table_id:
                    blocks.append((table_id, []))
                # Nested tables are separate semantic grids.
                if table.end() == len(key):
                    blocks[-1][1].append(field)
                continue
            text = field.get('text', '').strip()
            if not text or not re.fullmatch(r'p-\d+', key):
                continue
            index = len(blocks)
            blocks.append((None, field))
            if _CONDITION.search(text):
                conditions.append(index)
            if key in wanted:
                directions = [name for name, pattern in _TABLE_DIRECTIONS if pattern.search(text)]
                if directions:
                    refs.append((key, index, directions))
        for key, index, directions in refs:
            entry = {'direction': directions, 'tables': [], 'section_context': {}}
            for direction in directions:
                step = 1 if direction == 'following' else -1
                for distance in range(1, 8):
                    pos = index + step * distance
                    if not 0 <= pos < len(blocks):
                        break
                    table_id, members = blocks[pos]
                    if table_id:
                        cells = {}
                        clipped = False
                        remaining = 1500
                        for member in members:
                            text = member.get('text', '').strip()
                            if not text:
                                continue
                            if len(cells) >= 32 or remaining <= 0:
                                clipped = True
                                break
                            excerpt = text[:min(180, remaining)]
                            cells[member['id']] = excerpt
                            remaining -= len(excerpt)
                            clipped |= len(excerpt) < len(text)
                        entry['tables'].append({'id': table_id, 'direction': direction, 'cells': cells,
                                                'excerpt_only': clipped})
                        break
            before = bisect_left(conditions, index)
            # Only nearby antecedent context, in source order. Its claims belong to the attachment.
            for pos in conditions[max(0, before-4):before]:
                if index-pos <= 14:
                    field = blocks[pos][1]
                    entry['section_context'][field['id']] = field['text'][:180]
            result[key] = entry
    return result


def _cell(field):
    identity = field['id']
    match = re.fullmatch(r'(.+)-r(\d+)-c(\d+)', identity)
    if not match or not field.get('kind', '').startswith('table_'):
        return None
    return match.group(1), int(match.group(2)), int(match.group(3))


def _nearest_anchors_for_table(candidates, targets):
    """Batch physical adjacency: sorted row indexes and an above-anchor sweep.

    Vertical coverage expands only across original XML continuation rows. Wide
    gridSpan intervals never expand into columns; above queries compress just
    the requested target columns.
    """
    result = {field['id']: {} for field, _ in targets}
    ordinary_rows, ordinary_columns, geometry_rows, rectangles = {}, {}, {}, []
    for field, (_, row, column) in candidates:
        if not field.get('text', '').strip():
            continue
        identity, grid = field['id'], field.get('table_grid')
        if grid:
            rectangles.append((grid['row_end'], identity, grid))
            for covered_row in range(grid['row_start'], grid['row_end']):
                geometry_rows.setdefault(covered_row, []).append((identity, grid))
        else:
            ordinary_rows.setdefault(row, []).append((column, identity))
            ordinary_columns.setdefault(column, []).append((row, identity))
    for values in (*ordinary_rows.values(), *ordinary_columns.values()):
        values.sort()
    geometry_index = {}
    for row, anchors in geometry_rows.items():
        geometry_index[row] = (
            sorted((grid['column_end'], identity) for identity, grid in anchors),
            sorted((grid['column_start'], identity) for identity, grid in anchors))
    geometry_targets = []
    for field, (_, row, column) in targets:
        identity, grid = field['id'], field.get('table_grid')
        relation = result[identity]
        if grid:
            geometry_targets.append((grid['row_start'], grid['column_start'], identity))
            ends, starts = geometry_index.get(grid['row_start'], ([], []))
            left = bisect_right(ends, (grid['column_start'], chr(0x10ffff))) - 1
            right = bisect_left(starts, (grid['column_end'], ''))
            if left >= 0:
                relation['left'] = ends[left][1]
            if right < len(starts):
                relation['right'] = starts[right][1]
        else:
            anchors = ordinary_rows.get(row, [])
            left = bisect_left(anchors, (column, '')) - 1
            right = bisect_right(anchors, (column, chr(0x10ffff)))
            if left >= 0:
                relation['left'] = anchors[left][1]
            if right < len(anchors):
                relation['right'] = anchors[right][1]
            above = ordinary_columns.get(column, [])
            position = bisect_left(above, (row, '')) - 1
            if position >= 0:
                relation['above'] = above[position][1]
    # Range-best updates / point queries: each source interval is inserted once,
    # and each target probes one compressed column in logarithmic time.
    columns = sorted({column for _, column, _ in geometry_targets})
    size = 1
    while size < len(columns):
        size *= 2
    empty = (float('inf'), '')
    best = [empty] * (size * 2)
    events = iter(sorted(rectangles, key=lambda item: (item[0], item[1])))
    event = next(events, None)
    for row, column, identity in sorted(geometry_targets):
        while event is not None and event[0] <= row:
            end, anchor_id, grid = event
            left = bisect_left(columns, grid['column_start']) + size
            right = bisect_left(columns, grid['column_end']) + size
            value = (-end, anchor_id)
            while left < right:
                if left & 1:
                    best[left] = min(best[left], value)
                    left += 1
                if right & 1:
                    right -= 1
                    best[right] = min(best[right], value)
                left //= 2
                right //= 2
            event = next(events, None)
        position, value = bisect_left(columns, column) + size, empty
        while position:
            value = min(value, best[position])
            position //= 2
        if value != empty:
            result[identity]['above'] = value[1]
    # Stable direction order also defines direct-context serialization priority.
    return {identity: {direction: relation[direction] for direction in ('left', 'above', 'right')
                       if direction in relation} for identity, relation in result.items()}


def table_contexts(fields, target_ids):
    """Use actual recorded coordinates, including merged-cell first anchors."""
    target_ids = tuple(target_ids)
    indexed, by_id = {}, {}
    for field in fields:
        location = _cell(field)
        if location is None:
            continue
        by_id[field['id']] = (field, location)
        if field.get('text', '').strip():
            table, row, column = location
            indexed.setdefault(table, {}).setdefault(row, []).append((column, field))
    for rows in indexed.values():
        for row, cells in rows.items():
            cells.sort(key=lambda item: (item[0], item[1]['id']))
            rows[row] = ([column for column, _ in cells], cells)
    row_numbers = {table: sorted(rows) for table, rows in indexed.items()}
    by_table = {}
    for field, location in by_id.values():
        by_table.setdefault(location[0], []).append((field, location))
    requested = {}
    for identity in target_ids:
        if identity in by_id:
            requested.setdefault(by_id[identity][1][0], []).append(by_id[identity])
    neighbors = {}
    for table, targets in requested.items():
        neighbors.update(_nearest_anchors_for_table(by_table[table], targets))
    result = {}
    for identity in target_ids:
        if identity not in by_id:
            continue
        target_field, (table, target_row, target_column) = by_id[identity]
        rows = indexed.get(table, {})
        numbers = row_numbers.get(table, [])
        row_position = bisect_left(numbers, target_row)
        previous = numbers[row_position - 1] if row_position else None
        row_order = list(dict.fromkeys(row for row in (target_row, previous, 0) if row in rows))
        nearest = neighbors[identity]
        direct_ids = list(dict.fromkeys(nearest.values()))
        for neighbor in direct_ids:
            neighbor_row = by_id[neighbor][1][1]
            if neighbor_row not in row_order:
                row_order.append(neighbor_row)
        context, truncated = [], False
        budget_per_row = 1024 // max(1, len(row_order))
        for row in row_order:
            columns, cells = rows[row]
            position = bisect_left(columns, target_column)
            nearby = [item for item in cells[max(0, position - 6):position + 7] if item[1]['id'] != identity]
            nearby.sort(key=lambda item: (abs(item[0] - target_column), item[0] > target_column, item[0]))
            direct = [(by_id[neighbor][1][2], by_id[neighbor][0])
                      for neighbor in direct_ids if by_id[neighbor][1][1] == row]
            chosen = (sorted(direct, key=lambda item: direct_ids.index(item[1]['id']))
                + [item for item in nearby if item[1]['id'] not in direct_ids])[:4]
            available = len(cells) - int(row == target_row and bool(target_field.get('text', '').strip()))
            truncated |= len(chosen) < available
            budget = budget_per_row
            emitted = []
            for column, field in chosen:
                if budget <= 0:
                    truncated = True
                    break
                original = field['text']
                text = original[:min(128, budget)]
                item = {'id': field['id'], 'row': row, 'column': column, 'text': text}
                grid = field.get('table_grid')
                if grid and (field['id'] in direct_ids or grid['row_span'] > 1 or grid['grid_span'] > 1):
                    item['grid'] = dict(grid)
                if len(text) < len(original):
                    item.update(truncated=True, original_length=len(original))
                    truncated = True
                budget -= len(text)
                emitted.append(item)
            context.extend(sorted(emitted, key=lambda item: item['column']))
        if context:
            retained = {cell['id'] for cell in context}
            result[identity] = {'table_id': table,
                'relation_semantics': 'physical_neighbors_not_ownership',
                'nearest_nonempty_anchor_ids': {key: value for key, value in nearest.items() if value in retained},
                'target_coordinate': {'row': target_row, 'column': target_column},
                # Only retained original anchors: these are candidates, not
                # inferred headers or merged-cell coverage/role assignments.
                'preceding_same_column_ids': [cell['id'] for cell in
                    sorted(context, key=lambda item: item['row'], reverse=True)
                    if cell['column'] == target_column and cell['row'] < target_row],
                'cells': sorted(context, key=lambda item: (
                    direct_ids.index(item['id']) if item['id'] in direct_ids else len(direct_ids),
                    item['row'], item['column'])),
                'truncated': truncated}
            if target_field.get('table_grid'):
                result[identity]['target_grid'] = dict(target_field['table_grid'])
    return result
