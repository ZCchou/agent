"""Bounded source-topic queries; source hints never become retrieval authority."""
import json
import re
import unicodedata
from pathlib import PureWindowsPath
from agent.storage import CoreDataError


_UNIT=r'(?:mm\s*Hg|in\s*Hg|mbar(?:s)?|kPa|MPa|hPa|Pa|ft|km|mm|cm|m|°C|℃|min|ms|s|h|%RH|%)'
_NUMBER=r'[+−-]?\d+(?:,\d{3})*(?:\.\d+)?'
_MEASUREMENT=re.compile(r'(?<![A-Za-z0-9.])('+_NUMBER+r')\s*('+_UNIT+r')(?![A-Za-z])',re.I)
_TABLE_CELL=re.compile(r'(.+-r\d+)-c\d+$')


def _normalized(text):
    return unicodedata.normalize('NFKC',text).replace('−','-').replace('，',',')


def _row_quantities(fields):
    rows={}
    for field in fields:
        match=_TABLE_CELL.fullmatch(str(field.get('id','')))
        if match:
            numbers=_MEASUREMENT.findall(_normalized(field.get('text','')))
            rows.setdefault(match[1],set()).update((n.lstrip('+').replace(',',''),u.lower()) for n,u in numbers)
    return rows


def _table_row_excerpts(source, quantities):
    """Keep original header and original rows only when target units align to physical columns."""
    from agent.rag.grounding import _plain_quote_options
    from agent.rag.quote_scopes import illustration_references
    locator=source.get('locator') or {}
    if locator.get('continues_before') or locator.get('continues_after'):
        return []
    lines=source['text'].splitlines(keepends=True)
    offsets=[0]
    for line in lines:offsets.append(offsets[-1]+len(line))
    options=[]
    for start,line in enumerate(lines):
        if not re.match(r'^\s*(?:表\s*\d|table\s+\d)',_normalized(line),re.I):continue
        for header in range(start+1,min(len(lines),start+8)):
            text=_normalized(lines[header]).strip()
            if re.search(r'\d|[。；;]',text):break
            units=re.findall(_UNIT,text,re.I)
            if not units or re.sub(_UNIT+r'|[\s(),/]+','',text,flags=re.I):continue
            matches={}
            for number in range(header+1,len(lines)):
                row=_normalized(lines[number]).strip()
                if not row or not re.match(r'^[+\-]?\d',row):break
                tail=re.search(r'(?:\s+'+_NUMBER+r')+\s*$',row)
                if not tail or len(tail.group().split())!=len(units):break
                cells=list(zip(tail.group().split(),units))
                explicit=_MEASUREMENT.findall(row[:tail.start()])
                row_values={(n.lstrip('+').replace(',',''),u.lower()) for n,u in [*explicit,*cells]}
                for key,values in quantities.items():
                    overlap=row_values & values
                    aligned={(n.lstrip('+').replace(',',''),u.lower()) for n,u in cells}
                    if overlap and aligned & values:matches.setdefault(key,[]).append((number,overlap))
            # Two distinct quantities from the same target row must corroborate the table.
            selected=sorted({n for rows in matches.values() if len(set.union(*(v for _,v in rows)))>=2 for n,_ in rows})
            if selected:
                excerpt=_plain_quote_options(source['text'][offsets[start]:offsets[header+1]],offset=offsets[start])
                for n in selected:
                    excerpt.extend(_plain_quote_options(lines[n],offset=offsets[n]))
                labels=illustration_references(line)
                if len(labels)==1:
                    for option in excerpt:option['illustration_labels']=labels
                options.extend(excerpt)
            break
    return options


def indexed_quantity_table(connection, index_id, version_ids, query):
    """Find a corroborating table in the same confirmed source as the semantic hits."""
    if not version_ids:
        return None
    for line in _normalized(query).splitlines():
        values={(n.lstrip('+').replace(',',''),unit.lower()) for n,unit in _MEASUREMENT.findall(line)}
        if len(values)<2:
            continue
        numbers=sorted({n.lstrip('-') for n,_ in values})
        if len(numbers)<2:
            continue
        match=' AND '.join('"'+n+'"' for n in numbers)
        rows=connection.execute('''SELECT c.id,c.text,c.locator FROM rag_fts
            JOIN rag_chunks c ON c.id=rag_fts.chunk_id JOIN rag_versions v ON v.id=c.version_id
            JOIN rag_index_chunks ic ON ic.chunk_id=c.id WHERE ic.index_id=? AND v.status='confirmed'
            AND c.version_id IN (SELECT value FROM json_each(?)) AND rag_fts MATCH ?
            ORDER BY bm25(rag_fts),c.id LIMIT 24''',(index_id,json.dumps(sorted(version_ids)),match))
        for row in rows:
            source={'text':row['text'],'locator':json.loads(row['locator'])}
            if _table_row_excerpts(source,{'query_line':values}):
                return row['id']
    return None


def retain_quantity_table(ranked, limit, table_ids):
    chosen=ranked[:limit]
    if limit>1 and chosen and not any(row['id'] in table_ids for row in chosen):
        table=next((row for row in ranked[limit:] if row['id'] in table_ids),None)
        if table:
            chosen[-1]=table
    return chosen


def review_evidence_window(sources, fields, budget):
    from agent.context_engine.policy import estimate_tokens
    from agent.rag.grounding import evidence_for_model,evidence_with_quotes
    rows=_row_quantities(fields)
    focused=[]
    if any(len(values)>=2 for values in rows.values()):
        for source in sources:
            excerpts=_table_row_excerpts(source,rows)
            if excerpts:focused.append((source,evidence_for_model({**source,'quote_options':excerpts})))
    result=[];used=set()
    for source,item in [*focused,*((s,evidence_for_model(evidence_with_quotes(s))) for s in sources)]:
        if source['id'] in used or estimate_tokens([*(v for _,v in result),item])>budget:continue
        result.append((source,item));used.add(source['id'])
    return result


def _search_text(text):
    # Bare local cross-references are not references to the external standard.
    from agent.rag.retrieval import _NAMED_STANDARD
    def local_or_named(match):
        before = text[:match.start()].rstrip()
        standards = list(_NAMED_STANDARD.finditer(before))
        return match.group() if standards and standards[-1].end() == len(before) else ''
    return re.sub(r'第\s*\d+(?:\.\d+)*\s*[章节条]|§\s*\d+(?:\.\d+)*', local_or_named, text)


def review_reference_scope(request):
    """Separate attachment selection coordinates from requested external clauses."""
    from agent.rag.retrieval import _CLAUSE_REFERENCE, _NAMED_STANDARD
    text = unicodedata.normalize('NFKC', request)
    topics = []
    pieces = []
    previous = 0
    local = False
    for ref in _CLAUSE_REFERENCE.finditer(text):
        between = text[previous:ref.start()]
        pieces.append(between)
        # Adjacent list items inherit their owner; a new sentence starts a new context.
        if re.search(r'[。！？;\n]', between):
            local = False
        context = re.split(r'[。！？;\n]', between)[-1]
        markers = [(m.end(), False) for m in _NAMED_STANDARD.finditer(context)]
        markers += [(m.end(), False) for m in re.finditer(r'(?:标准|规范|资料库|知识库|\bstandard\b)', context, re.I)]
        markers += [(m.end(), True) for m in re.finditer(
            r'(?:本大纲|附件|待审(?:文档|文件)|审阅(?=\s*(?:第|$))|审查(?=\s*(?:第|$))|\battachment\b)', context, re.I)]
        if markers:
            local = max(markers)[1]
        if local:
            title = re.match(r'\s*[章节条]\s*([\u3400-\u9fff]{2,40})', text[ref.end():])
            if title:
                topic = re.split(r'(?:和|与|及|、)第|试验计划|试验程序', title[1])[0].strip()
                if len(topic) >= 2 and topic not in topics:
                    topics.append(topic)
        else:
            pieces.append(ref.group())
        previous = ref.end()
    pieces.append(text[previous:])
    return ''.join(pieces), topics


def review_queries(request, batch, filename, max_characters, *, max_queries=3):
    cap = max(1, min(3, max_queries))
    stem = PureWindowsPath(filename).stem
    topic = re.sub(r'(?:试验|测试)?(?:步骤|大纲|模板|报告)$', '', stem).strip()
    groups = {}
    heading = ''
    for field in batch['fields']:
        text = str(field.get('text') or '').strip()
        if not text:
            continue
        path = field.get('chapter_path') or batch.get('chapter_path') or []
        if not path and re.fullmatch(r'[a-zA-Z][)）]\s*[^。；;\n]{2,20}', text):
            heading = text
            continue
        anchor = ' / '.join(str(p) for p in path) if path else heading
        groups.setdefault(anchor, []).append(field)
    if not groups:
        return [_search_text(request)[:max_characters]]
    # Filename only fills absent chapter metadata when the exact source body corroborates it.
    supported = len(topic) >= 2 and any(topic in field['text'] for items in groups.values() for field in items)
    ordered = list(groups.items())
    if supported:
        ordered.sort(key=lambda pair: (not any(topic in field['text'] for field in pair[1]), -len(pair[1])))
    _, scope_topics = review_reference_scope(request)
    # Include a shared topic search when a cross-reference batch has only administrative
    # headings. Section numbers of the attachment never become external query terms.
    matching_topics = [topic for topic in scope_topics if any(topic in anchor for anchor in groups)]
    queries = [topic[:max_characters] for topic in (matching_topics or scope_topics)[:max(0, cap - 1)]]
    for anchor, texts in ordered:
        hint = anchor
        if not any(f.get('chapter_path') for f in batch['fields']) and supported:
            hint = ' / '.join(filter(None, [topic, anchor]))
        ranked = sorted(texts, key=lambda field: (not (supported and topic in field['text']), -len(field['text'])))
        # One focused target per query avoids averaging unrelated constraints.
        candidates = ranked[:1] if len(ordered) > 1 else ranked[:cap]
        for field in candidates:
            from agent.document_review import review_evidence_query
            # Preserve verified immediate table anchors without borrowing peer groups.
            local = review_evidence_query('', {'chapter_path': [], 'fields': [field]}, max_characters)
            row=_TABLE_CELL.fullmatch(str(field.get('id','')))
            if row:
                # Keep original values from other selected cells in this physical row;
                # relation metadata still qualifies the focus cell below.
                peers=[str(other['text']).strip() for other in texts if other is not field
                    and (peer:=_TABLE_CELL.fullmatch(str(other.get('id','')))) and peer[1]==row[1]
                    and _MEASUREMENT.search(_normalized(str(other['text'])))]
                if peers:local='\n'.join([*(value[:96] for value in dict.fromkeys(peers)),local])
            query = _search_text('\n'.join(filter(None, [hint, local])))[:max_characters].strip()
            if query and query not in queries:
                queries.append(query)
            if len(queries) == cap:
                return queries
    return queries or [_search_text(request)[:max_characters]]


def _heading_covers_topic(chapter, item):
    # PDF chunks can also contain text from an adjacent clause. Qualify against
    # the authoritative heading rather than an incidental mention in the body.
    heading_path = (item.get('locator') or {}).get('heading_path') or []
    heading = ' '.join(heading_path).casefold()
    if not heading:
        return False
    for label in chapter.casefold().split(' / '):
        if label in heading:
            continue
        # Accept swapped multi-character terms ("短时工作低温" / "低温短时工作").
        # Keep numbers, signed conditions and non-Chinese identifiers literal.
        if not re.fullmatch(r'[\u3400-\u9fff]{4,48}', label):
            return False
        reachable = {0}
        for start in range(len(label)):
            if start in reachable:
                reachable.update(end for end in range(start + 2, len(label) + 1)
                                 if label[start:end] in heading)
        if len(label) not in reachable:
            return False
    return True


def _cached_review_evidence(cache, generation, query):
    key = (generation, query)
    if key in cache:
        return cache[key]
    chapter, _, topic = query.partition('\n')
    topic = topic.strip()
    if len(topic) < 8 or not chapter.strip():
        return None
    if any(len(set(_MEASUREMENT.findall(_normalized(line))))>=2 for line in topic.splitlines()):
        return None
    # Split only at punctuation. Preserve polarity, quantities, units and equipment
    # qualifiers within each phrase. A short or ambiguous fragment requires search.
    from agent.rag.retrieval import explicit_clause_numbers, explicit_standard_references
    clauses = explicit_clause_numbers(topic)
    standards = explicit_standard_references(topic)
    parts = [part.strip() for part in re.split(r'[,，、；;。\n]+', topic) if part.strip()]
    if not parts or any(len(part) < 3 for part in parts):
        return None
    patterns = [re.compile((r'(?<![a-zA-Z0-9.+−-])' if part[0].isascii() and part[0].isalnum() else '')
                + re.escape(part) + (r'(?![a-zA-Z0-9.])' if part[-1].isascii() and part[-1].isalnum() else ''))
                for part in parts]
    matches = {}
    for (saved_generation, saved_query), evidence in cache.items():
        if saved_generation != generation or saved_query.partition('\n')[0] != chapter:
            continue
        for item in evidence:
            if not _heading_covers_topic(chapter, item):
                continue
            if clauses and any(clause != item.get('clause_number') for clause in clauses):
                continue
            if standards and not set(standards).issubset(explicit_standard_references(item.get('document_number') or '')):
                continue
            for sentence in re.split(r'[。！？!?\n]', item.get('text', '')):
                first, cursor = None, 0
                for pattern in patterns:
                    found = pattern.search(sentence, cursor)
                    if not found:
                        break
                    if first is None:
                        first = found.start()
                    cursor = found.end()
                else:
                    span = cursor - first
                    if span <= sum(map(len, parts)) + 48:
                        key = (item.get('index_id'), item.get('version_id'), item.get('text_hash') or item['text'])
                        # Rank independently for this query. Never compare cached cross-encoder scores.
                        if key not in matches or span < matches[key][0]:
                            matches[key] = (span, item)
    if matches:
        return [item for _, item in sorted(matches.values(), key=lambda pair: (pair[0], pair[1]['id']))[:8]]
    return None


async def search_review_units(queries, search, *, generation, cache=None, validate_cached=None):
    results = []
    for query in list(dict.fromkeys(queries))[:3]:
        if cache is not None:
            cached = _cached_review_evidence(cache, generation, query)
            if cached is not None:
                if validate_cached is not None:
                    cached = validate_cached(cached)
                if (generation, query) not in cache:
                    cached = [{k: v for k, v in item.items() if k != 'score'} for item in cached]
                results.append(cached)
                continue
        try:
            result = await search(query)
        except CoreDataError as error:
            if error.code not in {'rag_no_evidence', 'rag_evidence_missing'}:
                raise
            result = {'evidence': [], 'generation': generation}
        if result['generation'] != generation:
            raise CoreDataError('rag_evidence_stale', '审阅检索期间资料已变化，请重新核对。')
        if cache is not None and result['evidence']:
            cache[(generation, query)] = result['evidence']
            if len(cache) > 64:
                del cache[next(iter(cache))]
        results.append(result['evidence'])
    merged, seen = [], set()
    # Give each local unit a candidate before consuming all slots from one topic.
    for position in range(max((len(items) for items in results), default=0)):
        for items in results:
            if position >= len(items):
                continue
            item = items[position]
            identity = (json.dumps({key:item.get(key) for key in
                ('index_id','version_id','locator','text')},sort_keys=True,ensure_ascii=False)
                if item.get('index_id') and item.get('version_id') else item['id'])
            if identity not in seen:
                seen.add(identity)
                merged.append(item)
    return {'evidence':merged,'generation':generation}
