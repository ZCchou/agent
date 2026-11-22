import asyncio

import pytest

from agent.execution.execution_wait import ExecutionMonitor
from agent.storage import CoreDataError, CoreRepository


QUESTION = {'id': 'equipment', 'question': '请提供设备型号', 'options': ['型号 A', '型号 B']}


def test_runtime_question_waits_for_explicit_answer_and_reuses_answer(tmp_path):
    from agent.execution.task_input import TaskInput
    async def scenario():
        core = CoreRepository(tmp_path / 'core.db')
        root = ExecutionMonitor(core).start_task(intent_task_id='request:question')
        inputs = TaskInput(core)
        task = asyncio.create_task(inputs.ask(root['id'], '需要设备信息', [QUESTION]))
        await asyncio.sleep(0)
        question = inputs.pending(root['id'])
        assert question['clarification']['questions'] == [QUESTION]
        assert not task.done()
        with pytest.raises(CoreDataError):
            inputs.respond(root['id'], question['id'], question['revision'], {})
        inputs.respond(root['id'], question['id'], question['revision'], {'equipment': '用户输入的型号 C'})
        assert await task == {'equipment': '用户输入的型号 C'}
        assert inputs.pending(root['id']) is None
        assert await inputs.ask(root['id'], '需要设备信息', [QUESTION]) == {'equipment': '用户输入的型号 C'}
    asyncio.run(scenario())


def test_question_persists_across_restart_and_rejects_another_task_answer(tmp_path):
    from agent.execution.task_input import TaskInput
    async def scenario():
        core = CoreRepository(tmp_path / 'core.db')
        monitor = ExecutionMonitor(core)
        root = monitor.start_task(intent_task_id='request:one')
        other = monitor.start_task(intent_task_id='request:two')
        inputs = TaskInput(core)
        task = asyncio.create_task(inputs.ask(root['id'], '补充信息', [QUESTION]))
        await asyncio.sleep(0)
        question = inputs.pending(root['id'])
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        restored = TaskInput(core)
        assert restored.pending(root['id']) == question
        with pytest.raises(CoreDataError):
            restored.respond(other['id'], question['id'], 0, {'equipment': 'A'})
        restored.respond(root['id'], question['id'], 0, {'equipment': 'A'})
        assert restored.answers(root['id']) == [{'question': '请提供设备型号', 'answer': 'A'}]
    asyncio.run(scenario())
