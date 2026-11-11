"""Merge upgraded SQLite snapshots while preserving conflicting business graphs.

The migration coordinator owns source snapshots, file copies, model registries,
settings and atomic publication. This module only writes the candidate database.
"""
from __future__ import annotations

import hashlib
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import uuid

from agent.storage import CoreDataError


_GLOBALS = {'schema_info', 'global_instructions', 'local_models', 'local_model_settings'}
_JSON_COLUMNS = {'payload', 'options', 'locator', 'warnings', 'ocr_summary', 'version_ids', 'index_ids', 'splitter_versions', 'result_summary'}
_TEXT_FIELDS = {'text', 'content', 'quote', 'summary', 'query', 'query_text', 'error_message',
                'name', 'title', 'filename', 'original_name', 'description', 'revision', 'source', 'document_number'}
_STORAGE_FIELDS = {'storage_key', 'model_storage_key', 'source_key'}
_ID_TOKEN = re.compile(r'[a-zA-Z0-9_-]{8,}')
_UUID = re.compile(r'[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}')


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def rewrite_storage_key(value, mapping):
    value = _UUID.sub(lambda match: mapping.get(match[0], match[0]), value)
    return re.sub(r'[^/\\.]+', lambda match: mapping.get(match[0], match[0]), value)


def rewrite_references(value, mapping, *, exact=False):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in _STORAGE_FIELDS and isinstance(item, str):
                item = rewrite_storage_key(item, mapping)
            else:
                item = rewrite_references(item, mapping, exact=key not in _TEXT_FIELDS)
            result[mapping.get(key, key)] = item
        return result
    if isinstance(value, list):
        return [rewrite_references(item, mapping, exact=exact) for item in value]
    if isinstance(value, str):
        if exact and value in mapping:
            return mapping[value]
        return re.sub(r'rag-evidence:([a-zA-Z0-9_-]+)',
                      lambda match: 'rag-evidence:' + mapping.get(match[1], match[1]), value)
    return value


def _tables(connection):
    names = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
             if not row[0].startswith(('sqlite_', 'rag_fts', 'profile_migration_')) and row[0] not in _GLOBALS]
    result = {}
    for name in names:
        columns = connection.execute(f'PRAGMA table_info({_quote(name)})').fetchall()
        primary = [row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5]]
        if not primary:
            raise CoreDataError('migration_schema_unsupported', f'Table {name} has no stable identity.')
        result[name] = {'columns': [row[1] for row in columns], 'primary': primary}
    return result


def _row_hash(row):
    digest = hashlib.sha256()
    for value in row:
        data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode('utf8')
        digest.update(type(value).__name__.encode('ascii') + str(len(data)).encode('ascii') + b':' + data)
    return digest.hexdigest()


def _graph(connection, tables):
    hashes, parents, identifiers, references = {}, {}, {}, {}
    for table, schema in tables.items():
        for row in connection.execute(f'SELECT * FROM {_quote(table)}'):
            values = dict(zip(schema['columns'], row))
            node = (table, tuple(values[key] for key in schema['primary']))
            hashes[node] = _row_hash(row)
            parents[node] = node
            if 'id' in schema['primary'] and isinstance(values.get('id'), str):
                identifiers.setdefault(values['id'], []).append(node)
            links = set()
            for column, value in values.items():
                if not isinstance(value, str):
                    continue
                if column.endswith('_id') or column in {'source_id', 'id'}:
                    links.add(value)
                if column.endswith('_json') or column in _JSON_COLUMNS:
                    links.update(_ID_TOKEN.findall(value))
                if column == 'content':
                    links.update(re.findall(r'rag-evidence:([a-zA-Z0-9_-]+)', value))
            references[node] = links

    def find(node):
        while parents[node] != node:
            parents[node] = parents[parents[node]]
            node = parents[node]
        return node

    for node, links in references.items():
        for value in links:
            for other in identifiers.get(value, []):
                parents[find(other)] = find(node)
    groups = {}
    for node in hashes:
        groups.setdefault(find(node), set()).add(node)
    membership = {node: group for group in groups.values() for node in group}
    return hashes, list(groups.values()), membership


def _new_id(value):
    match = re.search(r'[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}$', value)
    return value[:match.start()] + str(uuid.uuid4()) if match else str(uuid.uuid4())


def _rewrite_column(column, value, mapping):
    if not isinstance(value, str):
        return value
    if value in mapping and column not in _TEXT_FIELDS:
        return mapping[value]
    if column.endswith('_json') or column in _JSON_COLUMNS:
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return rewrite_references(value, mapping)
        rewritten = rewrite_references(parsed, mapping)
        if rewritten == parsed:
            return value
        return json.dumps(rewritten, ensure_ascii=False, separators=(',', ':'))
    if column in _STORAGE_FIELDS:
        return rewrite_storage_key(value, mapping)
    return rewrite_references(value, mapping)


def _remap_row(table, values, mapping):
    rewritten = {column: _rewrite_column(column, value, mapping) for column, value in values.items()}
    return refresh_content_hashes(table, values, rewritten)


def refresh_content_hashes(table, values, rewritten):
    if table == 'execution_plans' and rewritten.get('resolved_intent_json') != values.get('resolved_intent_json'):
        from agent.context_engine.attachment_references import validate_saved_selection
        from agent.execution.domain import stable_hash
        original = json.loads(values['resolved_intent_json'])
        validate_saved_selection(values, original)
        intent = json.loads(rewritten['resolved_intent_json'])
        rewritten['resolution_hash'] = stable_hash(intent)
        if 'attachment_selection' in intent:
            requirements_hash = stable_hash(intent['effective_requirements'])
            selection = intent['attachment_selection']
            rewritten['requirements_hash'] = stable_hash({
                'requirements_hash': requirements_hash, 'attachment_selection': selection
            }) if selection else requirements_hash
    if table == 'tool_operations':
        if rewritten.get('arguments_json') != values.get('arguments_json'):
            from agent.tool_contracts import canonical_arguments
            rewritten['arguments_json'], rewritten['arguments_hash'] = canonical_arguments(json.loads(rewritten['arguments_json']))
        original = values.get('result_summary')
        if isinstance(original, str) and rewritten['result_summary'] != original:
            if values.get('result_hash') == hashlib.sha256(original.encode('utf8')).hexdigest():
                rewritten['result_hash'] = hashlib.sha256(rewritten['result_summary'].encode('utf8')).hexdigest()
    if table == 'execution_artifacts' and rewritten.get('content_json') != values.get('content_json'):
        content = json.dumps(json.loads(rewritten['content_json']), ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        rewritten.update(content_json=content, content_hash=hashlib.sha256(content.encode('utf8')).hexdigest(),
                         byte_size=len(content.encode('utf8')))
    return rewritten


def merge_snapshots(target_path, source_path, *, source_label, check_cancel=lambda: None):
    """Merge one immutable, upgraded source into an isolated target transaction."""
    target_path, source_path = Path(target_path).resolve(), Path(source_path).resolve()
    if target_path == source_path or not target_path.is_file() or not source_path.is_file():
        raise CoreDataError('migration_source_invalid', 'Choose separate source and candidate snapshots.')
    with closing(sqlite3.connect(source_path.as_uri() + '?mode=ro', uri=True)) as source, closing(sqlite3.connect(target_path)) as target:
        source.execute('BEGIN')
        target.execute('PRAGMA foreign_keys=OFF')
        target.execute('BEGIN IMMEDIATE')
        try:
            check_cancel()
            if source.execute('SELECT version FROM schema_info').fetchone() != target.execute('SELECT version FROM schema_info').fetchone():
                raise CoreDataError('migration_schema_unsupported', 'Upgrade copies of both profiles before merging.')
            from agent.migrations.v30_unified import create_tables
            create_tables(target)
            tables = _tables(source)
            if tables != _tables(target):
                raise CoreDataError('migration_schema_unsupported', 'Profile table contracts differ.')
            source_hashes, source_groups, _ = _graph(source, tables)
            target_hashes, _, target_membership = _graph(target, tables)
            fingerprint = hashlib.sha256()
            for node, digest in sorted(source_hashes.items(), key=lambda entry: repr(entry[0])):
                fingerprint.update(repr(node).encode('utf8') + digest.encode('ascii'))
            graph_digest = fingerprint.hexdigest()
            for table in sorted(_GLOBALS):
                hashes = sorted(_row_hash(row) for row in source.execute(f'SELECT * FROM {_quote(table)}'))
                fingerprint.update(table.encode('ascii') + ''.join(hashes).encode('ascii'))
            source_digest = fingerprint.hexdigest()
            previous = target.execute('SELECT report_json FROM profile_migration_sources WHERE source_label=? AND source_digest=?',
                                      (source_label, source_digest)).fetchone()
            if previous:
                target.rollback()
                return json.loads(previous[0])
            prior_graph = None
            for row in target.execute('SELECT report_json FROM profile_migration_sources WHERE source_label=? ORDER BY rowid DESC', (source_label,)):
                report = json.loads(row[0])
                if report.get('graph_digest') == graph_digest:
                    prior_graph = report
                    break
            mapping, imported = dict(prior_graph['id_map']) if prior_graph else {}, set()
            for group in ([] if prior_graph else source_groups):
                check_cancel()
                first = next(iter(group))
                same = target_membership.get(first) == group and all(target_hashes.get(node) == source_hashes[node] for node in group)
                if same:
                    continue
                imported.update(group)
                if group.intersection(target_hashes):
                    for table, key in group:
                        primary = tables[table]['primary']
                        if 'id' in primary:
                            value = key[primary.index('id')]
                            if isinstance(value, str):
                                mapping.setdefault(value, _new_id(value))
            # Model IDs are immutable weight identities. Preserve the preferred
            # profile configuration and bring in missing model identities only.
            model_columns = [row[1] for row in source.execute('PRAGMA table_info(local_models)')]
            if model_columns:
                for row in source.execute('SELECT * FROM local_models'):
                    if not target.execute('SELECT 1 FROM local_models WHERE id=?', (row[model_columns.index('id')],)).fetchone():
                        values = list(row)
                        if target.execute('SELECT 1 FROM local_models WHERE is_default=1').fetchone():
                            values[model_columns.index('is_default')] = 0
                        values[model_columns.index('status')] = 'imported'
                        target.execute(f'INSERT INTO local_models VALUES ({",".join("?" for _ in row)})', values)
            counts = {}
            for table, schema in tables.items():
                columns = schema['columns']
                for row in source.execute(f'SELECT * FROM {_quote(table)}'):
                    check_cancel()
                    values = dict(zip(columns, row))
                    if (table, tuple(values[key] for key in schema['primary'])) not in imported:
                        continue
                    rewritten = _remap_row(table, values, mapping)
                    for key in ('idempotency_key', 'request_key'):
                        if key in columns and values[key]:
                            identity = f'{source_label}\0{graph_digest}\0{table}\0{values[key]}'
                            rewritten[key] = 'migration-' + hashlib.sha256(identity.encode('utf8')).hexdigest()
                    if table in {'projects', 'chat_groups', 'rag_libraries'} and values.get('id') in mapping:
                        rewritten['name'] = f'{values["name"]} · {source_label}'
                    if table == 'conversations' and values.get('id') in mapping:
                        rewritten['title'] = f'{values["title"]} · {source_label}'
                    target.execute(f'INSERT INTO {_quote(table)} ({",".join(_quote(column) for column in columns)}) VALUES ({",".join("?" for _ in row)})', [rewritten[column] for column in columns])
                    counts[table] = counts.get(table, 0) + 1
            from agent.rag.retrieval import lexical_tokens
            target.execute('DELETE FROM rag_fts')
            for chunk_id, text in target.execute('SELECT id,text FROM rag_chunks'):
                check_cancel()
                target.execute('INSERT INTO rag_fts(chunk_id,tokens) VALUES (?,?)', (chunk_id, ' '.join(lexical_tokens(text))))
            violations = target.execute('PRAGMA foreign_key_check').fetchall()
            if violations:
                raise CoreDataError('migration_integrity_failed', 'Merged references failed validation.')
            report = {'migration_id': str(uuid.uuid4()), 'source_label': source_label,
                      'source_digest': source_digest, 'graph_digest': graph_digest, 'id_map': mapping, 'imported_rows': counts}
            target.execute('INSERT INTO profile_migration_sources VALUES (?,?,?,?)',
                           (report['migration_id'], source_label, source_digest, json.dumps(report, ensure_ascii=False)))
            check_cancel()
            target.commit()
            return report
        except BaseException:
            target.rollback()
            raise
