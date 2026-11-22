"""Business goals and coverage, independent of immutable model-call routes."""
from __future__ import annotations

import json
from collections import Counter

from agent.storage import CoreDataError, _utc_now
from agent.execution.goal_requirements import GoalRequirements, covers_scope


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _steps(values):
    if not isinstance(values, list) or len(values) > 100:
        raise CoreDataError('task_plan_invalid', '任务计划需要有效的步骤清单。')
    steps = {}
    for value in values:
        if (not isinstance(value, dict) or set(value) != {'id', 'description', 'depends_on'}
                or not isinstance(value['id'], str) or not 1 <= len(value['id']) <= 128
                or value['id'] in steps or not isinstance(value['description'], str)
                or not 1 <= len(value['description']) <= 2000
                or not isinstance(value['depends_on'], list)
                or any(not isinstance(key, str) for key in value['depends_on'])):
            raise CoreDataError('task_plan_invalid', '任务步骤标识、说明或依赖无效。')
        steps[value['id']] = value
    resolved = set()
    while len(resolved) < len(steps):
        ready = {key for key, value in steps.items() if set(value['depends_on']) <= resolved}
        if ready <= resolved:
            raise CoreDataError('task_plan_invalid', '任务步骤包含缺失或循环依赖。')
        resolved.update(ready)
    return values


def validate_requirements(source_scopes, deliverables):
    if not isinstance(source_scopes, list) or len(source_scopes) > 32 or not isinstance(deliverables, list) or len(deliverables) > 100:
        raise CoreDataError('task_plan_invalid', '任务资料范围或交付物无效。')
    seen = set()
    for scope in source_scopes:
        if (not isinstance(scope, dict) or not {'source_id', 'operation', 'scope', 'target_ids', 'reason'} <= set(scope) or set(scope) - {'source_id', 'operation', 'scope', 'target_ids', 'reason', 'selector', 'metadata_keys'}
                or not isinstance(scope['source_id'], str) or not 1 <= len(scope['source_id']) <= 1024
                or not isinstance(scope['operation'], str) or not isinstance(scope['scope'], str)
                or (scope['source_id'], scope.get('operation')) in seen or scope['operation'] not in {'read', 'review'}
                or scope['scope'] not in {'full', 'selected', 'metadata', 'excluded'}
                or not isinstance(scope['target_ids'], list) or len(scope['target_ids']) > 10000
                or any(not isinstance(key, str) or not 1 <= len(key) <= 256 for key in scope['target_ids'])
                or len(set(scope['target_ids'])) != len(scope['target_ids'])
                or (scope['scope'] == 'selected') != bool(scope['target_ids'] or scope.get('selector'))
                or not isinstance(scope['reason'], str) or len(scope['reason']) > 2000
                or scope['scope'] == 'excluded' and not scope['reason'].strip()):
            raise CoreDataError('task_plan_invalid', '请明确每份资料的处理范围和位置。')
        selector = scope.get('selector')
        if selector is not None:
            if scope['scope'] != 'selected' or scope['target_ids'] or not isinstance(selector, dict):
                raise CoreDataError('task_selector_invalid', '范围选择器无效。')
            prefix = (set(selector) == {'kind', 'count'} and selector['kind'] == 'prefix_nonempty'
                      and type(selector['count']) is int and 1 <= selector['count'] <= 10000)
            pending = (set(selector) in ({'kind', 'condition'}, {'kind', 'condition', 'count'})
                       and selector['kind'] == 'selection_pending'
                       and isinstance(selector['condition'], str) and 1 <= len(selector['condition'].strip()) <= 2000
                       and ('count' not in selector or type(selector['count']) is int and 1 <= selector['count'] <= 10000))
            if not prefix and not pending:
                raise CoreDataError('task_selector_invalid', '范围选择器无效。')
        if 'metadata_keys' in scope and (not isinstance(scope['metadata_keys'], list) or any(not isinstance(key, str) for key in scope['metadata_keys']) or set(scope['metadata_keys']) - {'template_id','total'}):
            raise CoreDataError('task_selector_invalid', '元数据要求无效。')
        seen.add((scope['source_id'], scope['operation']))
    for output in deliverables:
        if (not isinstance(output, dict) or set(output) != {'path'} or not isinstance(output['path'], str)
                or not output['path'] or len(output['path']) > 1024 or '\0' in output['path']
                or ':' in output['path'] or output['path'].startswith(('/', '\\'))
                or '..' in output['path'].replace('\\', '/').split('/')):
            raise CoreDataError('task_plan_invalid', '交付物需要项目内相对路径。')


def _path_key(path):
    return path.replace('\\', '/').removeprefix('./').casefold()


class TaskPlans:
    def __init__(self, repository):
        self.repository = repository
        self.goals = GoalRequirements(repository)

    @staticmethod
    def _active(connection, task_id, fence=None):
        row = connection.execute('SELECT status,revision,attempt FROM execution_wait_tasks WHERE id=?', (task_id,)).fetchone()
        if row is None or row['status'] not in {'running', 'waiting_recovery'}:
            raise CoreDataError('task_plan_inactive', '当前任务已结束，无法更新计划或结果。')
        if fence is not None and (row['revision'], row['attempt']) != fence:
            raise CoreDataError('task_plan_stale', '任务执行身份已变化，旧结果已丢弃。')

    def create(self, task_id, request):
        goal = request.get('content')
        if not isinstance(goal, str):
            raise CoreDataError('invalid_params', '任务内容需要文本。')
        # Keep attachment identities and original instructions, never binary data.
        saved = {key: request[key] for key in ('content', 'conversation_id', 'project_id', 'rag_mode', 'attachment_references') if key in request}
        attachments = request.get('attachments')
        saved['attachments'] = [{key: item[key] for key in ('id', 'name', 'sha256', 'purpose') if key in item}
                                for item in attachments if isinstance(item, dict)] if isinstance(attachments, list) else []
        goal = goal or '处理所选附件'
        with self.repository._connect() as connection:
            connection.execute('''INSERT OR IGNORE INTO task_plan_revisions
                VALUES (?,0,?,?,?,?)''', (task_id, goal, _json(saved),
                _json({'steps': [], 'success_criteria': []}), _utc_now()))
        return self.get(task_id)

    def get(self, task_id):
        with self.repository._connect() as connection:
            row = connection.execute('''SELECT * FROM task_plan_revisions WHERE task_id=?
                ORDER BY revision DESC LIMIT 1''', (task_id,)).fetchone()
        if row is None:
            return None
        return {'task_id': task_id, 'revision': row['revision'], 'goal': row['goal'],
                'request': json.loads(row['request_json']), 'source_scopes': [], 'deliverables': [],
                'requirements_declared': False, **json.loads(row['plan_json']),
                'goal_requirements': self.goals.get(task_id)}

    def capture_requirements(self, task_id, requirements, *, expected_revision, fence=None, user_change=False, reconciled=False):
        with self.repository._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            self._active(connection, task_id, fence)
            return self.goals.capture(connection, task_id, requirements, expected_revision=expected_revision,
                                      user_change=user_change, reconciled=reconciled)

    def update(self, task_id, *, expected_revision, steps, success_criteria,
               source_scopes=None, deliverables=None, goal_requirements=None, expected_goal_revision=None, fence=None):
        steps = _steps(steps)
        if (type(expected_revision) is not int or not isinstance(success_criteria, list)
                or len(success_criteria) > 100 or any(not isinstance(item, str)
                or not 1 <= len(item) <= 2000 for item in success_criteria)):
            raise CoreDataError('task_plan_invalid', '任务完成条件或修订无效。')
        with self.repository._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            self._active(connection, task_id, fence)
            current = connection.execute('''SELECT * FROM task_plan_revisions WHERE task_id=?
                ORDER BY revision DESC LIMIT 1''', (task_id,)).fetchone()
            if current is None or current['revision'] != expected_revision:
                raise CoreDataError('task_plan_revision_changed', '任务计划修订已变化，请读取最新计划。')
            previous = json.loads(current['plan_json'])
            declarations = source_scopes is not None or deliverables is not None
            scopes = source_scopes if source_scopes is not None else previous.get('source_scopes', [])
            outputs = deliverables if deliverables is not None else previous.get('deliverables', [])
            validate_requirements(scopes, outputs)
            if (goal_requirements is None) != (expected_goal_revision is None):
                raise CoreDataError('task_goal_invalid', '任务要求与要求修订号需要同时提交。')
            if goal_requirements is not None:
                self.goals.capture(connection, task_id, goal_requirements, expected_revision=expected_goal_revision)
            if previous.get('requirements_declared') and self.goals.get(task_id, connection=connection) is None and (
                    any(not any(covers_scope(candidate, scope) for candidate in scopes)
                        for scope in previous.get('source_scopes', []))
                    or any(output not in outputs for output in previous.get('deliverables', []))):
                raise CoreDataError('task_plan_invalid', '原任务要求的资料范围与交付物需要保留。')
            from agent.context_engine.attachment_references import task_attachment_ids
            allowed = task_attachment_ids(connection, task_id, json.loads(current['request_json']))
            allowed.update(row[0] for row in connection.execute('SELECT source_id FROM task_sources WHERE task_id=?', (task_id,)))
            allowed.update(row[0] for row in connection.execute('SELECT alias FROM task_source_aliases WHERE task_id=?', (task_id,)))
            if any(scope['source_id'] not in allowed for scope in scopes):
                raise CoreDataError('task_plan_invalid', '任务资料不属于当前请求。')
            connection.execute('INSERT INTO task_plan_revisions VALUES (?,?,?,?,?,?)',
                (task_id, expected_revision + 1, current['goal'], current['request_json'],
                 _json({'steps': steps, 'success_criteria': success_criteria, 'source_scopes': scopes,
                        'deliverables': outputs, 'requirements_declared': declarations or previous.get('requirements_declared', False)}), _utc_now()))
        return self.get(task_id)

    def register_source(self, task_id, source_id, artifact_id, source_hash, fields, *, operation, fence=None):
        if operation not in {'read', 'review'}:
            raise CoreDataError('task_source_invalid', '任务资料操作无效。')
        self.register_items(task_id, artifact_id, source_hash, fields, fence=fence)
        with self.repository._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            self._active(connection, task_id, fence)
            from agent.execution.source_inventory import SourceInventory
            canonical = SourceInventory(self.repository).canonical(task_id, source_id, connection)
            connection.execute('DELETE FROM task_review_proofs WHERE task_id=? AND source_id=? AND source_hash<>?',
                               (task_id, canonical, source_hash))
            if operation == 'review' and not connection.execute('''SELECT 1 FROM task_sources
                    WHERE task_id=? AND artifact_id=? AND operation='review' AND source_hash=?''',
                    (task_id, artifact_id, source_hash)).fetchone():
                connection.execute("UPDATE task_work_items SET outcome='pending',result_json=NULL WHERE task_id=? AND source_id=? AND source_hash=?",
                                   (task_id, artifact_id, source_hash))
            connection.execute('''INSERT INTO task_sources VALUES (?,?,?,?,?)
                ON CONFLICT(task_id,source_id,operation) DO UPDATE SET
                artifact_id=excluded.artifact_id,source_hash=excluded.source_hash''',
                (task_id, source_id, operation, artifact_id, source_hash))

    def register_items(self, task_id, source_id, source_hash, items, *, fence=None):
        if not isinstance(source_id, str) or not source_id or not isinstance(source_hash, str) or not source_hash:
            raise CoreDataError('task_source_invalid', '任务资料身份无效。')
        if not isinstance(items, list) or any(not isinstance(item, dict) or not isinstance(item.get('id'), str)
                                             or not item['id'] for item in items):
            raise CoreDataError('task_items_invalid', '任务位置清单无效。')
        with self.repository._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            self._active(connection, task_id, fence)
            connection.execute('DELETE FROM task_work_items WHERE task_id=? AND source_id=? AND source_hash<>?',
                               (task_id, source_id, source_hash))
            for item in items:
                encoded = _json(item)
                connection.execute('''INSERT INTO task_work_items
                    (task_id,source_id,source_hash,target_id,content_json,updated_at) VALUES (?,?,?,?,?,?)
                    ON CONFLICT(task_id,source_id,target_id) DO UPDATE SET
                    outcome=CASE WHEN content_json=excluded.content_json THEN outcome ELSE 'pending' END,
                    result_json=CASE WHEN content_json=excluded.content_json THEN result_json ELSE NULL END,
                    content_json=excluded.content_json,updated_at=excluded.updated_at''',
                    (task_id, source_id, source_hash, item['id'], encoded, _utc_now()))

    @staticmethod
    def current_evidence_generation(connection, task_id):
        row = connection.execute('''SELECT t.status,c.project_id,COALESCE(p.generation,0) AS generation
            FROM execution_wait_tasks t JOIN model_runs r ON r.id=t.run_id
            JOIN conversations c ON c.id=r.conversation_id
            LEFT JOIN rag_projects p ON p.project_id=c.project_id WHERE t.id=?''', (task_id,)).fetchone()
        return row['generation'] if row and row['status'] != 'completed' and row['project_id'] else None

    def record_results(self, task_id, source_id, source_hash, results, *, fence=None,
                       authorization_source_id=None, evidence_generation=None):
        if evidence_generation is not None and (type(evidence_generation) is not int or evidence_generation < 0):
            raise CoreDataError('task_result_invalid', '审阅依据版本身份无效。')
        with self.repository._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            self._active(connection, task_id, fence)
            reviewers = [row[0] for row in connection.execute('''SELECT source_id FROM task_sources
                WHERE task_id=? AND artifact_id=? AND source_hash=? AND operation='review' ''',
                (task_id, source_id, source_hash))]
            if authorization_source_id is not None:
                if authorization_source_id not in reviewers:
                    raise CoreDataError('task_source_changed', '审阅结果与授权资料身份不一致。')
                reviewers = [authorization_source_id]
            if len(reviewers) > 1:
                raise CoreDataError('task_source_alias_ambiguous', '请明确审阅结果所属的授权附件。')
            for result in results:
                status = result.get('status')
                if status not in {'reviewed', 'pending'}:
                    raise CoreDataError('task_result_invalid', '任务结果状态无效。')
                outcome = ('failed' if result.get('error_code') else
                           'gap' if status == 'pending' or result.get('gaps') or result.get('version_issues') else 'done')
                cursor = connection.execute('''UPDATE task_work_items SET outcome=?,result_json=?,updated_at=?
                    WHERE task_id=? AND source_id=? AND source_hash=? AND target_id=?''',
                    (outcome, _json(result), _utc_now(), task_id, source_id, source_hash, result.get('target_id')))
                if cursor.rowcount != 1:
                    raise CoreDataError('task_source_changed', '任务资料或位置身份已变化，请重新读取。')
                if reviewers:
                    from agent.execution.source_inventory import SourceInventory
                    canonical = SourceInventory(self.repository).canonical(task_id, reviewers[0], connection)
                    connection.execute('''INSERT OR REPLACE INTO task_review_proofs
                        (task_id,source_id,source_hash,target_id,outcome,evidence_generation) VALUES (?,?,?,?,?,?)''',
                        (task_id, canonical, source_hash, result['target_id'], outcome, evidence_generation))

    def items(self, task_id, *, offset=0, limit=100):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise CoreDataError('invalid_params', '任务位置分页范围无效。')
        with self.repository._connect() as connection:
            rows = connection.execute('''SELECT * FROM task_work_items WHERE task_id=?
                ORDER BY source_id,rowid LIMIT ? OFFSET ?''', (task_id, limit, offset)).fetchall()
            total = connection.execute('SELECT count(*) FROM task_work_items WHERE task_id=?', (task_id,)).fetchone()[0]
        return {'items': [{**{key: row[key] for key in ('source_id', 'source_hash', 'target_id', 'outcome')},
                          'content': json.loads(row['content_json']),
                          'result': json.loads(row['result_json']) if row['result_json'] else None} for row in rows],
                'total': total, 'next_offset': offset + limit if offset + limit < total else None}

    def record_file_read(self, task_id, path, source_hash, size, start, end, *, fence=None):
        if (not isinstance(path, str) or not path or not isinstance(source_hash, str) or not source_hash
                or any(type(value) is not int for value in (size, start, end)) or not 0 <= start <= end <= size):
            raise CoreDataError('task_source_invalid', '文件读取身份或范围无效。')
        source_id = 'file:' + path
        with self.repository._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            self._active(connection, task_id, fence)
            previous = connection.execute('''SELECT source_hash,content_json,result_json FROM task_work_items
                WHERE task_id=? AND source_id=? AND target_id='bytes' ''', (task_id, source_id)).fetchone()
            ranges = []
            if previous and previous['source_hash'] == source_hash and json.loads(previous['content_json'])['size'] == size:
                ranges = json.loads(previous['result_json'])['ranges']
            merged = []
            for begin, stop in sorted([*ranges, [start, end]]):
                if merged and begin <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], stop)
                else:
                    merged.append([begin, stop])
            outcome = 'done' if merged == [[0, size]] else 'pending'
            connection.execute('''INSERT INTO task_work_items VALUES (?,?,?,'bytes',?,?,?,?)
                ON CONFLICT(task_id,source_id,target_id) DO UPDATE SET source_hash=excluded.source_hash,
                content_json=excluded.content_json,outcome=excluded.outcome,result_json=excluded.result_json,updated_at=excluded.updated_at''',
                (task_id, source_id, source_hash, _json({'id': 'bytes', 'path': path, 'size': size}), outcome,
                 _json({'ranges': merged}), _utc_now()))
            connection.execute('''INSERT INTO task_sources VALUES (?,?,'read',?,?)
                ON CONFLICT(task_id,source_id,operation) DO UPDATE SET artifact_id=excluded.artifact_id,source_hash=excluded.source_hash''',
                (task_id, path, source_id, source_hash))

    def completion(self, task_id, *, require_declared=False):
        plan = self.get(task_id)
        goals = plan.get('goal_requirements') if plan else None
        unmet = []
        from agent.execution.source_inventory import SourceInventory
        inventory = SourceInventory(self.repository)
        inventory_counts = Counter()
        inventory_total = 0
        with self.repository._connect() as connection:
            rows = connection.execute('SELECT source_id,target_id,outcome FROM task_work_items WHERE task_id=?', (task_id,)).fetchall()
            available = {(row['source_id'], row['target_id']): row['outcome'] for row in rows}
            sources = {(row['source_id'], row['operation']): row['artifact_id'] for row in connection.execute(
                'SELECT * FROM task_sources WHERE task_id=?', (task_id,))}
            required, missing = set(), 0
            unresolved_goal = False
            scopes = goals['source_scopes'] if goals else plan.get('source_scopes', []) if plan else []
            declared = bool(goals or plan and plan['requirements_declared'])
            if declared:
                for scope in scopes:
                    known = connection.execute('SELECT 1 FROM task_source_aliases WHERE task_id=? AND alias=?', (task_id, scope['source_id'])).fetchone()
                    if known:
                        proof = inventory.coverage(task_id, scope)
                        inventory_counts['done'] += proof['completed']
                        inventory_counts['pending'] += proof['remaining']
                        if not proof['ready'] and not proof['remaining']:
                            missing += 1
                            unmet.append({'code': proof.get('reason', 'scope_unresolved'), 'source_id': scope['source_id']})
                        inventory_total += proof['completed'] + proof['remaining']
                        continue
                    if scope.get('selector'):
                        missing += 1
                        unmet.append({'code': 'scope_unresolved', 'source_id': scope['source_id']})
                        continue
                    if scope['scope'] == 'excluded':
                        continue
                    if scope['scope'] == 'metadata':
                        if not scope.get('metadata_keys'):
                            continue
                        missing += 1
                        unmet.append({'code': 'inventory_missing', 'source_id': scope['source_id']})
                        continue
                    artifact = sources.get((scope['source_id'], scope['operation']))
                    if artifact is None:
                        missing += 1
                        unmet.append({'code': 'source_missing', 'source_id': scope['source_id'], 'operation': scope['operation']})
                        continue
                    ids = scope['target_ids'] if scope['scope'] == 'selected' else [key[1] for key in available if key[0] == artifact]
                    if scope['operation'] == 'review':
                        canonical = inventory.canonical(task_id, scope['source_id'], connection)
                        generation = self.current_evidence_generation(connection, task_id)
                        review_outcomes = dict(connection.execute('''SELECT p.target_id,p.outcome FROM task_review_proofs p
                            JOIN task_sources s ON s.task_id=p.task_id AND s.source_hash=p.source_hash
                              AND s.source_id=? AND s.operation='review'
                            WHERE p.task_id=? AND p.source_id=? AND (? IS NULL OR p.evidence_generation=?)''',
                            (scope['source_id'], task_id, canonical, generation, generation)))
                        inventory_counts.update(review_outcomes.get(key, 'pending') for key in ids)
                        inventory_total += len(ids)
                        continue
                    required.update((artifact, key) for key in ids)
                from agent.context_engine.attachment_references import task_attachment_ids
                authorized_ids = task_attachment_ids(connection, task_id, plan['request'])
                missing += len({inventory.canonical(task_id, identity, connection) for identity in authorized_ids}
                               - {inventory.canonical(task_id, scope['source_id'], connection) for scope in scopes})
            else:
                required.update(available)
                missing += int(require_declared)
                has_inventory = connection.execute('SELECT 1 FROM task_inventory_sources WHERE task_id=? LIMIT 1',(task_id,)).fetchone()
                unresolved_goal = bool(has_inventory)
            if require_declared and goals is None:
                missing += 1
                unmet.append({'code': 'goal_requirements_missing'})
            counts = Counter(available.get(key, 'pending') for key in required)
            counts.update(inventory_counts)
            root = connection.execute('SELECT run_id FROM execution_wait_tasks WHERE id=?', (task_id,)).fetchone()
            outputs = goals['deliverables'] if goals else plan.get('deliverables', []) if plan else []
            published = set()
            format_proofs = {}
            aliases = {}
            if outputs and root and root['run_id']:
                aliases.update((_path_key(row[0]), _path_key(row[1])) for row in connection.execute('''
                    SELECT json_extract(o.arguments_json,'$.path'),d.output_path FROM tool_operations o
                    JOIN document_artifacts d ON d.id=json_extract(o.arguments_json,'$.artifact_id') AND d.run_id=o.run_id
                    WHERE o.run_id=? AND o.tool_name='document.export' AND o.status='completed' AND d.status='published'
                    ORDER BY o.sequence,o.rowid''', (root['run_id'],)))
                format_proofs.update((_path_key(row[0]), row[1]) for row in connection.execute(
                    "SELECT output_path,kind FROM document_artifacts WHERE run_id=? AND status='published'", (root['run_id'],)))
                format_proofs.update((_path_key(row[0]), row[1]) for row in connection.execute('''
                    SELECT json_extract(o.arguments_json,'$.path'),d.kind FROM tool_operations o
                    JOIN document_artifacts d ON d.id=json_extract(o.arguments_json,'$.artifact_id') AND d.run_id=o.run_id
                    WHERE o.run_id=? AND o.tool_name='document.export' AND o.status='completed' AND d.status='published'
                    ''', (root['run_id'],)))
                published.update(_path_key(row[0]) for row in connection.execute(
                    "SELECT output_path FROM document_artifacts WHERE run_id=? AND status='published'", (root['run_id'],)))
                published.update(_path_key(row[0]) for row in connection.execute('''
                    SELECT json_extract(o.arguments_json,'$.path') FROM tool_operations o
                    JOIN document_artifacts d ON d.id=json_extract(o.arguments_json,'$.artifact_id') AND d.run_id=o.run_id
                    WHERE o.run_id=? AND o.tool_name='document.export' AND o.status='completed' AND d.status='published'
                    ''', (root['run_id'],)))
                terminal_completed = False
                for operation in connection.execute('''SELECT o.tool_name,o.arguments_json,
                    d.kind AS artifact_kind,d.output_path,d.status AS artifact_status FROM tool_operations o
                    LEFT JOIN document_artifacts d ON d.id=json_extract(o.arguments_json,'$.artifact_id') AND d.run_id=o.run_id
                    WHERE o.run_id=? AND o.status='completed' ORDER BY o.sequence,o.rowid''', (root['run_id'],)):
                    arguments = json.loads(operation['arguments_json'])
                    if operation['tool_name'] == 'document.export' and operation['artifact_status'] == 'published':
                        for path in (arguments['path'], operation['output_path']):
                            published.add(_path_key(path))
                            format_proofs[_path_key(path)] = operation['artifact_kind']
                    elif operation['tool_name'] == 'terminal.run':
                        terminal_completed = True
                    elif operation['tool_name'] == 'fs.read' and terminal_completed:
                        published.add(_path_key(arguments['path']))
                    elif operation['tool_name'] == 'fs.propose_changes':
                        for item in arguments['mutations']:
                            path = _path_key(item['path'])
                            identity = aliases.get(path, path)
                            affected = {key for key in published if aliases.get(key, key) == identity} | {path}
                            for key in affected:
                                format_proofs.pop(key, None)
                            if item['kind'] == 'delete':
                                published.difference_update(affected)
                            elif item['kind'] in {'create_text', 'overwrite_text', 'patch_text', 'copy', 'move'}:
                                published.add(_path_key(item['path']))
                                if item['kind'] == 'move':
                                    published.discard(_path_key(item['source_path']))
            if goals:
                matched, output_gaps = self.goals.match_outputs(outputs, published, format_proofs, aliases=aliases)
                unmet.extend(output_gaps)
                verified_outputs = len(matched)
            else:
                verified_outputs = sum(_path_key(output['path']) in published for output in outputs)
        counts['done'] += verified_outputs
        remaining = counts.get('pending', 0) + counts.get('failed', 0) + missing + len(outputs) - verified_outputs
        result = {'ready': remaining == 0 and not unresolved_goal, 'total': inventory_total + len(required) + missing + len(outputs),
                  'completed': counts.get('done', 0), 'gaps': counts.get('gap', 0), 'remaining': remaining}
        if goals or require_declared:
            result['unmet_requirements'] = unmet
        return result
