"""Persist user clarification during an active agent tool loop."""
import asyncio
import hashlib
import json
import uuid

from agent.execution.intent_session import IntentClarification
from agent.execution.task_plans import TaskPlans
from agent.execution.goal_requirements import GoalRequirements
from agent.storage import CoreDataError, _utc_now


class TaskInput:
    def __init__(self, repository, notify=None):
        self.repository = repository
        self.notify = notify
        self.waiters = {}

    @staticmethod
    def _decode(row):
        return {'id': row['id'], 'revision': row['revision'],
                'clarification': json.loads(row['payload_json'])}

    def pending(self, task_id):
        with self.repository._connect() as connection:
            row = connection.execute("SELECT * FROM task_questions WHERE task_id=? AND status='waiting' ORDER BY rowid LIMIT 1",
                                     (task_id,)).fetchone()
        return self._decode(row) if row else None

    def answers(self, task_id):
        return GoalRequirements(self.repository).answers(task_id)

    def answered_ids(self, task_id):
        with self.repository._connect() as connection:
            return [row[0] for row in connection.execute(
                "SELECT id FROM task_questions WHERE task_id=? AND status='answered'", (task_id,))]

    def _changed(self, task_id):
        if self.notify:
            self.notify(task_id)

    async def ask(self, task_id, summary, questions):
        payload = IntentClarification(summary, questions).value
        encoded = json.dumps(questions, ensure_ascii=False, sort_keys=True)
        fingerprint = hashlib.sha256(encoded.encode('utf-8')).hexdigest()
        with self.repository._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            TaskPlans._active(connection, task_id)
            connection.execute('''INSERT OR IGNORE INTO task_questions
                (id,task_id,fingerprint,status,payload_json,created_at) VALUES (?,?,?,'waiting',?,?)''',
                (str(uuid.uuid4()), task_id, fingerprint, json.dumps(payload, ensure_ascii=False), _utc_now()))
            row = connection.execute('SELECT * FROM task_questions WHERE task_id=? AND fingerprint=?',
                                     (task_id, fingerprint)).fetchone()
            if row['status'] == 'answered':
                return json.loads(row['answers_json'])
        return await self.wait(task_id, row['id'])

    async def wait(self, task_id, question_id):
        future = asyncio.get_running_loop().create_future()
        self.waiters[question_id] = future
        try:
            # Recheck after installing the waiter so an immediate answer is retained.
            with self.repository._connect() as connection:
                row = connection.execute('SELECT * FROM task_questions WHERE id=? AND task_id=?',
                                         (question_id, task_id)).fetchone()
            if row is None:
                raise CoreDataError('task_question_ended', '澄清问题已结束。')
            if row['status'] == 'answered':
                return json.loads(row['answers_json'])
            self._changed(task_id)
            return await future
        finally:
            if self.waiters.get(question_id) is future:
                self.waiters.pop(question_id)

    def respond(self, task_id, question_id, revision, answers):
        with self.repository._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT * FROM task_questions WHERE id=? AND task_id=?',
                                     (question_id, task_id)).fetchone()
            if row is None:
                raise CoreDataError('task_question_ended', '澄清问题不属于当前任务。')
            questions = json.loads(row['payload_json'])['questions']
            if (not isinstance(answers, dict) or set(answers) != {item['id'] for item in questions}
                    or any(not isinstance(value, str) or not value.strip() or len(value) > 4000 for value in answers.values())):
                raise CoreDataError('invalid_params', '请回答弹窗中的问题。')
            answers = {key: value.strip() for key, value in answers.items()}
            if row['status'] == 'answered':
                if json.loads(row['answers_json']) == answers:
                    return answers
                raise CoreDataError('task_question_ended', '该问题已经答复。')
            TaskPlans._active(connection, task_id)
            if type(revision) is not int or row['revision'] != revision:
                raise CoreDataError('task_question_changed', '澄清问题已更新，请使用当前问题。')
            connection.execute("UPDATE task_questions SET status='answered',revision=revision+1,answers_json=? WHERE id=?",
                               (json.dumps(answers, ensure_ascii=False), question_id))
        future = self.waiters.get(question_id)
        if future is not None and not future.done():
            future.set_result(answers)
        self._changed(task_id)
        return answers

    def inherit(self, task_id, run):
        """Copy only questions from the same user's linked retry chain."""
        parent_id, seen = run.get('parent_run_id'), {run['id']}
        while parent_id and parent_id not in seen:
            parent = self.repository.get_model_run(parent_id)
            if (parent['conversation_id'], parent['user_message_id']) != (run['conversation_id'], run['user_message_id']):
                break
            with self.repository._connect() as connection:
                rows = connection.execute('''SELECT q.* FROM task_questions q JOIN execution_wait_tasks t ON t.id=q.task_id
                    WHERE t.run_id=? ORDER BY q.rowid''', (parent_id,)).fetchall()
                for row in rows:
                    connection.execute('''INSERT OR IGNORE INTO task_questions VALUES (?,?,?,?,?,?,?,?)''',
                        (str(uuid.uuid4()), task_id, row['fingerprint'], row['revision'], row['status'],
                         row['payload_json'], row['answers_json'], _utc_now()))
            seen.add(parent_id)
            parent_id = parent.get('parent_run_id')
        return self.pending(task_id)
