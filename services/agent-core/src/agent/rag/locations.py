"""Read-time correction of unverified inherited PDF illustration locations."""
import json
import re
import unicodedata


def normalize_source_location(source, *, block_text=None):
    locator = source.get('locator', {})
    if isinstance(locator, str):
        locator = json.loads(locator)
    if not isinstance(locator, dict) or locator.get('kind') != 'pdf' or locator.get('heading_path'):
        return source
    block = locator.get('block_id')
    if not block or block == locator.get('clause_parent_id'):
        return source
    text = unicodedata.normalize('NFKC', block_text if block_text is not None else source.get('text', ''))
    labels = list(dict.fromkeys(re.sub(r'\s*[-‐‑‒–—－]\s*', '-', match.group(0)).replace(' ', '')
        for match in re.finditer(r'(?m)^\s*[图表]\s*\d+\s*[-‐‑‒–—－]\s*\d+(?!\d)', text)))
    labels = [label.strip() for label in labels]
    if not labels:
        return source
    return {**source, 'clause_number': None,
        'locator': {**locator, 'clause_parent_id': block, 'clause_verified': False,
                    'illustration_labels': labels},
        'location_diagnostic': {'inherited_clause_number': source.get('clause_number'),
                                'inherited_clause_parent_id': locator.get('clause_parent_id')}}


def located_source(connection, source, cache=None):
    """Call only after source authorization, using the same read transaction."""
    locator = source.get('locator', {})
    if isinstance(locator, str):
        locator = json.loads(locator)
    if not isinstance(locator, dict) or locator.get('kind') != 'pdf' or locator.get('heading_path'):
        return source
    block = locator.get('block_id')
    if not block or block == locator.get('clause_parent_id'):
        return source
    if not (locator.get('fragment_count', 1) > 1 or locator.get('continues_before') or locator.get('continues_after')):
        return normalize_source_location(source)
    key = (source['version_id'], block)
    if cache is None:
        cache = {}
    if key not in cache:
        rows = connection.execute("SELECT text FROM rag_chunks WHERE version_id=? "
            "AND json_extract(locator,'$.block_id')=? ORDER BY ordinal", key)
        cache[key] = ''.join(row['text'] for row in rows)
    return normalize_source_location(source, block_text=cache[key] or source.get('text', ''))


def prime_location_cache(connection, sources):
    """Read required fragment blocks in one scan per authorized version."""
    requested = {}
    for source in sources:
        locator = source.get('locator', {})
        if isinstance(locator, str):
            locator = json.loads(locator)
        if (isinstance(locator, dict) and locator.get('kind') == 'pdf'
                and not locator.get('heading_path') and locator.get('block_id')
                and locator.get('block_id') != locator.get('clause_parent_id')
                and (locator.get('fragment_count', 1) > 1 or locator.get('continues_before') or locator.get('continues_after'))):
            requested.setdefault(source['version_id'], set()).add(locator['block_id'])
    cache = {}
    for version_id, blocks in requested.items():
        fragments = {(version_id, block): [] for block in blocks}
        rows = connection.execute("SELECT text,locator FROM rag_chunks WHERE version_id=? "
            "AND json_extract(locator,'$.block_id') IN (SELECT value FROM json_each(?)) ORDER BY ordinal",
            (version_id, json.dumps(sorted(blocks))))
        for row in rows:
            fragments[(version_id, json.loads(row['locator'])['block_id'])].append(row['text'])
        cache.update({key: ''.join(parts) for key, parts in fragments.items()})
    return cache
