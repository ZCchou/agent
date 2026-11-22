"""Bounded public projections of saved document work; never completion authority."""
import copy
import json
import time


class DocumentProgress:
    def __init__(self, repository, *, clock=time.monotonic, interval=0.5):
        self.repository=repository
        self.clock=clock
        self.interval=interval
        self.cache={}

    def invalidate(self, task_id):
        self.cache.pop(task_id,None)

    def snapshot(self, root, *, refresh=False):
        key=root['id'];now=self.clock();fence=(root.get('revision'),root.get('attempt'),root.get('status'))
        saved=self.cache.get(key)
        if not refresh and saved and saved[1]==fence and now-saved[0]<self.interval:
            result=copy.deepcopy(saved[2])
        else:
            result=self._read(root)
            if len(self.cache)>=64 and key not in self.cache:self.cache.pop(next(iter(self.cache)))
            self.cache[key]=(now,fence,copy.deepcopy(result))
        result=_apply_live_progress(result,root)
        if result is not None:result['status']=root.get('status')
        return result

    def _read(self, root):
        from agent.execution.task_plans import TaskPlans
        with self.repository._connect() as db:
            row=db.execute('SELECT payload_json FROM task_goal_execution WHERE task_id=?',(root['id'],)).fetchone()
            state=json.loads(row[0]).get('_review_selection',{}) if row else {}
            identity=state.get('identity',{})
            source=identity.get('source_id');source_hash=identity.get('source_sha256')
            metadata=db.execute('SELECT source_hash FROM task_inventory_sources WHERE task_id=? AND source_id=?',
                                (root['id'],source)).fetchone() if source else None
            if not metadata or metadata[0]!=source_hash:state={}
            generation=TaskPlans.current_evidence_generation(db,root['id'])
            query='''FROM task_work_items w JOIN task_sources s
                ON s.task_id=w.task_id AND s.artifact_id=w.source_id AND s.source_hash=w.source_hash AND s.operation='review'
                JOIN task_inventory_sources i ON i.task_id=s.task_id AND i.artifact_id=s.artifact_id AND i.source_hash=s.source_hash
                LEFT JOIN task_review_proofs p ON p.task_id=s.task_id AND p.source_id=i.source_id
                AND p.source_hash=w.source_hash AND p.target_id=w.target_id
                WHERE w.task_id=?'''
            values=[root['id']]
            declared=db.execute('SELECT requirements_json FROM task_goal_requirements WHERE task_id=? ORDER BY revision DESC LIMIT 1',(root['id'],)).fetchone()
            if declared:
                from agent.execution.source_inventory import SourceInventory
                clauses=[]
                for scope in json.loads(declared[0])['source_scopes']:
                    if scope['operation']!='review' or scope.get('selector'):continue
                    canonical=SourceInventory(self.repository).canonical(root['id'],scope['source_id'],db)
                    if scope['scope']=='full':
                        clauses.append('(i.source_id=?)');values.append(canonical)
                    elif scope['scope']=='selected' and scope['target_ids']:
                        clauses.append('(i.source_id=? AND w.target_id IN (SELECT value FROM json_each(?)))')
                        values.extend([canonical,json.dumps(scope['target_ids'])])
                query+=' AND ('+(' OR '.join(clauses) if clauses else '0')+')'
            # Stale evidence remains historical; it is not counted as current reviewed work.
            current=' AND (p.evidence_generation=? OR ? IS NULL)'
            review_total=db.execute('SELECT COUNT(*) '+query,values).fetchone()[0]
            outcomes=dict(db.execute('SELECT p.outcome,COUNT(*) '+query+current+' GROUP BY p.outcome',values+[generation,generation]).fetchall())
            latest=db.execute('SELECT w.target_id,w.result_json '+query+current+
                " AND p.outcome IN ('done','gap') AND w.result_json IS NOT NULL ORDER BY w.updated_at DESC,w.target_id DESC LIMIT 1",
                values+[generation,generation]).fetchone()
            reviews=db.execute("SELECT COUNT(*) FROM execution_wait_checkpoints WHERE task_id=? AND checkpoint_key LIKE 'document-review-%'",(root['id'],)).fetchone()[0]
            reviews+=db.execute("""
                SELECT COUNT(*) FROM execution_wait_checkpoints c
                JOIN task_goal_execution g ON g.task_id=c.task_id
                WHERE c.task_id=? AND c.checkpoint_key GLOB 'document-public-review-*'
                AND c.checkpoint_key NOT GLOB '*-supplement-*'
                AND json_extract(c.payload_json,'$.request_digest')=g.request_digest
                AND json_extract(c.payload_json,'$.goal_revision')=g.goal_revision
                AND length(trim(json_extract(c.payload_json,'$.body')))>0
                AND (? IS NULL OR json_extract(c.identity_json,'$.project_generation')=?)
                AND EXISTS (SELECT 1 FROM task_inventory_sources i WHERE i.task_id=c.task_id
                    AND i.artifact_id=json_extract(c.identity_json,'$.template_id')
                    AND i.source_hash=json_extract(c.identity_json,'$.template_sha256'))
                """,(root['id'],generation,generation)).fetchone()[0]
            searches=db.execute('SELECT COUNT(*) FROM rag_searches WHERE run_id=?',(root.get('run_id'),)).fetchone()[0]
            public=db.execute("SELECT payload_json FROM execution_wait_activity WHERE task_id=? AND (kind='document_business' OR (kind='progress' AND json_extract(payload_json,'$.public_document_event') IS NOT NULL)) ORDER BY id DESC LIMIT 1",(root['id'],)).fetchone()
            public_event=json.loads(public[0]).get('public_document_event') if public else None
            latest_progress=db.execute("SELECT payload_json FROM execution_wait_activity WHERE task_id=? AND kind='progress' ORDER BY id DESC LIMIT 1",(root['id'],)).fetchone()
            integrity_active=(json.loads(latest_progress[0]).get('stage')=='integrity_verification') if latest_progress else None
            decisions=state.get('decisions',{})
            decisions=decisions if isinstance(decisions,dict) else {}
            counts={name:sum(isinstance(value,dict) and value.get('decision')==name for value in decisions.values())
                    for name in ('include','exclude','context','uncertain')}
            counts.update(reviewed=outcomes.get('done',0)+outcomes.get('gap',0),gap=outcomes.get('gap',0),
                          role_rechecked=len(state.get('source_role_reviews',{})))
            total=db.execute('SELECT COUNT(*) FROM task_inventory_positions WHERE task_id=? AND source_id=? AND source_hash=?',
                             (root['id'],source,source_hash)).fetchone()[0] if state else 0
            if not state and not review_total and not public_event:return None
            recent=[]
            # Persisted dictionary order reflects committed batches; raw notes are never projected.
            for target,value in list(decisions.items())[-5:]:
                if not isinstance(value,dict) or value.get('decision') not in counts:continue
                position=db.execute('SELECT content_json FROM task_inventory_positions WHERE task_id=? AND source_id=? AND source_hash=? AND target_id=?',
                                     (root['id'],source,source_hash,target)).fetchone()
                if not position:continue
                item={'target_id':target,'decision':value['decision'],'reason':str(value.get('reason',''))[:320]}
                text=json.loads(position[0]).get('text','')
                if isinstance(text,str) and text:item['excerpt']=text[:160]
                recent.append(item)
        technical=bool(counts['reviewed'] or review_total)
        result={'phase':'document_review' if technical else 'source_selection',
                'completed':counts['reviewed'] if technical else sum(counts[name] for name in ('include','exclude','context','uncertain')),
                'total':review_total if technical else total,'batch_count':reviews if technical else len(state.get('batches',[])),
                'counts':counts,'recent':recent,'search_count':searches}
        if public_event:result['_public_document_event']=public_event
        if integrity_active is not None:result['_integrity_active']=integrity_active
        if latest:
            value=json.loads(latest['result_json'])
            labels=[]
            for evidence in value.get('evidence',[])[:8]:
                if not isinstance(evidence,dict):continue
                parts=[str(evidence[key]) for key in ('title','revision','clause_number','locator') if isinstance(evidence.get(key),(str,int)) and str(evidence[key])]
                if parts:labels.append(' · '.join(parts)[:320])
            if not labels:
                labels=[f"{citation['id']} / {citation['quote_id']}"[:320] for citation in value.get('citations',[])[:8]
                        if isinstance(citation,dict) and isinstance(citation.get('id'),str) and isinstance(citation.get('quote_id'),str)]
            result['latest_review']={'target_id':latest['target_id'],'summary':str(value.get('summary',''))[:800],
                                     'citations':list(dict.fromkeys(labels))}
        return result


_PUBLIC_EVENTS={'reading','inventory_ready','selecting','checking','searching','search_results','results_saved','correcting'}
_PUBLIC_PHASES={'attachment_read','source_selection','source_role_review','document_review'}


def publish_document_event(activity, phase, event, *, fields=(), total=None, completed=None, query=None, hits=None, correction=None):
    """Persist only app-constructed public business facts through the existing fence."""
    if phase not in _PUBLIC_PHASES or event not in _PUBLIC_EVENTS:return False
    if activity is None:
        from agent.execution.execution_wait import current_monitor,current_task_id
        monitor,task_id=current_monitor.get(),current_task_id.get()
        if monitor is None or task_id is None:return False
        root=monitor.get_task(task_id)
        value={'phase':phase,'event':event,'task_id':task_id,'revision':root['revision'],'attempt':root['attempt']}
    else:
        value={'phase':phase,'event':event,'task_id':activity.task_id,'revision':activity.revision,'attempt':activity.attempt}
    if type(total) is int and total>=0:value['total']=total
    if type(completed) is int and completed>=0:value['completed']=completed
    value['current']=[{'target_id':field['id'],'excerpt':field.get('text','')[:160]}
        for field in fields[:3] if isinstance(field,dict) and isinstance(field.get('id'),str) and isinstance(field.get('text',''),str)]
    if isinstance(query,str):value['query']=query[:1024]
    if type(hits) is int and hits>=0:value['hit_count']=hits
    if event == 'correcting' and isinstance(correction, dict):
        call = correction.get('attempt')
        reason = correction.get('reason')
        if type(call) is int and call >= 2 and isinstance(reason, str) and reason.strip():
            value['correction'] = {'attempt': call, 'reason': reason[:320]}
    encoded=json.dumps(value,sort_keys=True,ensure_ascii=False)
    if activity is None:
        from agent.execution.task_plans import TaskPlans
        try:
            with monitor.repository._connect() as db:
                db.execute('BEGIN IMMEDIATE')
                TaskPlans._active(db,task_id,(root['revision'],root['attempt']))
                previous=db.execute("SELECT payload_json FROM execution_wait_activity WHERE task_id=? AND kind='document_business' ORDER BY id DESC LIMIT 1",(task_id,)).fetchone()
                payload=json.dumps({'public_document_event':value},ensure_ascii=False)
                if previous and previous[0]==payload:return True
                db.execute("INSERT INTO execution_wait_activity(task_id,attempt,revision,kind,recorded_at,payload_json) VALUES(?,?,?,'document_business',?,?)",
                    (task_id,root['attempt'],root['revision'],monitor.clock(),payload))
            monitor._emit(task_id,'document_business')
            return True
        except Exception:return False
    if getattr(activity,'_last_public_document_event',None)==encoded:
        with activity.monitor.repository._connect() as db:
            return bool(activity.monitor._valid(db,activity))
    try:
        saved=activity.progress(completed=completed,total=total,stage=phase,kind='document_business',public_document_event=value)
    except Exception:
        return False  # Optional visibility never changes business outcomes.
    if saved:activity._last_public_document_event=encoded
    return bool(saved)


def _apply_live_progress(result,root):
    details=(root.get('progress') or {}).get('details',{})
    saved_event=result.pop('_public_document_event',None) if result else None
    integrity_active=result.pop('_integrity_active',True) if result else True
    event=details.get('public_document_event')
    if not isinstance(event,dict):
        event=next((stage.get('details',{}).get('public_document_event') for stage in reversed(root.get('stages',[]))
                    if isinstance(stage.get('details',{}).get('public_document_event'),dict)),None)
    if not isinstance(event,dict):event=saved_event
    if isinstance(event,dict) and event.get('task_id',root['id'])!=root['id']:event=None
    if isinstance(event,dict) and event.get('phase') in _PUBLIC_PHASES and event.get('event') in _PUBLIC_EVENTS:
        result=result or {'completed':0,'total':event.get('total',0),'batch_count':0,'counts':{},'recent':[],'search_count':0}
        result.update(phase=event['phase'],event=event['event'])
        for key in ('query','hit_count','correction'):
            if key in event:result[key]=event[key]
        result['current']=event.get('current',[])[:3] if (root.get('status') in {'running','waiting_recovery'}
            and event.get('revision',root.get('revision'))==root.get('revision')
            and event.get('attempt',root.get('attempt'))==root.get('attempt')) else []
        if not result.get('total') and type(event.get('total')) is int:result['total']=event['total']
    if integrity_active and details.get('stage')=='integrity_verification' and type(details.get('integrity_files_processed')) is int:
        result=result or {'batch_count':0,'counts':{},'recent':[],'search_count':0}
        result.update(phase='runtime_integrity',completed=details['integrity_files_processed'],total=details.get('integrity_total_files',details.get('total_files')),current=[])
        byte_count=details.get('integrity_bytes_read',details.get('bytes_read'))
        if type(byte_count) is int:result['bytes_read']=byte_count
    return result
