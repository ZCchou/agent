"""Map parser fragments onto source positions without changing document identity."""
import hashlib
import json

from agent.storage import CoreDataError


def record_attachment_read(store, task_id, attachment_id, source_hash, coverage, result,
                           *, docx_payload=None, fence=None):
    if docx_payload is None:
        fields = [{**item, 'position': item.get('locator', {})} for item in coverage['items']]
        parser_view = coverage.get('parser_view')
        canonical = store.register(task_id, attachment_id, attachment_id, source_hash,
            fields, complete=coverage.get('complete', False), fence=fence, parser_view=parser_view)
        store.record_read(task_id, canonical, source_hash, result['blocks'], fence=fence, parser_view=parser_view)
        return canonical

    from agent.document_inventory import inventory
    document = inventory(docx_payload)
    if document['sha256'] != source_hash:
        raise CoreDataError('task_source_changed', 'DOCX source identity changed.')
    fields = document['fields']
    with store.repository._connect() as connection:
        canonical = store.canonical(task_id, attachment_id, connection)
        existing = connection.execute('SELECT artifact_id FROM task_inventory_sources WHERE task_id=? AND source_id=?',
                                      (task_id, canonical)).fetchone()
    artifact_id = existing[0] if existing else attachment_id
    canonical = store.register(task_id, attachment_id, artifact_id, source_hash,
        fields, complete=True, fence=fence)
    from agent.documents import load_docx
    body = load_docx(docx_payload).element.body
    paragraph_count = len(body.xpath('.//w:p[not(ancestor::w:tbl) and not(ancestor::w:p)]'))
    paragraphs = {field['position']['paragraph_index'] + 1: field for field in fields
                  if 'paragraph_index' in field.get('position', {})}
    if len(paragraphs) != paragraph_count:
        paragraphs = {}
    rows = {}
    for field in fields:
        position = field.get('position', {})
        if len(position.get('table_path', [])) == 1:
            key = (position['table_path'][0] + 1, position['row'] + 1)
            rows.setdefault(key, []).append(field)
    table_count = len(body.xpath('.//w:tbl[not(ancestor::w:tbl)]'))
    if len({key[0] for key in rows}) != table_count:
        rows = {}
    groups = {}
    for block in result['blocks']:
        locator = block.get('locator', {})
        if locator.get('kind') != 'docx' or locator.get('part', 'body') != 'body':
            continue
        if 'table' in locator:
            selected = sorted(rows.get((locator['table'], locator.get('row')), []),
                              key=lambda field: field['position']['column'])
            if any(field.get('kind') == 'table_container' for field in selected):
                continue
            separator = ' | '
        else:
            field = paragraphs.get(locator.get('paragraph'))
            if field and not rows:
                selected = [paragraphs[index] for index in sorted(paragraphs)
                            if index >= locator['paragraph']]
            else:
                selected = [field] if field else []
            separator = '\n'
        if not selected:
            continue
        expected = separator.join(field['original_content'] for field in selected)
        offset = block.get('character_offset', locator.get('offset', 0))
        text = block.get('text', '')
        if type(offset) is not int or offset < 0 or expected[offset:offset + len(text)] != text:
            continue
        identity = tuple(field['id'] for field in selected)
        entry = groups.setdefault(identity, {'fields': selected, 'text': expected, 'segments': []})
        entry['segments'].append([offset, offset + len(text)])

    from agent.execution.task_plans import TaskPlans
    with store.repository._connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        TaskPlans._active(connection, task_id, fence)
        source = connection.execute('SELECT source_hash,parser_view FROM task_inventory_sources WHERE task_id=? AND source_id=?',
                                    (task_id, canonical)).fetchone()
        if source is None or source['source_hash'] != source_hash or source['parser_view'] is not None:
            raise CoreDataError('task_source_changed', 'Attachment source identity changed.')
        for identity, entry in groups.items():
            marker = 'parser-read:' + hashlib.sha256(json.dumps(identity).encode()).hexdigest()
            previous = connection.execute('SELECT text FROM task_read_proofs WHERE task_id=? AND source_id=? AND source_hash=? AND target_id=?',
                (task_id, canonical, source_hash, marker)).fetchone()
            ranges = json.loads(previous[0]) if previous else []
            merged = []
            for begin, end in sorted([*ranges, *entry['segments']]):
                if merged and begin <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], end)
                else:
                    merged.append([begin, end])
            connection.execute('INSERT OR REPLACE INTO task_read_proofs VALUES (?,?,?,?,?,0)',
                (task_id, canonical, source_hash, marker, json.dumps(merged)))
            begin = 0
            separator_length = 3 if entry['fields'][0].get('kind') in {'table_cell', 'table_container'} else 1
            for field in entry['fields']:
                end = begin + len(field['original_content'])
                if field.get('field_type') != 'unreadable' and any(left <= begin and right >= end for left, right in merged):
                    connection.execute('INSERT OR REPLACE INTO task_read_proofs VALUES (?,?,?,?,?,1)',
                        (task_id, canonical, source_hash, field['id'], field['original_content']))
                begin = end + separator_length
    return canonical
