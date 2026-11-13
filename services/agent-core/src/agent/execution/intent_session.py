"""Persisted preparation, bounded classifier calls and user-driven recovery."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from copy import deepcopy
import hashlib
import json
import time
import uuid

from agent.storage import CoreDataError, _utc_now


current_intent = ContextVar('current_intent', default=None)
LIVE_STATES = {'queued', 'identifying', 'loading'}
RETRY_CODES = {'routing_intent_timeout', 'routing_intent_invalid', 'provider_timeout',
               'provider_unavailable', 'provider_connection_error', 'connection_error',
               'network_error', 'response_format_unsupported', 'connection_reset',
               'stream_stalled', 'network_unavailable', 'request_timeout'}
RECOVERY_CODES = RETRY_CODES | {'core_interrupted', 'resource_exhausted', 'model_not_loaded',
    'model_not_found',
    'model_load_failed', 'runtime_missing', 'engine_missing', 'runtime_integrity_invalid',
    'model_file_changed', 'model_runtime_unavailable'}


class IntentClarification(Exception):
    def __init__(self, summary, questions):
        super().__init__(summary)
        if (not isinstance(summary, str) or not 1 <= len(summary) <= 2000
                or not isinstance(questions, list) or not 1 <= len(questions) <= 3):
            raise CoreDataError('routing_intent_invalid', '任务澄清格式无效。')
        seen = set()
        for question in questions:
            if (not isinstance(question, dict) or set(question) - {'id', 'question', 'options'}
                    or not isinstance(question.get('id'), str) or not 1 <= len(question['id']) <= 80
                    or question['id'] in seen or not isinstance(question.get('question'), str)
                    or not 1 <= len(question['question']) <= 1000):
                raise CoreDataError('routing_intent_invalid', '任务澄清问题无效。')
            options = question.get('options', [])
            if (not isinstance(options, list) or len(options) > 4
                    or any(not isinstance(option, str) or not 1 <= len(option) <= 300 for option in options)):
                raise CoreDataError('routing_intent_invalid', '任务澄清选项无效。')
            seen.add(question['id'])
        self.value = {'summary': summary, 'questions': deepcopy(questions)}


class IntentAwaitRetry(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class IntentExecution:
    def __init__(self, service, identity, revision):
        self.service, self.identity, self.revision = service, identity, revision
        self.timer = None
        self.started = None
        self.deadline = None

    def state(self):
        return self.service._read(self.identity)

    def elapsed(self):
        return 0 if self.started is None else max(0, asyncio.get_running_loop().time() - self.started)

    def remaining_budget(self):
        state = self.state()
        return max(0, state['timeout_seconds'] * 3 - state['active_seconds'] - self.elapsed())

    def extend(self):
        if self.timer is None or self.started is None or self.timer.expired():
            raise CoreDataError('intent_request_ended', '当前识别请求已经结束，请重新尝试。')
        now = asyncio.get_running_loop().time()
        state = self.state()
        hard = min(self.started + 180, now + self.remaining_budget())
        deadline = min(hard, self.deadline + state['timeout_seconds'])
        if deadline <= self.deadline + 0.01:
            raise CoreDataError('intent_budget_exhausted', '当前识别等待预算已用完。')
        self.deadline = deadline
        self.service._patch(self.identity, self.revision,
            attempt_wait_seconds=deadline - self.started)
        self.timer.reschedule(deadline)

    async def classify(self, operation):
        mode = 'json_object'
        while True:
            state = self.state()
            remaining = state['timeout_seconds'] * 3 - state['active_seconds']
            if state['attempts'] >= 3 or remaining <= 0:
                raise IntentAwaitRetry('routing_intent_timeout', '本轮任务识别预算已用完。')
            self.service._patch(self.identity, self.revision, status='identifying',
                                attempts=state['attempts'] + 1, error=None,
                                attempt_started_at=time.time(), attempt_wait_seconds=min(state['timeout_seconds'], remaining))
            self.started = asyncio.get_running_loop().time()
            self.deadline = self.started + min(state['timeout_seconds'], remaining)
            failure = None
            try:
                async with asyncio.timeout_at(self.deadline) as timer:
                    self.timer = timer
                    return await operation(mode)
            except asyncio.CancelledError:
                raise
            except (TimeoutError, ValueError, TypeError, json.JSONDecodeError, CoreDataError) as error:
                failure = error
            except Exception as error:
                # ProviderError lives in a provider package that this state layer
                # does not import. Only documented transport/format codes retry.
                failure = error
            finally:
                elapsed = self.elapsed()
                self.started, self.deadline, self.timer = None, None, None
                current = self.state()
                if current['revision'] == self.revision:
                    self.service._patch(self.identity, self.revision,
                                        active_seconds=current['active_seconds'] + elapsed,
                                        attempt_started_at=None, attempt_wait_seconds=None)
            code = ('routing_intent_timeout' if isinstance(failure, TimeoutError)
                    else 'routing_intent_invalid' if isinstance(failure, (ValueError, TypeError))
                    else getattr(failure, 'code', 'routing_intent_unavailable'))
            message = ('本地模型未在等待时间内完成任务识别。' if code == 'routing_intent_timeout'
                       else '任务识别结果无效，可重新尝试。' if code in {'routing_intent_invalid', 'response_format_unsupported'}
                       else '任务识别调用失败，请检查模型运行状态。')
            state = self.state()
            self.service._patch(self.identity, self.revision, error={'code': code, 'message': message})
            if state['attempts'] >= 2 or code not in RETRY_CODES or self.remaining_budget() <= 0:
                raise IntentAwaitRetry(code, message) from failure
            if code in {'routing_intent_invalid', 'response_format_unsupported'}:
                mode = None


class IntentService:
    def __init__(self, repository, start, *, timeout_seconds=90):
        self.repository, self.start = repository, start
        self.timeout_seconds = timeout_seconds
        self.tasks = {}
        self.executions = {}
        self._lock = asyncio.Lock()
        self._closed = False
        with self.repository._connect() as connection:
            rows = connection.execute('SELECT id,state_json FROM intent_tasks').fetchall()
            for row in rows:
                state = json.loads(row['state_json'])
                if state['status'] in LIVE_STATES:
                    started = state.get('attempt_started_at')
                    allocated = state.get('attempt_wait_seconds')
                    if type(started) in (int, float) and type(allocated) in (int, float):
                        elapsed = min(max(0, time.time() - started), max(0, min(180, allocated)))
                        state['active_seconds'] = min(state['timeout_seconds'] * 3, state['active_seconds'] + elapsed)
                    state.update(status='interrupted', error={'code': 'core_interrupted',
                        'message': '应用退出中断了任务识别，可以恢复继续。'},
                        attempt_started_at=None, attempt_wait_seconds=None)
                    connection.execute('UPDATE intent_tasks SET state_json=?,updated_at=? WHERE id=?',
                                       (json.dumps(state, ensure_ascii=False), _utc_now(), row['id']))

    def _read(self, identity):
        with self.repository._connect() as connection:
            row = connection.execute('SELECT state_json FROM intent_tasks WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise CoreDataError('intent_not_found', '任务识别记录不存在。')
        return json.loads(row['state_json'])

    def _patch(self, identity, expected_revision, **changes):
        with self.repository._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT state_json FROM intent_tasks WHERE id=?', (identity,)).fetchone()
            if row is None:
                raise CoreDataError('intent_not_found', '任务识别记录不存在。')
            state = json.loads(row['state_json'])
            if state['revision'] != expected_revision:
                raise CoreDataError('intent_revision_changed', '任务已更新，请使用最新的问题继续。')
            state.update(changes, updated_at=_utc_now())
            connection.execute('UPDATE intent_tasks SET revision=?,state_json=?,updated_at=? WHERE id=?',
                               (state['revision'], json.dumps(state, ensure_ascii=False), state['updated_at'], identity))
        return state

    def get(self, identity):
        state = self._read(identity)
        execution = self.executions.get(identity)
        elapsed = execution.elapsed() if execution else 0
        used = state['active_seconds'] + elapsed
        state['elapsed_seconds'] = round(used, 1)
        state['budget_seconds'] = state['timeout_seconds'] * 3
        state['budget_remaining_seconds'] = max(0, round(state['budget_seconds'] - used, 1))
        state['remaining_seconds'] = (max(0, round(execution.deadline - asyncio.get_running_loop().time(), 1))
                                      if execution and execution.deadline else 0)
        state['can_extend'] = bool(execution and execution.deadline and execution.started is not None
                                  and execution.deadline < execution.started + 179.99
                                  and state['budget_remaining_seconds'] > state['remaining_seconds'] + 0.01)
        state['can_retry'] = (state['status'] in {'waiting_user', 'interrupted', 'failed'}
                              and not state.get('clarification') and state['attempts'] < 3
                              and state['budget_remaining_seconds'] > 0
                              and (state.get('error') or {}).get('code') in RECOVERY_CODES)
        return state

    def _launch(self, identity):
        previous = self.tasks.get(identity)
        if previous is not None and not previous.done():
            raise CoreDataError('intent_busy', '任务识别仍在运行。')
        task = asyncio.create_task(self._run(identity))
        self.tasks[identity] = task

    async def _run(self, identity):
        state = self._read(identity)
        revision = state['revision']
        execution = IntentExecution(self, identity, revision)
        self.executions[identity] = execution
        token = current_intent.set(execution)
        try:
            self._patch(identity, revision, status='queued', error=None)
            request = deepcopy(state['request'])
            if state.get('answers'):
                request['_intent_answers'] = deepcopy(state['answers'])
            result = await self.start(request)
            self._patch(identity, revision, status='completed', result=result, clarification=None, error=None)
        except IntentClarification as question:
            self._patch(identity, revision, status='waiting_user', clarification=question.value, error=None)
        except IntentAwaitRetry as error:
            self._patch(identity, revision, status='waiting_user', error={'code': error.code, 'message': str(error)})
        except asyncio.CancelledError:
            state = self._read(identity)
            if state['revision'] == revision and state['status'] in LIVE_STATES:
                self._patch(identity, revision, status='interrupted' if self._closed else 'cancelled',
                            error={'code': 'core_interrupted' if self._closed else 'cancelled_by_user',
                                   'message': '任务识别已中断。' if self._closed else '任务识别已取消。'})
        except Exception as error:
            self._patch(identity, revision, status='failed', error={
                'code': getattr(error, 'code', 'routing_intent_unavailable'),
                'message': str(error) if isinstance(error, CoreDataError) else '任务准备失败，请检查模型和附件状态。',
                'details': error.details if isinstance(error, CoreDataError) else None})
        finally:
            current_intent.reset(token)
            if self.executions.get(identity) is execution:
                self.executions.pop(identity, None)

    async def handle(self, action, params):
        if self._closed or not isinstance(params, dict):
            raise CoreDataError('invalid_params', '任务识别服务不可用或参数无效。')
        async with self._lock:
            if action == 'start':
                return await self._start(params)
            if action == 'list':
                scope = params.get('scope')
                if not isinstance(scope, str) or len(scope) > 300:
                    raise CoreDataError('invalid_params', '请选择会话范围。')
                with self.repository._connect() as connection:
                    rows = connection.execute('SELECT id FROM intent_tasks WHERE scope=? ORDER BY updated_at DESC LIMIT 20',
                                              (scope,)).fetchall()
                return {'tasks': [self.get(row['id']) for row in rows]}
            identity = params.get('task_id')
            if not isinstance(identity, str):
                raise CoreDataError('invalid_params', '缺少任务识别标识。')
            state = self.get(identity)
            if action == 'status':
                return {'task': state}
            if type(params.get('revision')) is not int or params['revision'] != state['revision']:
                raise CoreDataError('intent_revision_changed', '任务已更新，请使用最新的问题继续。')
            if action == 'extend':
                execution = self.executions.get(identity)
                if execution is None:
                    raise CoreDataError('intent_request_ended', '请求已经结束，请重新尝试。')
                execution.extend()
            elif action == 'cancel':
                if state['status'] == 'completed':
                    raise CoreDataError('intent_request_ended', '任务已经进入正式执行，请停止对应运行。')
                task = self.tasks.get(identity)
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                self._patch(identity, state['revision'], status='cancelled',
                            error={'code': 'cancelled_by_user', 'message': '任务识别已取消。'})
            elif action == 'retry':
                if not state['can_retry']:
                    raise CoreDataError('intent_budget_exhausted', '当前任务需要补充信息或已经用完尝试预算。')
                self._patch(identity, state['revision'], status='queued', error=None)
                self._launch(identity)
            elif action == 'respond':
                await self._respond(identity, state, params.get('answers'))
            else:
                raise CoreDataError('unsupported_action', '不支持的任务识别操作。')
            return {'task': self.get(identity)}

    async def _start(self, params):
        request = params.get('request')
        scope = params.get('scope', '')
        if (not isinstance(request, dict) or not isinstance(request.get('content'), str)
                or not request['content'].strip() or not isinstance(scope, str) or len(scope) > 300):
            raise CoreDataError('invalid_params', '请提供有效的任务请求。')
        request = deepcopy(request)
        key = request.get('idempotency_key') or str(uuid.uuid4())
        if not isinstance(key, str) or not 1 <= len(key) <= 512:
            raise CoreDataError('invalid_params', '请求身份无效。')
        request['idempotency_key'] = key
        try:
            fingerprint = hashlib.sha256(json.dumps({'request': request, 'scope': scope}, sort_keys=True,
                                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        except (TypeError, ValueError) as error:
            raise CoreDataError('invalid_params', '请求包含无效数据。') from error
        with self.repository._connect() as connection:
            previous = connection.execute('SELECT id,fingerprint FROM intent_tasks WHERE idempotency_key=?', (key,)).fetchone()
            if previous is not None:
                if previous['fingerprint'] != fingerprint:
                    raise CoreDataError('idempotency_conflict', 'The request key already identifies a different request.')
                return {'task': self.get(previous['id'])}
            timeout = self.timeout_seconds() if callable(self.timeout_seconds) else self.timeout_seconds
            if type(timeout) is not int or not 30 <= timeout <= 180:
                raise CoreDataError('invalid_settings', '任务识别等待时间须为 30 至 180 秒。')
            identity = str(uuid.uuid4())
            now = _utc_now()
            state = {'id': identity, 'scope': scope, 'revision': 1, 'status': 'queued', 'request': request,
                     'attempts': 0, 'active_seconds': 0, 'timeout_seconds': timeout, 'answers': {},
                     'clarification': None, 'error': None, 'result': None, 'created_at': now, 'updated_at': now}
            connection.execute('INSERT INTO intent_tasks VALUES (?,?,?,?,?,?,?,?)',
                (identity, key, fingerprint, scope, 1, json.dumps(state, ensure_ascii=False), now, now))
        self._launch(identity)
        return {'task': self.get(identity)}

    async def _respond(self, identity, state, answers):
        if state['status'] != 'waiting_user' or not state.get('clarification'):
            raise CoreDataError('intent_question_ended', '当前任务没有等待答复的问题。')
        questions = state['clarification']['questions']
        expected = {question['id'] for question in questions}
        if (not isinstance(answers, dict) or set(answers) != expected
                or any(not isinstance(value, str) or not value.strip() or len(value) > 4000 for value in answers.values())):
            raise CoreDataError('invalid_params', '请回答弹窗中的问题。')
        recorded = dict(state.get('answers', {}))
        for question in questions:
            recorded[question['question']] = answers[question['id']].strip()
        self._patch(identity, state['revision'], revision=state['revision'] + 1, status='queued',
                    attempts=0, active_seconds=0, answers=recorded, clarification=None, error=None, result=None)
        self._launch(identity)

    async def wait(self, identity):
        task = self.tasks.get(identity)
        if task:
            await task

    async def close(self):
        self._closed = True
        tasks = list(self.tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
