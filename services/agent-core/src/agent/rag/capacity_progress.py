"""Bound repeated evidence-capacity failures while other actions stay available."""
import json


def capacity_feedback(run, name, details):
    if not details or details.get('evidence_capacity_exhausted') is not True:
        return None
    counts = run.setdefault('_rag_capacity_failures', {})
    counts[name] = counts.get(name, 0) + 1
    options = details.get('admitted_citations', [])[:4]
    return ('本次证据页超过可用容量。请使用已取得且与问题直接相关的引文；'
            '证据不足的事项明确列为待核。已取得的引文标识：'
            + json.dumps(options, ensure_ascii=False)[:1200])


def note_evidence_progress(run):
    run.pop('_rag_capacity_failures', None)


def paused_tools(run):
    return {name for name, count in run.get('_rag_capacity_failures', {}).items() if count >= 2}


class CapacitySynthesisGuard:
    """A bounded transport decision, never evidence admission or goal completion."""
    def __init__(self):
        self.fingerprint=None
        self.failures=0
        self.controls=0
        self.used=False

    def observe(self, fingerprint, names, *, failures=0, allowed=False, has_evidence=False):
        if self.used:return False
        if not allowed or not has_evidence:
            self.fingerprint=fingerprint;self.failures=self.controls=0
            return False
        if fingerprint!=self.fingerprint:
            self.fingerprint=fingerprint;self.failures=self.controls=0
        if failures:
            self.failures+=failures;self.controls=0
        elif names and set(names)<={'tools.discover','task.update_plan'} and self.failures>=2:
            self.controls+=1
        else:self.controls=0
        if self.failures>=2 and self.controls>=2:
            self.used=True
            return True
        return False


def retrieval_failure(name, success, error_code, output):
    if name not in {'rag.search','rag.read'}:return False
    if not success:return error_code in {'rag_context_too_small','rag_no_evidence','rag_evidence_missing'}
    try:value=json.loads(output)
    except (TypeError,ValueError):return False
    return isinstance(value,dict) and value.get('evidence')==[] and isinstance(value.get('gap'),str)


def text_synthesis_allowed(current, decision, plan):
    import re
    if (current.get('attachments') or current.get('attachment_rejections')
        or decision.get('task_type')!='evidence_answer' or decision.get('operation_hint')
        or decision.get('document_workflow') not in {None,'none'} or decision.get('template_review')):return False
    goals=(plan or {}).get('goal_requirements') or plan or {}
    if goals.get('deliverables') or any(scope.get('operation')=='review' for scope in goals.get('source_scopes',[])):return False
    actions=re.compile(r'审阅|审查|修改|编辑|回填|填写|导出|保存|另存|写入|创建|删除|移动|重命名|更新|修复|编写|下载|上传|发送|提交|打包|部署|安装|复制|转换|运行|执行|技能|联网|上网|网页|浏览器|'
        r'\b(?:review|audit|modify|edit|fill|export|save|write|create|delete|move|rename|update|repair|download|upload|send|submit|package|deploy|install|copy|convert|run|execute|skill|browse)\b',re.I)
    negation=re.compile(r'(?:不要|无需|请勿|禁止|不必|不需要|不|without|do not|don\'t|no)\s*(?:(?:修改|审阅|审查|导出|保存|modify|review|export|save)\s*(?:或|和|及|、|or|and)\s*)*$',re.I)
    return not any(not negation.search(current['content'][max(0,m.start()-80):m.start()]) for m in actions.finditer(current['content']))


def evidence_work_fingerprint(repository, task_id, state):
    import hashlib
    with repository._connect() as db:
        work=[list(row) for row in db.execute('SELECT source_id,source_hash,target_id,outcome,result_json FROM task_work_items WHERE task_id=? ORDER BY source_id,target_id',(task_id,))]
        sources=[list(row) for row in db.execute('SELECT source_id,source_hash FROM task_inventory_sources WHERE task_id=? ORDER BY source_id',(task_id,))]
        questions=[list(row) for row in db.execute('SELECT id,status,answers_json FROM task_questions WHERE task_id=? ORDER BY id',(task_id,))]
    value={'generation':state.get('generation'),'admitted':state.get('admitted',{}),'quotes':state.get('quote_options',{}),
        'evidence':sorted(state.get('evidence',{})), 'work':work,'sources':sources,'questions':questions}
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=True).encode()).hexdigest()
