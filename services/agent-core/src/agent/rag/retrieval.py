"""SQLite vector similarity and FTS5 fusion without lexical vector prefiltering."""
from __future__ import annotations

import math
import json
import re
import struct

from agent.storage import CoreDataError
from agent.rag.documents import CLAUSE


def explicit_subject(query: str) -> str | None:
    match = re.search(r'(?:关于|有关)\s*(.{2,60}?)\s*(?:的)?(?:相关)?(?:内容|条款|规定|要求|资料|知识)(?=[\s。？！?!，,]|$)', query)
    return match.group(1).strip().removesuffix('的').strip() if match else None


def matches_subject(text: str, subject: str) -> bool:
    compact_subject = re.sub(r'\s+', '', subject).casefold()
    compact_text = re.sub(r'\s+', '', text).casefold()
    if compact_subject in compact_text:
        return True
    terms = set(lexical_tokens(compact_subject))
    matched = terms.intersection(lexical_tokens(compact_text))
    return len(matched) >= 2 and len(matched) >= len(terms) * 0.65


def reference_material(text: str, query: str) -> bool:
    """Identify explicit front matter/navigation without excluding formal scope sections."""
    titles = r'译者的话|译者序|编者的话|前言|序言|目\s*录|foreword|preface|table of contents'
    if re.search(titles + r'|译者|作者|编者|谁翻译|谁编写|出版|修订历史|版本历史|封面|扉页|发行日期|title page|cover page', query, re.IGNORECASE):
        return False
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if any(re.fullmatch(titles, line, re.IGNORECASE) for line in lines[:3]):
        return True
    publisher = re.search(r'^\s*RTCA\s*[,，]\s*INC\.?\s*$', text, re.IGNORECASE | re.MULTILINE)
    contact = re.search(r'\bWashington\s*,\s*D\.?\s*C\.?(?=\s|\d|$)', text, re.IGNORECASE)
    chapter = re.search(r'^\s*(?:第\s*[一二三四五六七八九十百零〇\d]+\s*章|section\s+\d+\b)',
                        text, re.IGNORECASE | re.MULTILINE)
    standard = re.search(r'RTCA\s*/\s*DO[-－ ]?160[A-Z]?', text, re.IGNORECASE)
    issued = re.search(r'发行日期|issue date|date of issue', text, re.IGNORECASE)
    technical_prose = re.search(
        r'\b(?:shall|must|should|appl(?:y|ies|icable)|verif(?:y|ies)|purpose|requirements?)\b'
        r'|(?:设备|系统|试验)[^\n]{0,32}(?:应|须|必须|不得|验证|满足)'
        r'|(?:^|\n)\s*本(?:章|节|试验)(?:适用|用于|规定)', text, re.IGNORECASE)
    if publisher and contact and chapter and standard and issued and not technical_prose \
            and not any(CLAUSE.match(line) for line in lines):
        return True
    return sum(bool(re.search(r'(?:…{2,}|\.{5,}|·{5,})\s*\w+(?:[-－]\d+)?\s*$', line)) for line in lines) >= 2


def lexical_tokens(text: str) -> list[str]:
    tokens = re.findall(r'[a-zA-Z0-9]+(?:[.\-][a-zA-Z0-9]+)*', text.lower())
    for run in re.findall(r'[\u3400-\u9fff]+', text):
        tokens.extend(run[index:index + 2] for index in range(len(run) - 1))
        if len(run) == 1:
            tokens.append(run)
    return tokens


def reciprocal_rank_fusion(rankings, k=60):
    scores = {}
    for ranking in rankings:
        for rank, identifier in enumerate(ranking, 1):
            scores[identifier] = scores.get(identifier, 0.0) + 1.0 / (k + rank)
    return scores


def vector_blob(vector, dimension=None):
    try:
        values = [float(value) for value in vector]
    except (TypeError, ValueError) as error:
        raise CoreDataError('rag_invalid_embedding', 'The embedding is not a numeric vector.') from error
    if not values or (dimension is not None and len(values) != dimension):
        raise CoreDataError('rag_dimension_mismatch', 'The embedding dimension does not match the index.')
    if not all(math.isfinite(value) for value in values) or not any(values):
        raise CoreDataError('rag_invalid_embedding', 'The embedding must be finite and nonzero.')
    return struct.pack(f'<{len(values)}f', *values)


def load_vector_extension(connection):
    try:
        import sqlite_vec
        connection.enable_load_extension(True)
        try:
            sqlite_vec.load(connection)
        finally:
            connection.enable_load_extension(False)
    except Exception as error:
        raise CoreDataError('rag_vector_runtime_unavailable', 'The sqlite-vec runtime is unavailable.') from error


def candidates(connection, index_id, query_vector, query, version_ids=None, count=40):
    """Cosine is computed over every authorized indexed chunk before top-k."""
    load_vector_extension(connection)
    params = [query_vector, index_id]
    version_filter = ''
    if version_ids is not None:
        if not version_ids:
            return [], {}
        version_filter = ' AND c.version_id IN (SELECT value FROM json_each(?))'
        params.append(json.dumps(list(dict.fromkeys(version_ids))))
    authorization = ''' FROM rag_index_chunks ic JOIN rag_chunks c ON c.id=ic.chunk_id
        JOIN rag_versions v ON v.id=c.version_id WHERE ic.index_id=? AND v.status='confirmed' ''' + version_filter
    rows = connection.execute('SELECT c.id,vec_distance_cosine(ic.embedding,?) AS distance' + authorization
                              + ' ORDER BY distance,c.id LIMIT ?', [*params, count]).fetchall()
    vector_ids = [row['id'] for row in rows]
    tokens = list(dict.fromkeys(lexical_tokens(query)))[:128]
    lexical_ids = []
    if tokens:
        expression = ' OR '.join('"' + token.replace('"', '""') + '"' for token in tokens)
        lexical_params = [index_id]
        if version_ids is not None:
            lexical_params.append(json.dumps(list(dict.fromkeys(version_ids))))
        lexical_rows = connection.execute('''SELECT c.id FROM rag_fts
            JOIN rag_chunks c ON c.id=rag_fts.chunk_id
            JOIN rag_versions v ON v.id=c.version_id
            JOIN rag_index_chunks ic ON ic.chunk_id=c.id
            WHERE ic.index_id=? AND v.status='confirmed' ''' + version_filter
            + ' AND rag_fts MATCH ? ORDER BY bm25(rag_fts),c.id LIMIT ?',
            [*lexical_params, expression, count]).fetchall()
        lexical_ids = [row['id'] for row in lexical_rows]
    scores = reciprocal_rank_fusion([vector_ids, lexical_ids])
    identifiers = sorted(scores, key=lambda key: (-scores[key], key))[:count]
    return identifiers, scores
