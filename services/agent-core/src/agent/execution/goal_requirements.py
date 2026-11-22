"""User-goal requirements survive revisions of the execution strategy."""
from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath

from agent.storage import CoreDataError, _utc_now


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def format_name(value):
    value = value.lower() if isinstance(value, str) else value
    return {'markdown': 'md', 'text': 'txt'}.get(value, value)


def covers_scope(candidate, required):
    if candidate['source_id'] != required['source_id']:
        return False
    if candidate['operation'] != required['operation']:
        return False
    if not set(required.get('metadata_keys', [])) <= set(candidate.get('metadata_keys', [])):
        return False
    if required['scope'] == 'excluded' or candidate['scope'] == 'excluded':
        return candidate['scope'] == required['scope']
    if required['scope'] == 'metadata':
        return True
    if (required.get('selector') or {}).get('kind') == 'selection_pending':
        if candidate['scope'] != 'selected':
            return False
        if candidate['target_ids']:
            count = required['selector'].get('count')
            return count is None or len(candidate['target_ids']) == count
        return candidate.get('selector') == required['selector']
    if candidate['scope'] == 'full':
        return True
    if required.get('selector'):
        candidate_selector = candidate.get('selector') or {}
        return candidate_selector.get('kind') == required['selector']['kind'] and candidate_selector.get('count', 0) >= required['selector']['count']
    return (candidate['scope'] == required['scope'] == 'selected'
            and set(candidate['target_ids']) >= set(required['target_ids']))


def validate_goal_requirements(value):
    from agent.execution.task_plans import validate_requirements
    if not isinstance(value, dict) or set(value) != {'source_scopes', 'deliverables', 'success_criteria'}:
        raise CoreDataError('task_goal_invalid', '请明确任务要求的资料范围、交付物和完成条件。')
    outputs = value['deliverables']
    if not isinstance(outputs, list) or len(outputs) > 100:
        raise CoreDataError('task_goal_invalid', '任务交付物清单无效。')
    paths = []
    for item in outputs:
        if (not isinstance(item, dict) or set(item) != {'path', 'format'}
                or item['path'] is not None and not isinstance(item['path'], str)
                or item['format'] is not None and (not isinstance(item['format'], str)
                    or not item['format'].isascii() or not item['format'].isalnum()
                    or len(item['format']) > 32)
                or item['path'] is None and item['format'] is None):
            raise CoreDataError('task_goal_invalid', '请提供交付物格式或项目内路径。')
        if item['path'] is not None:
            paths.append({'path': item['path']})
    validate_requirements(value['source_scopes'], paths)
    criteria = value['success_criteria']
    if not isinstance(criteria, list) or len(criteria) > 100 or any(
            not isinstance(item, str) or not 1 <= len(item) <= 2000 for item in criteria):
        raise CoreDataError('task_goal_invalid', '任务完成条件无效。')
    return value


class GoalRequirements:
    def __init__(self, repository):
        self.repository = repository

    def get(self, task_id, *, connection=None):
        if connection is None:
            with self.repository._connect() as reader:
                return self.get(task_id, connection=reader)
        row = connection.execute('''SELECT * FROM task_goal_requirements WHERE task_id=?
            ORDER BY revision DESC LIMIT 1''', (task_id,)).fetchone()
        if row is None:
            return None
        return {'revision': row['revision'], 'request_digest': row['request_digest'],
                'provenance': row['provenance'], **json.loads(row['requirements_json'])}

    def request_digest(self, task_id, *, connection=None):
        if connection is None:
            with self.repository._connect() as reader:
                return self.request_digest(task_id, connection=reader)
        request = connection.execute('SELECT request_json FROM task_plan_revisions WHERE task_id=? ORDER BY revision LIMIT 1',
                                     (task_id,)).fetchone()
        answers = self.answers(task_id, connection=connection)
        return hashlib.sha256(encode({'request': json.loads(request[0]), 'answers': answers}).encode('utf-8')).hexdigest()

    def answers(self, task_id, *, connection=None):
        if connection is None:
            with self.repository._connect() as reader:
                return self.answers(task_id, connection=reader)
        intent = connection.execute('''SELECT i.state_json FROM execution_wait_tasks t
            JOIN intent_tasks i ON i.id=t.intent_task_id WHERE t.id=?''', (task_id,)).fetchone()
        recorded = json.loads(intent[0]).get('answers', {}) if intent else {}
        answers = [{'question': key, 'answer': value} for key, value in recorded.items()
                   if key != 'scope' or value not in [item for name, item in recorded.items() if name != 'scope']]
        for row in connection.execute('''SELECT payload_json,answers_json FROM task_questions
                WHERE task_id=? AND status='answered' ORDER BY rowid''', (task_id,)):
            response = json.loads(row['answers_json'])
            answers.extend({'question': question['question'], 'answer': response[question['id']]}
                           for question in json.loads(row['payload_json'])['questions'])
        return answers

    def capture(self, connection, task_id, value, *, expected_revision, user_change=False, reconciled=False):
        validate_goal_requirements(value)
        current = self.get(task_id, connection=connection)
        revision = current['revision'] if current else 0
        if type(expected_revision) is not int or expected_revision != revision:
            raise CoreDataError('task_goal_revision_changed', '任务要求已更新，请读取当前版本。')
        request = connection.execute('SELECT request_json FROM task_plan_revisions WHERE task_id=? ORDER BY revision LIMIT 1',
                                     (task_id,)).fetchone()
        if request is None:
            raise CoreDataError('task_goal_invalid', '任务原始要求尚未保存。')
        original = json.loads(request[0])
        from agent.context_engine.attachment_references import task_attachment_ids
        allowed = task_attachment_ids(connection, task_id, original)
        allowed.update(row[0] for row in connection.execute('SELECT source_id FROM task_sources WHERE task_id=?', (task_id,)))
        allowed.update(row[0] for row in connection.execute('SELECT alias FROM task_source_aliases WHERE task_id=?', (task_id,)))
        if any(scope['source_id'] not in allowed for scope in value['source_scopes']):
            raise CoreDataError('task_goal_invalid', '任务要求包含未授权的资料。')
        request_digest = self.request_digest(task_id, connection=connection)
        if user_change and (not current or current['request_digest'] == request_digest):
            raise CoreDataError('task_goal_change_requires_input', '修改用户要求需要本任务的新答复记录。')
        allow_reduction = user_change or reconciled and current and current['provenance'] == 'model_inferred'
        floors = []
        if current and not allow_reduction:
            floors.append((current, False))
        confirmed = connection.execute('''SELECT requirements_json FROM task_goal_requirements
            WHERE task_id=? AND provenance='user_clarification' ORDER BY revision DESC LIMIT 1''', (task_id,)).fetchone()
        if confirmed and not user_change:
            floors.append((json.loads(confirmed[0]), True))
        aliases = dict(connection.execute('SELECT alias,source_id FROM task_source_aliases WHERE task_id=?', (task_id,)))
        def canonical_scope(scope):
            return {**scope, 'source_id': aliases.get(scope['source_id'], scope['source_id'])}

        for floor, user_confirmed in floors:
            if any(not any(covers_scope(canonical_scope(candidate), canonical_scope(required)) for candidate in value['source_scopes'])
                   for required in floor['source_scopes']):
                raise CoreDataError('task_goal_change_requires_input', '缩小原任务范围需要用户明确补充要求。')
            remaining = list(value['deliverables'])
            for required in floor['deliverables']:
                matched = next((item for item in remaining if
                    (not required['format'] or format_name(item['format']) == format_name(required['format']))
                    and (not required['path'] or not user_confirmed and required['path'] not in original['content']
                         or item['path'] == required['path'])), None)
                if matched is None:
                    raise CoreDataError('task_goal_change_requires_input', '取消原任务交付物需要用户明确补充要求。')
                remaining.remove(matched)
            if user_confirmed and not set(floor['success_criteria']) <= set(value['success_criteria']):
                raise CoreDataError('task_goal_change_requires_input', '修改用户已确认的完成条件需要新的答复。')
        connection.execute('INSERT INTO task_goal_requirements VALUES (?,?,?,?,?,?)',
            (task_id, revision + 1, request_digest, encode(value),
             'user_clarification' if user_change else 'model_inferred', _utc_now()))
        return self.get(task_id, connection=connection)

    @staticmethod
    def match_outputs(requirements, published, format_proofs, *, aliases=None):
        aliases = aliases or {}
        remaining = set(published)
        matched, missing = [], []
        for index, output in enumerate(requirements):
            path = output['path'].replace('\\', '/').removeprefix('./').casefold() if output['path'] else None
            wanted_format = format_name(output['format'].lower()) if output['format'] else None
            selected = next((item for item in sorted(remaining) if
                (path is None or item == path) and (wanted_format is None
                    or PurePosixPath(item).suffix.lower() == '.' + wanted_format)
                and (wanted_format != 'docx' or format_proofs.get(item) == 'docx')), None)
            if selected is None:
                missing.append({'code': 'deliverable_missing', 'index': index, **output})
            else:
                identity = aliases.get(selected, selected)
                remaining = {item for item in remaining if aliases.get(item, item) != identity}
                matched.append(selected)
        return matched, missing
