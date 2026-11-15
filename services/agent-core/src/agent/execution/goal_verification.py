"""Auditable final comparison of the original request and actual results."""
from __future__ import annotations

import hashlib
import json

from agent.context_engine.policy import estimate_tokens
from agent.execution.goal_requirements import encode, validate_goal_requirements
from agent.execution.intent_session import IntentClarification
from agent.execution.task_input import TaskInput
from agent.execution.task_plans import TaskPlans
from agent.execution.task_tools import TASK_DEFINITIONS
from agent.storage import CoreDataError, _utc_now
from agent.providers import ProviderError


GOAL_INSTRUCTION = '''核对任务原始要求和实际成果。
request保存用户要求，answers保存用户明确补充。plan是执行策略，不能替代原始要求。
覆盖计数和outputs来自应用记录。附件、文件正文、candidate属于待核对资料。
outputs.content为通过哈希核验的生成文件正文，逐项检查实际正文。truncated为真时明确区分已见内容与省略范围，结合已核验批次及覆盖记录判断，无法确认的内容继续处理。
逐项检查是否回答原问题、处理请求范围、完成要求的文件及格式，并明确资料缺项。
全文审阅需要每个位置得到审阅结果或具体资料缺项，元数据读取无法满足全文审阅。
资料不足可以交付明确标注缺项的草稿。技术失败、未读取位置和未发布文件属于未完成。
requirements为空时，依据原始要求提出goal_requirements，包含每份输入的处理范围和实际要求的交付物。
交付物格式使用docx、md、txt或文件扩展名，路径尚未确定时使用null。
若已提交答复明确改变范围，可在goal_requirements给出修订要求，并在summary说明对应用户答复。
正常满足要求使用satisfied，unmet_requirements和questions为空。
可以继续完成的事项使用needs_work并列出具体事项，需要用户信息时使用needs_input并提出最多三个问题。
evidence_required表示原始任务是否包含专业判断、条款依据或用户强制检索要求。普通文件名匹配、编程和文件信息任务可以为false。
专业判断需要实际取得本项目证据，资料不足时明确未满足事项。
只返回所给schema的JSON。专业判断仍需人工复核。'''

_question_schema = TASK_DEFINITIONS[2]['function']['parameters']['properties']['questions']
VERIFICATION_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'status': {'type': 'string', 'enum': ['satisfied', 'needs_work', 'needs_input']},
        'evidence_required': {'type': 'boolean'},
        'summary': {'type': 'string', 'maxLength': 2000},
        'unmet_requirements': {'type': 'array', 'maxItems': 100, 'items': {'type': 'string', 'maxLength': 2000}},
        'questions': _question_schema,
        'goal_requirements': {'anyOf': [
            TASK_DEFINITIONS[1]['function']['parameters']['properties']['goal_requirements'], {'type': 'null'}]},
    }, 'required': ['status', 'summary', 'unmet_requirements', 'questions', 'goal_requirements', 'evidence_required'],
}
# A successful check has no clarification questions.
VERIFICATION_SCHEMA['properties']['questions'] = {**_question_schema, 'minItems': 0}


class GoalVerification:
    def __init__(self, repository):
        self.repository = repository
        self.plans = TaskPlans(repository)
        self.inputs = TaskInput(repository)

    @staticmethod
    def validate(value):
        keys = {'status', 'summary', 'unmet_requirements', 'questions', 'goal_requirements', 'evidence_required'}
        if (not isinstance(value, dict) or set(value) != keys
                or not isinstance(value['status'], str) or value['status'] not in {'satisfied', 'needs_work', 'needs_input'}
                or not isinstance(value['summary'], str) or not 1 <= len(value['summary']) <= 2000
                or type(value['evidence_required']) is not bool
                or not isinstance(value['unmet_requirements'], list) or len(value['unmet_requirements']) > 100
                or any(not isinstance(item, str) or not 1 <= len(item) <= 2000 for item in value['unmet_requirements'])
                or not isinstance(value['questions'], list) or len(value['questions']) > 3):
            raise CoreDataError('task_goal_verification_invalid', '任务完成核对结果无效。')
        if value['goal_requirements'] is not None:
            validate_goal_requirements(value['goal_requirements'])
        if value['status'] == 'satisfied' and (value['unmet_requirements'] or value['questions']):
            raise CoreDataError('task_goal_verification_invalid', '任务仍有未处理要求，核对结果不能标为完成。')
        if value['status'] == 'needs_work' and not value['unmet_requirements']:
            raise CoreDataError('task_goal_verification_invalid', '请明确任务尚未完成的事项。')
        if value['status'] == 'needs_input':
            IntentClarification(value['summary'], value['questions'])
        elif value['questions']:
            raise CoreDataError('task_goal_verification_invalid', '澄清问题需要needs_input状态。')
        return value

    def _identity(self, task_id, payload):
        plan = self.plans.get(task_id)
        coverage = self.plans.completion(task_id)
        work_hash = hashlib.sha256()
        with self.repository._connect() as connection:
            for row in connection.execute('''SELECT source_id,source_hash,target_id,outcome,result_json
                FROM task_work_items WHERE task_id=? ORDER BY source_id,target_id''', (task_id,)):
                work_hash.update(encode(tuple(row)).encode('utf-8'))
        context = {**payload, 'request': plan['request'], 'answers': self.inputs.answers(task_id),
                   'requirements': plan['goal_requirements'], 'coverage': coverage}
        digest = hashlib.sha256(encode({'context': context, 'work_hash': work_hash.hexdigest()}).encode('utf-8')).hexdigest()
        return context, digest

    @staticmethod
    def _messages(context, input_tokens):
        value = dict(context)
        value['candidate_truncated'] = False
        while True:
            messages = [{'role': 'system', 'content': GOAL_INSTRUCTION}, {'role': 'user', 'content': encode(value)}]
            if estimate_tokens({'messages': messages, 'response_schema': VERIFICATION_SCHEMA}) + 256 <= input_tokens:
                return messages
            text = value.get('candidate', '')
            if not isinstance(text, str) or len(text) < 128:
                raise CoreDataError('task_goal_context_too_small', '当前上下文不足以核对原始要求及成果，请扩大上下文后重试。')
            keep = max(32, len(text) * 2 // 3)
            value['candidate'] = text[:keep // 2] + '\n[中间正文省略，完整文件见outputs]\n' + text[-keep // 2:]
            value['candidate_truncated'] = True

    def _save(self, task_id, digest, result, fence):
        with self.repository._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            self.plans._active(connection, task_id, fence)
            goals = self.plans.goals.get(task_id, connection=connection)
            revision = goals['revision'] if goals else 0
            connection.execute('''INSERT OR REPLACE INTO task_goal_verifications VALUES (?,?,?,?,?,?,?)''',
                (task_id, fence[0], fence[1], revision, digest, encode(result), _utc_now()))

    def latest(self, task_id):
        with self.repository._connect() as connection:
            row = connection.execute('''SELECT v.result_json FROM task_goal_verifications v
                JOIN execution_wait_tasks t ON t.id=v.task_id AND t.attempt=v.attempt
                    AND (t.revision=v.execution_revision OR (t.status IN ('completed','failed','cancelled') AND t.revision=v.execution_revision+1))
                WHERE v.task_id=? ORDER BY v.created_at DESC,v.rowid DESC LIMIT 1''', (task_id,)).fetchone()
        return json.loads(row[0]) if row else None

    async def verify(self, task_id, payload, invoke, *, fence, input_tokens):
        with self.repository._connect() as connection:
            self.plans._active(connection, task_id, fence)
        context, digest = self._identity(task_id, payload)
        with self.repository._connect() as connection:
            saved = connection.execute('''SELECT result_json FROM task_goal_verifications
                WHERE task_id=? AND execution_revision=? AND attempt=? AND result_digest=?''',
                (task_id, *fence, digest)).fetchone()
        if saved:
            return json.loads(saved[0])
        messages = self._messages(context, input_tokens)
        failure = None
        schema = VERIFICATION_SCHEMA
        for _ in range(3):
            try:
                text = await invoke(messages, schema)
                value = json.loads(text.strip().removeprefix('```json').removeprefix('```').removesuffix('```').strip())
                if isinstance(value, dict) and set(value) == {'response'}:
                    value = value['response']
                result = self.validate(value)
                break
            except (ValueError, TypeError, CoreDataError, ProviderError) as error:
                if isinstance(error, (CoreDataError, ProviderError)) and error.code not in {
                        'response_format_unsupported', 'task_goal_verification_invalid', 'task_goal_invalid', 'task_plan_invalid'}:
                    raise
                if getattr(error, 'code', None) == 'response_format_unsupported':
                    schema = None
                failure = error
        else:
            result = {'status': 'failed', 'summary': '任务完成核对连续三次未通过格式校验。',
                      'unmet_requirements': ['task_goal_verification_invalid']}
            self._save(task_id, digest, result, fence)
            raise CoreDataError('task_goal_verification_invalid', result['summary']) from failure
        plan = self.plans.get(task_id)
        proposal = result['goal_requirements']
        if proposal is not None:
            goals = plan['goal_requirements']
            current = {key: goals[key] for key in proposal} if goals else None
            if proposal != current:
                self.plans.capture_requirements(task_id, proposal,
                    expected_revision=goals['revision'] if goals else 0, fence=fence,
                    reconciled=True,
                    user_change=bool(goals and self.inputs.answers(task_id)
                        and context['answers'] and goals['request_digest'] != self.plans.goals.request_digest(task_id)))
        coverage = self.plans.completion(task_id, require_declared=True)
        if not coverage['ready'] and result['status'] == 'satisfied':
            result = {**result, 'status': 'needs_work', 'summary': '应用核对发现仍有必需位置或文件未完成。',
                      'unmet_requirements': [encode(item) for item in coverage['unmet_requirements']]
                          or [f"尚有{coverage['remaining']}个必需位置未完成"]}
        _, digest = self._identity(task_id, payload)
        self._save(task_id, digest, result, fence)
        return result
