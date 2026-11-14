import asyncio

import pytest

from agent.execution import execution_wait as wait
from agent.migrations.v32_execution_wait import migrate
from agent.storage import CoreDataError, CoreRepository


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds
        await asyncio.sleep(0)


@pytest.fixture
def foundation(tmp_path):
    repository = CoreRepository(tmp_path / 'core.db')
    with repository._connect() as connection:
        migrate(connection)
    clock = Clock()
    monitor = wait.ExecutionMonitor(repository, clock=clock, sleep=clock.sleep)
    return repository, monitor, clock


def test_policy_validates_ranges_and_preserves_unknown_total():
    policy = wait.ExecutionWaitPolicy.from_dict()
    assert policy.to_dict() == {
        'mode': 'adaptive', 'long_running_notice_seconds': 90,
        'no_progress_probe_seconds': 300, 'max_total_seconds': None,
        'max_auto_recoveries': 2,
    }
    for key, value in [('mode', 'fixed'), ('no_progress_probe_seconds', 59),
                       ('max_total_seconds', 59), ('max_auto_recoveries', 3),
                       ('long_running_notice_seconds', 29), ('max_auto_recoveries', True)]:
        with pytest.raises(CoreDataError):
            wait.ExecutionWaitPolicy.from_dict({key: value})


def test_root_rebind_controls_and_terminal_outcome(foundation):
    _, monitor, _ = foundation
    root = monitor.start_task(intent_task_id='intent')
    assert monitor.bind_run(root['id'], 'run')['id'] == root['id']
    with monitor.associate(root['id']):
        assert wait.current_task_id.get() == root['id']
    assert wait.current_task_id.get() is None
    response = monitor.control(root['id'], 'retry', revision=0, idempotency_key='once')
    assert response['revision'] == 1
    assert monitor.control(root['id'], 'retry', revision=0, idempotency_key='once') == response
    with pytest.raises(CoreDataError, match='revision'):
        monitor.control(root['id'], 'retry', revision=0, idempotency_key='different')
    monitor.finish_task(root['id'])
    assert monitor.finish_task(root['id'], 'cancelled')['status'] == 'completed'


def test_progress_survives_both_old_fixed_horizons_and_context(foundation):
    _, monitor, clock = foundation
    root = monitor.start_task(run_id='run')

    async def operation(activity):
        assert wait.current_activity.get() is activity
        for index in range(250):
            activity.progress(completed=index, total=250)
            activity.heartbeat()
            clock.now += 5
            await asyncio.sleep(0)
        return {'answer': 42}

    assert asyncio.run(monitor.execute_stage(root['id'], 'model', operation)) == {'answer': 42}
    assert clock.now > 1_001_200
    assert monitor.get_task(root['id'])['status'] == 'running'
    assert wait.current_activity.get() is None


def test_heartbeat_and_unknown_resources_do_not_claim_progress(foundation):
    repository, monitor, clock = foundation
    root = monitor.start_task(run_id='run')
    diagnosis_times = []

    async def operation(activity):
        while monitor.get_task(root['id'])['status'] != 'waiting_recovery':
            activity.heartbeat()
            activity.resource(active=None, cpu=None)
            await asyncio.sleep(0)
        return 'alive'

    def diagnose(activity):
        diagnosis_times.append(clock.now)
        return wait.Diagnostic(communication_lost=False, activity_observed=None)

    assert asyncio.run(monitor.execute_stage(root['id'], 'load', operation, diagnose=diagnose)) == 'alive'
    assert diagnosis_times[0] - root['created_at'] >= 300
    with repository._connect() as connection:
        row = connection.execute('SELECT * FROM execution_wait_stages').fetchone()
    assert row['progress_at'] == root['created_at']


def test_uncertain_pause_keeps_request_alive_and_cancel_unblocks(foundation):
    _, monitor, _ = foundation
    root = monitor.start_task(run_id='run')
    calls = 0
    drained = []

    async def operation(activity):
        nonlocal calls
        calls += 1
        try:
            await asyncio.Event().wait()
        finally:
            drained.append(calls)

    async def scenario():
        stage = asyncio.create_task(monitor.execute_stage(root['id'], 'load', operation))
        while monitor.get_task(root['id'])['status'] != 'waiting_recovery':
            await asyncio.sleep(0)
        assert drained == []
        current = monitor.get_task(root['id'])
        monitor.control(root['id'], 'continue', revision=current['revision'], idempotency_key='continue')
        await asyncio.sleep(0)
        assert calls == 1
        current = monitor.get_task(root['id'])
        monitor.control(root['id'], 'cancel', revision=current['revision'], idempotency_key='cancel')
        with pytest.raises(asyncio.CancelledError):
            await stage

    asyncio.run(scenario())
    assert drained == [1]
    assert monitor.get_task(root['id'])['status'] == 'cancelled'


def test_confirmed_loss_requires_two_diagnostics_and_drains_before_retry(foundation):
    _, monitor, clock = foundation
    root = monitor.start_task(run_id='run')
    calls = []
    diagnoses = []
    drained = []

    async def operation(activity):
        calls.append(activity.attempt)
        if len(calls) == 2:
            assert drained == [1]
            activity.progress(completed=1)
            return 'recovered'
        try:
            await asyncio.Event().wait()
        finally:
            drained.append(1)

    def diagnose(activity):
        diagnoses.append(clock.now)
        return wait.Diagnostic(communication_lost=True, activity_observed=False)

    assert asyncio.run(monitor.execute_stage(root['id'], 'worker', operation,
                                            safe_to_retry=True, diagnose=diagnose)) == 'recovered'
    assert len(diagnoses) == 2
    assert diagnoses[1] - diagnoses[0] >= 30
    assert calls == [1, 2]


def test_recoverable_failure_pauses_after_two_recoveries_and_manual_retry(foundation):
    _, monitor, _ = foundation
    root = monitor.start_task(run_id='run')
    calls = 0

    class WorkerExited(Exception):
        pass

    async def operation(activity):
        nonlocal calls
        calls += 1
        if calls < 4:
            raise WorkerExited('gone')
        return 'manual success'

    async def scenario():
        task = asyncio.create_task(monitor.execute_stage(root['id'], 'worker', operation,
                                  safe_to_retry=True, recoverable=lambda error: isinstance(error, WorkerExited)))
        while calls < 3 or monitor.get_task(root['id'])['status'] != 'waiting_recovery':
            await asyncio.sleep(0)
        assert calls == 3
        current = monitor.get_task(root['id'])
        monitor.control(root['id'], 'retry', revision=current['revision'], idempotency_key='manual')
        return await task

    assert asyncio.run(scenario()) == 'manual success'
    assert calls == 4


def test_continue_rejects_exhausted_failures_until_manual_retry(foundation):
    _, monitor, clock = foundation
    root = monitor.start_task(run_id='exhausted-continue')
    calls, drained = [], []

    class WorkerExited(Exception):
        pass

    async def operation(activity):
        calls.append(activity.attempt)
        try:
            if activity.attempt <= 3:
                raise WorkerExited('worker exited')
            assert drained == [1, 2, 3]
            return 'manual recovery'
        finally:
            drained.append(activity.attempt)

    async def scenario():
        task = asyncio.create_task(monitor.execute_stage(root['id'], 'worker', operation,
            safe_to_retry=True, recoverable=lambda error: isinstance(error, WorkerExited)))
        while calls != [1, 2, 3] or monitor.get_task(root['id'])['status'] != 'waiting_recovery':
            await asyncio.sleep(0)
        current = monitor.get_task(root['id'])
        with pytest.raises(CoreDataError) as rejected:
            monitor.control(root['id'], 'continue', revision=current['revision'], idempotency_key='continue')
        assert rejected.value.code == 'execution_continue_unavailable'
        assert current['can_continue'] is False
        assert current['capabilities']['continue'] is False
        until = clock.now + 100
        while clock.now < until:
            await asyncio.sleep(0)
        paused = monitor.get_task(root['id'])
        assert paused['status'] == 'waiting_recovery'
        assert paused['current_stage']['status'] == 'waiting_recovery'
        assert paused['revision'] == current['revision']
        assert calls == [1, 2, 3]
        assert task.done() is False
        monitor.control(root['id'], 'retry', revision=paused['revision'], idempotency_key='retry')
        return await task

    assert asyncio.run(scenario()) == 'manual recovery'
    assert calls == [1, 2, 3, 4]


def test_continue_requires_a_live_stage_and_terminal_capabilities_disable_it(foundation):
    _, monitor, _ = foundation
    root = monitor.start_task(run_id='before-stage')
    assert monitor.get_task(root['id'])['can_continue'] is False
    with pytest.raises(CoreDataError) as rejected:
        monitor.control(root['id'], 'continue', revision=root['revision'], idempotency_key='no-stage')
    assert rejected.value.code == 'execution_continue_unavailable'

    async def operation(activity):
        return 1

    asyncio.run(monitor.execute_stage(root['id'], 'completed-stage', operation))
    assert monitor.get_task(root['id'])['can_continue'] is False
    current = monitor.get_task(root['id'])
    with pytest.raises(CoreDataError):
        monitor.control(root['id'], 'continue', revision=current['revision'], idempotency_key='finished-stage')
    for status in ('failed', 'interrupted'):
        terminal = monitor.start_task(run_id=status)
        monitor.finish_task(terminal['id'], status)
        assert monitor.get_task(terminal['id'])['can_continue'] is False


@pytest.mark.parametrize('pause', ['uncertain', 'custom'])
def test_continue_preserves_live_uncertain_and_custom_waits(foundation, pause):
    _, monitor, clock = foundation
    policy = {'mode': 'custom', 'max_total_seconds': 60} if pause == 'custom' else None
    root = monitor.start_task(run_id='live-' + pause, policy=policy)
    calls, drained = [], []

    async def operation(activity):
        calls.append(activity.attempt)
        try:
            while True:
                if pause == 'custom':
                    activity.heartbeat()
                    activity.progress(completed=1)
                await asyncio.sleep(0)
        finally:
            drained.append(activity.attempt)

    async def scenario():
        task = asyncio.create_task(monitor.execute_stage(root['id'], 'worker', operation))
        while monitor.get_task(root['id'])['status'] != 'waiting_recovery':
            await asyncio.sleep(0)
        current = monitor.get_task(root['id'])
        assert current['can_continue'] is True
        response = monitor.control(root['id'], 'continue', revision=current['revision'], idempotency_key='continue')
        assert response['status'] == 'running'
        until = clock.now + 5
        while clock.now < until:
            await asyncio.sleep(0)
        assert monitor.get_task(root['id'])['status'] == 'running'
        assert calls == [1]
        assert drained == []
        current = monitor.get_task(root['id'])
        monitor.control(root['id'], 'cancel', revision=current['revision'], idempotency_key='cancel')
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert drained == [1]


def test_side_effects_and_unclassified_typed_errors_are_never_automatically_retried(foundation):
    _, monitor, _ = foundation
    calls = []

    class PermissionDenied(Exception):
        pass

    async def operation(activity):
        calls.append(activity.task_id)
        raise PermissionDenied('original code')

    for safe, classifier in [(True, None), (False, lambda error: True)]:
        root = monitor.start_task(run_id=str(safe))
        with pytest.raises(PermissionDenied, match='original code'):
            asyncio.run(monitor.execute_stage(root['id'], 'write', operation,
                        safe_to_retry=safe, recoverable=classifier))
    assert len(calls) == 2


def test_checkpoint_identity_and_attempt_fence(foundation):
    repository, monitor, _ = foundation
    root = monitor.start_task(run_id='run')
    activities = []

    async def operation(activity):
        activities.append(activity)
        assert activity.checkpoint('batch', {'model': 'a', 'order': [2, 1]}, {'scores': [0.1, 0.2]})
        assert activity.load_checkpoint('batch', {'order': [2, 1], 'model': 'a'}) == {'scores': [0.1, 0.2]}
        assert activity.load_checkpoint('batch', {'model': 'changed', 'order': [2, 1]}) is None
        return 1

    asyncio.run(monitor.execute_stage(root['id'], 'rerank', operation))
    assert activities[0].progress(completed=99) is False
    assert activities[0].checkpoint('batch', {}, {'bad': True}) is False
    with repository._connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM execution_wait_checkpoints').fetchone()[0] == 1


def test_restart_interrupts_and_cleanup_preserves_recent_checkpoints(foundation):
    repository, monitor, clock = foundation
    root = monitor.start_task(intent_task_id='intent')
    assert monitor.interrupt_active() == 1
    assert monitor.get_task(root['id'])['status'] == 'interrupted'
    assert monitor.cleanup() == 0
    clock.now += 30 * 86400 + 1
    assert monitor.cleanup() == 1
    with repository._connect() as connection:
        assert connection.execute('PRAGMA foreign_key_check').fetchall() == []


def test_migration_is_repeatable_and_preserves_business_rows(foundation):
    repository, monitor, _ = foundation
    repository.create_project('Preserved', 'research')
    with repository._connect() as connection:
        before = [tuple(row) for row in connection.execute('SELECT * FROM projects')]
        migrate(connection)
        migrate(connection)
        assert [tuple(row) for row in connection.execute('SELECT * FROM projects')] == before
        assert connection.execute('PRAGMA foreign_key_check').fetchall() == []


def test_wall_clock_jump_does_not_pause_active_execution(foundation):
    repository, _, _ = foundation
    wall, active = Clock(), Clock()
    monitor = wait.ExecutionMonitor(repository, clock=wall, active_clock=active, sleep=active.sleep)
    root = monitor.start_task(run_id='clock-jump')
    diagnoses = []

    async def operation(activity):
        for index in range(20):
            wall.now += 100_000
            activity.progress(completed=index)
            activity.heartbeat()
            await asyncio.sleep(0)
        return 'done'

    def diagnose(activity):
        diagnoses.append(activity.attempt)
        return wait.Diagnostic()

    assert asyncio.run(monitor.execute_stage(root['id'], 'load', operation, diagnose=diagnose)) == 'done'
    assert diagnoses == []


def test_manual_retry_fences_old_finally_progress_before_draining(foundation):
    _, monitor, _ = foundation
    root = monitor.start_task(run_id='retry-fence')
    calls, late = [], []

    async def operation(activity):
        calls.append(activity.attempt)
        if activity.attempt == 2:
            return 'done'
        try:
            await asyncio.Event().wait()
        finally:
            late.append(activity.progress(completed=999))

    async def scenario():
        task = asyncio.create_task(monitor.execute_stage(root['id'], 'worker', operation))
        while not calls:
            await asyncio.sleep(0)
        current = monitor.get_task(root['id'])
        monitor.control(root['id'], 'retry', revision=current['revision'], idempotency_key='retry')
        return await task

    assert asyncio.run(scenario()) == 'done'
    assert late == [False]


def test_diagnostic_error_preserves_original_operation_until_cancel(foundation):
    _, monitor, _ = foundation
    root = monitor.start_task(run_id='diagnostic-failure')
    drained = []

    async def operation(activity):
        try:
            await asyncio.Event().wait()
        finally:
            drained.append(True)

    def diagnose(activity):
        raise LookupError('diagnostic broken')

    async def scenario():
        task = asyncio.create_task(monitor.execute_stage(root['id'], 'worker', operation, diagnose=diagnose))
        while monitor.get_task(root['id'])['status'] != 'waiting_recovery':
            if task.done():
                await task
            await asyncio.sleep(0)
        assert drained == []
        current = monitor.get_task(root['id'])
        assert current['error_code'] == 'execution_diagnostic_unavailable'
        monitor.control(root['id'], 'cancel', revision=current['revision'], idempotency_key='cancel')
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert drained == [True]


def test_snapshot_exposes_persisted_stage_progress_and_event_sequence(foundation):
    _, monitor, _ = foundation
    events = []
    monitor.on_event = events.append
    root = monitor.start_task(run_id='snapshot')

    async def operation(activity):
        activity.progress(completed=2, total=5, model='test-model', device='cpu')
        activity.queue(waiting=True, reason='resource')
        snapshot = monitor.get_task(root['id'])
        assert snapshot['current_stage']['stage'] == 'load'
        assert snapshot['progress']['completed'] == 2
        assert snapshot['progress']['total'] == 5
        assert snapshot['stages'][0]['queue_waiting'] == 1
        return 1

    asyncio.run(monitor.execute_stage(root['id'], 'load', operation))
    assert events[-1]['sequence'] > events[0]['sequence']
    assert monitor.get_task(root['id'])['capabilities']['cancel'] is True


def test_user_can_resume_interrupted_root_and_reuse_valid_checkpoint(foundation):
    repository, monitor, _ = foundation
    root = monitor.start_task(intent_task_id='resume')

    async def save(activity):
        activity.checkpoint('batch', {'model': 'stable'}, [0.5])
        return None

    asyncio.run(monitor.execute_stage(root['id'], 'rerank', save))
    monitor.interrupt_active()
    current = monitor.get_task(root['id'])
    assert current['status'] == 'interrupted'
    restored = monitor.control(root['id'], 'retry', revision=current['revision'], idempotency_key='resume')
    assert restored['status'] == 'running'

    async def resume(activity):
        assert activity.load_checkpoint('batch', {'model': 'stable'}) == [0.5]
        return 9

    assert asyncio.run(monitor.execute_stage(root['id'], 'rerank', resume)) == 9
    assert monitor.get_task(root['id'])['attempt'] == 2


def test_queue_wait_accumulates_independently_from_progress(foundation):
    _, monitor, clock = foundation
    root = monitor.start_task(run_id='resource-queue')

    async def operation(activity):
        activity.queue(waiting=True, reason='gpu_memory')
        clock.now += 17
        activity.heartbeat()
        assert monitor.get_task(root['id'])['progress']['queue_wait_seconds'] == 17
        activity.queue(waiting=False)
        clock.now += 20
        assert monitor.get_task(root['id'])['progress']['queue_wait_seconds'] == 17
        return 1

    asyncio.run(monitor.execute_stage(root['id'], 'load', operation))


def test_custom_horizon_pauses_without_destroying_live_request(foundation):
    _, monitor, _ = foundation
    root = monitor.start_task(run_id='custom', policy={'mode': 'custom', 'max_total_seconds': 60})
    drained = []

    async def operation(activity):
        try:
            while True:
                activity.heartbeat()
                activity.progress(completed=1)
                await asyncio.sleep(0)
        finally:
            drained.append(True)

    async def scenario():
        task = asyncio.create_task(monitor.execute_stage(root['id'], 'load', operation))
        while monitor.get_task(root['id'])['status'] != 'waiting_recovery':
            await asyncio.sleep(0)
        assert drained == []
        current = monitor.get_task(root['id'])
        monitor.control(root['id'], 'cancel', revision=current['revision'], idempotency_key='cancel')
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert drained == [True]


def test_cancel_wins_against_late_success_and_completed_controls(foundation):
    _, monitor, _ = foundation
    root = monitor.start_task(run_id='cancel-wins')

    async def operation(activity):
        current = monitor.get_task(root['id'])
        monitor.control(root['id'], 'cancel', revision=current['revision'], idempotency_key='cancel')
        return 'late success'

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(monitor.execute_stage(root['id'], 'load', operation))
    assert monitor.finish_task(root['id'])['status'] == 'cancelled'
    current = monitor.get_task(root['id'])
    assert monitor.control(root['id'], 'continue', revision=current['revision'], idempotency_key='continue')['status'] == 'cancelled'


def test_stage_success_and_controls_publish_durable_business_transitions(foundation):
    _, monitor, _ = foundation
    events = []
    monitor.on_event = events.append
    root = monitor.start_task(run_id='events')

    async def operation(activity):
        return 'done'

    assert asyncio.run(monitor.execute_stage(root['id'], 'load', operation)) == 'done'
    assert [event['event'] for event in events] == ['stage_started', 'stage_completed']
    assert events[-1]['status'] == 'running'
    assert events[-1]['current_stage']['status'] == 'completed'
    current = monitor.get_task(root['id'])
    monitor.control(root['id'], 'cancel', revision=current['revision'], idempotency_key='cancel')
    assert events[-1]['event'] == 'control_cancel'
    assert events[-1]['status'] == 'cancelled'
    other = monitor.start_task(run_id='terminal-events')
    monitor.finish_task(other['id'])
    assert events[-1]['event'] == 'terminal'
    assert events[-1]['status'] == 'completed'


def test_new_progress_breaks_consecutive_lost_diagnostics(foundation):
    _, monitor, clock = foundation
    root = monitor.start_task(run_id='progress-between-probes')
    diagnoses = []
    progressed = False

    async def operation(activity):
        nonlocal progressed
        if activity.attempt == 2:
            assert len(diagnoses) >= 3
            return 'recovered'
        while True:
            if len(diagnoses) == 1 and not progressed:
                activity.progress(completed=1)
                progressed = True
            await asyncio.sleep(0)

    def diagnose(activity):
        diagnoses.append(clock.now)
        return wait.Diagnostic(communication_lost=True, activity_observed=False)

    assert asyncio.run(monitor.execute_stage(root['id'], 'load', operation,
                     safe_to_retry=True, diagnose=diagnose)) == 'recovered'


def test_custom_total_applies_across_multiple_stages(foundation):
    _, monitor, clock = foundation
    root = monitor.start_task(run_id='total', policy={'mode': 'custom', 'max_total_seconds': 60})

    async def first(activity):
        clock.now += 40
        return 'first'

    async def second(activity):
        while True:
            activity.progress(completed=1)
            activity.heartbeat()
            activity.resource(active=True)
            await asyncio.sleep(0)

    async def scenario():
        await monitor.execute_stage(root['id'], 'one', first)
        task = asyncio.create_task(monitor.execute_stage(root['id'], 'two', second,
               diagnose=lambda activity: wait.Diagnostic(activity_observed=True)))
        while monitor.get_task(root['id'])['status'] != 'waiting_recovery':
            await asyncio.sleep(0)
            if clock.now - root['created_at'] > 120:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                pytest.fail('custom total did not pause after the configured task horizon')
        current = monitor.get_task(root['id'])
        assert current['elapsed_seconds'] <= 70
        monitor.control(root['id'], 'cancel', revision=current['revision'], idempotency_key='cancel')
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['library', 'version', 'index'])
def test_source_invalidation_fences_activity_and_deletes_only_matching_checkpoints(foundation, kind):
    repository, monitor, _ = foundation
    root = monitor.start_task(run_id='source-deletion-' + kind)

    async def operation(activity):
        source = {kind + '_ids': ['removed']}
        assert activity.progress(stage='retrieval_scope', sources=source)
        assert activity.checkpoint('matching', {'sources': source}, [1])
        assert activity.checkpoint('unrelated', {'sources': {kind + '_ids': ['kept']}}, [2])
        assert monitor.invalidate_sources(**{kind + '_id': 'removed'}) == 1
        assert activity.progress(completed=999) is False
        assert activity.checkpoint('late', {'sources': source}, [3]) is False
        assert monitor.get_task(root['id'])['status'] == 'interrupted'
        with repository._connect() as connection:
            keys = [row[0] for row in connection.execute('SELECT checkpoint_key FROM execution_wait_checkpoints')]
            assert keys == ['unrelated']
            assert connection.execute('SELECT COUNT(*) FROM execution_wait_activity').fetchone()[0] == 1
            assert connection.execute('SELECT COUNT(*) FROM execution_wait_tasks').fetchone()[0] == 1
        return 'stale result'

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(monitor.execute_stage(root['id'], 'rerank', operation))
    assert monitor.get_task(root['id'])['error_code'] == 'rag_evidence_stale'


def test_source_invalidation_fences_scope_before_first_checkpoint_and_preserves_terminal_audit(foundation):
    repository, monitor, _ = foundation
    root = monitor.start_task(run_id='scope-only')

    async def scope(activity):
        activity.progress(sources={'library_ids': ['removed']})
        assert monitor.invalidate_sources(library_id='removed') == 1
        assert activity.checkpoint('late', {}, []) is False

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(monitor.execute_stage(root['id'], 'retrieve', scope))
    other = monitor.start_task(run_id='finished-audit')

    async def finished(activity):
        activity.checkpoint('finished', {'sources': {'library_ids': ['removed']}}, [1])

    asyncio.run(monitor.execute_stage(other['id'], 'retrieve', finished))
    monitor.finish_task(other['id'])
    monitor.invalidate_sources(library_id='removed')
    assert monitor.get_task(other['id'])['status'] == 'completed'
    with repository._connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM execution_wait_tasks').fetchone()[0] == 2
        assert connection.execute('SELECT COUNT(*) FROM execution_wait_stages').fetchone()[0] == 2
