import asyncio
from types import SimpleNamespace

import pytest

from agent.runtime import ChatRunService


def service_fixture():
    service = ChatRunService.__new__(ChatRunService)
    state = {'id': 'task', 'run_id': 'run', 'status': 'running', 'sequence': 0, 'revision': 0, 'attempt': 1}
    calls, events, transitions = [], [], []
    service._execution_events = set()
    service._execution_delivery = {}
    service._execution_paused_runs = {}
    service._event_loop = asyncio.get_running_loop()
    service.sequences = {}
    service.execution_monitor = SimpleNamespace(get_task=lambda _: dict(state))
    run = {'status': 'generating'}
    def transition(identity, expected, status):
        transitions.append(status)
        run['status'] = status
    service.repository = SimpleNamespace(get_model_run=lambda _: dict(run), transition_model_run=transition)
    service.source_version = 1
    def snapshot(value):
        calls.append(value['sequence'])
        return {**value, 'coverage': {'source_version': service.source_version}}
    service._execution_snapshot = snapshot
    async def sink(event):
        events.append(event)
        await asyncio.sleep(0)
    service.event_sink = sink
    def publish(kind='progress', status='running', sequence=None):
        state.update(sequence=state['sequence'] + 1 if sequence is None else sequence, status=status)
        service._execution_activity({**state, 'event': kind})
    return service, state, calls, events, transitions, publish


async def settle():
    for _ in range(8):
        await asyncio.sleep(0)


def test_thousand_tokens_do_not_build_thousand_proof_snapshots_or_wait_for_ui_timer():
    async def scenario():
        service, _, calls, events, _, publish = service_fixture()
        service.EXECUTION_UI_INTERVAL = 60  # Deterministic simulated long interval, never slept by token sends.
        for _ in range(1000):
            publish()
            await service._emit('run', 'run.chunk', {'delta': 'x'})
        assert len(calls) <= 2, len(calls)
        assert sum(event['event_type'] == 'run.chunk' for event in events) == 1000
        assert len(service._execution_events) <= 1
        await service._close_execution_events()
        assert not service._execution_events
    asyncio.run(scenario())


def test_critical_fresh_snapshot_terminal_and_model_events_never_go_backwards():
    async def scenario():
        service, state, calls, events, _, publish = service_fixture()
        service.EXECUTION_UI_INTERVAL = 60
        publish()
        await settle()
        publish('heartbeat')  # Pending routine must not delay urgent delivery.
        service.source_version = 2
        publish('sources_invalidated')
        await service._emit('run', 'run.state_changed', {'status': 'generating'})
        invalidated = [event for event in events if event['event_type'] == 'execution.activity'
                       and event['data']['event'] == 'sources_invalidated']
        assert invalidated and invalidated[-1]['data']['coverage']['source_version'] == 2
        publish('progress')
        publish('terminal', 'completed')
        await service._emit('run', 'run.completed', {})
        await settle()
        terminal_index = next(i for i, event in enumerate(events) if event['event_type'] == 'run.completed')
        assert not any(event['event_type'] == 'execution.activity' and event['data']['status'] == 'running'
                       for event in events[terminal_index + 1:])
        sequences = [event['sequence'] for event in events]
        assert sequences == sorted(set(sequences))
        assert len(calls) <= 4
        await service._close_execution_events()
    asyncio.run(scenario())


def test_pause_status_remains_synchronous_and_active_status_queries_are_fresh():
    async def scenario():
        service, state, calls, events, transitions, publish = service_fixture()
        publish('waiting_recovery', 'waiting_recovery')
        assert transitions == ['waiting_recovery']
        publish('control_continue', 'running')
        assert transitions == ['waiting_recovery', 'generating']
        service.source_version = 3
        value = service.execution_status({'task_id': state['id']})
        assert value['coverage']['source_version'] == 3
        await service._close_execution_events()
    asyncio.run(scenario())


def test_close_discards_pending_and_prevents_task_or_event_leaks():
    async def scenario():
        service, _, calls, events, _, publish = service_fixture()
        service.EXECUTION_UI_INTERVAL = 60
        publish()
        await settle()
        publish()
        await settle()
        await service._close_execution_events()
        count = len(events)
        publish('terminal', 'cancelled')
        await settle()
        assert len(events) == count
        assert not service._execution_events and not service._execution_delivery
    asyncio.run(scenario())



def test_business_activity_persistence_and_source_revocation_remain_unthrottled(tmp_path):
    from agent.execution.execution_wait import ExecutionMonitor
    from agent.storage import CoreRepository
    async def scenario():
        service, _, snapshots, events, _, _ = service_fixture()
        core = CoreRepository(tmp_path / 'core.db')
        service.repository = core
        monitor = ExecutionMonitor(core, on_event=service._execution_activity)
        service.execution_monitor = monitor
        root = monitor.start_task(intent_task_id='request:ui-performance')
        activity = monitor._begin(root['id'], 'model_output')
        for index in range(32):
            assert activity.progress(completed=index, sources={'library_ids': ['removed']})
        assert monitor.invalidate_sources(library_id='removed') == 1
        assert activity.progress(completed=999) is False
        assert activity.checkpoint('late', {}, []) is False
        with core._connect() as db:
            assert db.execute("SELECT COUNT(*) FROM execution_wait_activity WHERE kind='progress'").fetchone()[0] == 32
        await service._emit(root['id'], 'test.boundary', {})
        invalidated = [event for event in events if event['event_type'] == 'execution.activity'
                       and event['data']['event'] == 'sources_invalidated']
        assert invalidated and invalidated[-1]['data']['status'] == 'interrupted'
        assert invalidated[-1]['data']['error_code'] == 'rag_evidence_stale'
        assert len(snapshots) <= 4
        await service._close_execution_events()
    asyncio.run(scenario())



def test_activity_ui_sink_failure_cannot_fail_or_drop_model_events():
    async def scenario():
        service, _, _, events, _, publish = service_fixture()
        original = service.event_sink
        async def sink(event):
            if event['event_type'] == 'execution.activity':
                raise RuntimeError('UI delivery failed')
            await original(event)
        service.event_sink = sink
        publish('stage_started')
        await service._emit('run', 'run.chunk', {'delta': 'preserved'})
        assert events[-1]['data']['delta'] == 'preserved'
        await service._close_execution_events()
    asyncio.run(scenario())
