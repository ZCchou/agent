"""Document contents, read proofs and semantic selectors, without work obligations."""
import hashlib
import json
from agent.storage import CoreDataError


class SourceInventory:
    def __init__(self, repository):
        self.repository=repository

    def record_attachment_read(self, task_id, attachment_id, source_hash, coverage, result,
                               *, docx_payload=None, fence=None):
        from agent.execution.attachment_read_proofs import record_attachment_read
        return record_attachment_read(self, task_id, attachment_id, source_hash, coverage, result,
                                      docx_payload=docx_payload, fence=fence)

    def canonical(self, task_id, alias, connection=None):
        if connection is None:
            with self.repository._connect() as db:return self.canonical(task_id,alias,db)
        if connection.execute('SELECT 1 FROM task_inventory_sources WHERE task_id=? AND source_id=?', (task_id, alias)).fetchone():
            return alias
        artifacts = connection.execute('SELECT source_id FROM task_inventory_sources WHERE task_id=? AND artifact_id=?',
                                       (task_id, alias)).fetchall()
        if len(artifacts) > 1:
            raise CoreDataError('task_source_alias_ambiguous', '该文档产物对应多个授权附件，请使用附件身份。')
        row=connection.execute('SELECT source_id FROM task_source_aliases WHERE task_id=? AND alias=?',(task_id,alias)).fetchone()
        return row[0] if row else alias

    def register(self, task_id, source_id, artifact_id, source_hash, fields, *, complete,
                 fence=None, parser_view=None):
        from agent.execution.task_plans import TaskPlans
        if parser_view is not None and (not isinstance(parser_view, str) or not 1 <= len(parser_view) <= 1024):
            raise CoreDataError('task_parser_view_invalid', '资料解析配置身份无效。')
        with self.repository._connect() as db:
            db.execute('BEGIN IMMEDIATE');TaskPlans._active(db,task_id,fence)
            request = db.execute('SELECT request_json FROM task_plan_revisions WHERE task_id=? ORDER BY revision LIMIT 1', (task_id,)).fetchone()
            authorized = {item['id'] for item in json.loads(request[0]).get('attachments', [])} if request else set()
            if source_id in authorized:
                canonical = source_id
            else:
                canonical = self.canonical(task_id, source_id, db)
                existing = db.execute('SELECT 1 FROM task_inventory_sources WHERE task_id=? AND source_id=?', (task_id, canonical)).fetchone()
                if not existing:
                    canonical = self.canonical(task_id, artifact_id, db)
                    existing = db.execute('SELECT 1 FROM task_inventory_sources WHERE task_id=? AND source_id=?', (task_id, canonical)).fetchone()
                    if not existing:
                        canonical = source_id
            previous=db.execute('SELECT source_hash,complete,artifact_id,parser_view FROM task_inventory_sources WHERE task_id=? AND source_id=?',(task_id,canonical)).fetchone()
            same_view = bool(previous and previous['source_hash'] == source_hash and previous['parser_view'] == parser_view)
            if previous and not same_view:
                db.execute('DELETE FROM task_read_proofs WHERE task_id=? AND source_id=?',(task_id,canonical))
                db.execute('DELETE FROM task_review_proofs WHERE task_id=? AND source_id=?',(task_id,canonical))
                db.execute('DELETE FROM task_inventory_positions WHERE task_id=? AND source_id=?',(task_id,canonical))
            inventory_complete = complete or bool(same_view and previous['complete'])
            db.execute('''INSERT OR REPLACE INTO task_inventory_sources
                (task_id,source_id,artifact_id,source_hash,complete,parser_view) VALUES (?,?,?,?,?,?)''',
                (task_id,canonical,artifact_id,source_hash,int(inventory_complete),parser_view))
            for alias in {canonical,source_id,artifact_id}:
                row=db.execute('SELECT source_id FROM task_source_aliases WHERE task_id=? AND alias=?',(task_id,alias)).fetchone()
                if alias == artifact_id and alias not in authorized:
                    owners = db.execute('SELECT source_id FROM task_inventory_sources WHERE task_id=? AND artifact_id=?', (task_id, artifact_id)).fetchall()
                    if len(owners) > 1:
                        db.execute('DELETE FROM task_source_aliases WHERE task_id=? AND alias=?', (task_id, alias))
                        continue
                if row and row[0]!=canonical:raise CoreDataError('task_source_alias_conflict','资料身份关联冲突。')
                db.execute('INSERT OR REPLACE INTO task_source_aliases VALUES (?,?,?)',(task_id,alias,canonical))
            for ordinal,field in enumerate(fields):
                text=field.get('original_content',field.get('text',''))
                value={k:field[k] for k in ('id','position','chapter_path','field_type','extraction_status') if k in field}
                value.update(nonempty=bool(text.strip()) or field.get('extraction_status')=='unreadable' or field.get('field_type')=='unreadable',
                    character_count=len(text),text_sha256=hashlib.sha256(text.encode()).hexdigest())
                previous_field = db.execute('SELECT ordinal,content_json FROM task_inventory_positions WHERE task_id=? AND source_id=? AND target_id=?',
                    (task_id,canonical,field['id'])).fetchone()
                if previous_field and (previous_field['ordinal'] != ordinal or json.loads(previous_field['content_json']) != value):
                    raise CoreDataError('task_inventory_conflict','同一资料版本的位置清单发生变化，请重新解析资料。')
                db.execute('INSERT OR REPLACE INTO task_inventory_positions VALUES (?,?,?,?,?,?)',
                    (task_id,canonical,field['id'],ordinal,source_hash,json.dumps(value,ensure_ascii=False)))
        return canonical

    def record_read(self, task_id, source_id, source_hash, fields, *, fence=None, parser_view=None):
        from agent.execution.task_plans import TaskPlans
        with self.repository._connect() as db:
            db.execute('BEGIN IMMEDIATE');TaskPlans._active(db,task_id,fence)
            source=self.canonical(task_id,source_id,db)
            current = db.execute('SELECT source_hash,parser_view FROM task_inventory_sources WHERE task_id=? AND source_id=?',
                                 (task_id, source)).fetchone()
            if current is None or current['source_hash'] != source_hash or current['parser_view'] != parser_view:
                raise CoreDataError('task_source_changed', '读取结果的资料版本或解析配置已变化。')
            for field in fields:
                row=db.execute('SELECT content_json FROM task_inventory_positions WHERE task_id=? AND source_id=? AND source_hash=? AND target_id=?',
                    (task_id,source,source_hash,field['id'])).fetchone()
                if row is None:raise CoreDataError('task_source_changed','读取结果与资料版本不一致。')
                item=json.loads(row[0]);text=field.get('text','')
                complete=(hashlib.sha256(text.encode()).hexdigest()==item['text_sha256'] and field.get('extraction_status')!='unreadable' and item.get('extraction_status')!='unreadable' and item.get('field_type')!='unreadable')
                old=db.execute('SELECT complete FROM task_read_proofs WHERE task_id=? AND source_id=? AND target_id=?',(task_id,source,field['id'])).fetchone()
                if not old or not old[0]:db.execute('INSERT OR REPLACE INTO task_read_proofs VALUES (?,?,?,?,?,?)',
                    (task_id,source,source_hash,field['id'],text,int(complete)))

    def resolve(self, task_id, scope):
        value=dict(scope)
        with self.repository._connect() as db:
            source=self.canonical(task_id,scope['source_id'],db)
            metadata=db.execute('SELECT * FROM task_inventory_sources WHERE task_id=? AND source_id=?',(task_id,source)).fetchone()
            if metadata is None:return {**value,'resolved':False}
            rows=db.execute('SELECT target_id,content_json FROM task_inventory_positions WHERE task_id=? AND source_id=? ORDER BY ordinal',(task_id,source)).fetchall()
        available=[r[0] for r in rows if json.loads(r[1])['nonempty']]
        selector=scope.get('selector')
        if selector:
            if selector.get('kind') == 'selection_pending':
                return {**value, 'resolved': False}
            if selector.get('kind')!='prefix_nonempty' or type(selector.get('count')) is not int or not 1<=selector['count']<=10000:
                raise CoreDataError('task_selector_invalid','资料范围选择器无效。')
            if len(available)<selector['count'] and not metadata['complete']:return {**value,'resolved':False}
            value['target_ids']=available[:selector['count']]
            value['missing_positions']=max(0,selector['count']-len(available))
        elif scope['scope']=='full':value['target_ids']=available
        elif scope['scope']=='selected' and set(scope['target_ids'])-set(available):return {**value,'resolved':False}
        return {**value,'resolved':scope['scope']!='full' or bool(metadata['complete']),
            'canonical_source_id':source,'artifact_id':metadata['artifact_id'],'source_hash':metadata['source_hash'],
            'parser_view':metadata['parser_view'],
            'inventory_complete':bool(metadata['complete']),'position_count':len(rows)}

    def coverage(self, task_id, scope):
        resolved=self.resolve(task_id,scope)
        ids=resolved.get('target_ids',[])
        if not resolved.get('resolved'):return {'ready':False,'completed':0,'remaining':len(ids),'reason':'scope_unresolved'}
        if 'total' in scope.get('metadata_keys',[]) and not resolved['inventory_complete']:
            return {'ready':False,'completed':0,'remaining':0,'reason':'inventory_incomplete'}
        if scope['scope'] in {'metadata','excluded'}:return {'ready':True,'completed':0,'remaining':0}
        with self.repository._connect() as db:
            if scope['operation']=='read':
                done={r[0] for r in db.execute('SELECT target_id FROM task_read_proofs WHERE task_id=? AND source_id=? AND source_hash=? AND complete=1',
                    (task_id,resolved['canonical_source_id'],resolved['source_hash']))}
            else:
                from agent.execution.task_plans import TaskPlans
                generation = TaskPlans.current_evidence_generation(db, task_id)
                done={r[0] for r in db.execute("SELECT target_id FROM task_review_proofs WHERE task_id=? AND source_id=? AND source_hash=? AND outcome IN ('done','gap') AND (? IS NULL OR evidence_generation=?)",
                    (task_id,resolved['canonical_source_id'],resolved['source_hash'],generation,generation))}
        completed=len(set(ids)&done)
        missing_positions=resolved.get('missing_positions',0)
        result={'ready':completed==len(ids) and not missing_positions,'completed':completed,'remaining':len(ids)-completed+missing_positions}
        if missing_positions:result['reason']='selected_positions_missing'
        return result

    def material(self, task_id, scope):
        resolved=self.resolve(task_id,scope)
        if not resolved.get('resolved'):return None
        fields=[]
        with self.repository._connect() as db:
            for identity in resolved.get('target_ids',[]):
                row=db.execute('SELECT p.content_json,r.text,r.complete FROM task_inventory_positions p LEFT JOIN task_read_proofs r ON r.task_id=p.task_id AND r.source_id=p.source_id AND r.target_id=p.target_id AND r.source_hash=p.source_hash WHERE p.task_id=? AND p.source_id=? AND p.target_id=?',
                    (task_id,resolved['canonical_source_id'],identity)).fetchone()
                if row and row['complete']:fields.append({'id':identity,'text':row['text'],'position':json.loads(row['content_json']).get('position')})
        return {'source_id':scope['source_id'],'template_id':resolved['artifact_id'],
            'total':resolved['position_count'] if resolved['inventory_complete'] else None,'fields':fields}
