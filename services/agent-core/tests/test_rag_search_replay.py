import asyncio
import json
import time

import pytest

from agent.execution.execution_wait import ExecutionMonitor, current_activity
from agent.rag.service import RagService
from agent.storage import CoreDataError, CoreRepository
from test_rag_service import Engines, ready, wait_job


def test_withdrawal_invalidates_every_version_in_the_same_ocr_job(tmp_path):
    async def scenario():
        repository = CoreRepository(tmp_path / 'core.db')
        service = RagService(repository, Engines())
        try:
            library = (await service.handle('library.create', {'name': 'OCR siblings',
                'embedding_model_id': 'embed', 'reranker_model_id': 'rank'}))['library']['id']
            first_id = await ready(service, library)
            first = service.repository.preview(first_id)['version']
            second = await service.handle('document.paste', {'library_id': library, 'document_id': first['document_id'],
                'title': 'Sibling', 'content': '25.1309 sibling safety requirement'})
            second_id = second['version']['id']
            job = service.repository.new_job('import.batch', library)
            with repository._connect() as connection:
                connection.execute('UPDATE rag_versions SET ocr_summary=? WHERE id IN (?,?)',
                                   (json.dumps({'job_id': job['id']}), first_id, second_id))
            monitor = service.execution_monitor
            root = monitor.start_task(run_id='sibling-score')
            async def scoring(activity):
                assert activity.checkpoint('scores', {'sources': {'version_ids': [second_id]}}, {'0': .5})
            await monitor.execute_stage(root['id'], 'rerank', scoring)
            monitor.finish_task(root['id'])
            await service.handle('document.withdraw', {'version_id': first_id})
            with repository._connect() as connection:
                assert connection.execute("SELECT count(*) FROM rag_versions WHERE status='withdrawn'").fetchone()[0] == 2
                assert connection.execute('SELECT count(*) FROM execution_wait_checkpoints').fetchone()[0] == 0
        finally:
            await service.close()
    asyncio.run(scenario())


async def indexed_service(repository, engine):
    service = RagService(repository, engine)
    library = (await service.handle('library.create', {'name': 'Replay',
        'embedding_model_id': 'embed', 'reranker_model_id': 'rank'}))['library']['id']
    content = '\n\n'.join(f'25.{1000 + number} safety component requirement {number}.' for number in range(4))
    await ready(service, library, content=content)
    job = await wait_job(service, await service.handle('index.build', {'library_id': library}))
    assert job['status'] == 'completed'
    return service, library


def test_replay_metadata_survives_restart_is_private_and_preserves_recovery_count(tmp_path):
    repository = CoreRepository(tmp_path / 'core.db')
    monitor = ExecutionMonitor(repository)
    root = monitor.start_task(run_id='rag-search-test')
    request = {'action': 'library.search', 'params': {'library_id': 'library', 'query': 'private query'}}
    monitor.set_replay(root['id'], 'rag_search', request)
    with repository._connect() as connection:
        connection.execute('UPDATE execution_wait_tasks SET auto_recoveries=2 WHERE id=?', (root['id'],))
    reopened = ExecutionMonitor(CoreRepository(repository.database_path))
    reopened.set_replay(root['id'], 'rag_search', request)
    assert reopened.get_replay(root['id'])['request'] == request
    snapshot = reopened.get_task(root['id'])
    assert snapshot['replay_kind'] == 'rag_search'
    assert 'replay_json' not in snapshot and 'private query' not in str(snapshot)
    assert snapshot['auto_recoveries'] == 2
    with pytest.raises(CoreDataError) as failure:
        reopened.set_replay(root['id'], 'rag_search', {'action': 'search', 'params': {}})
    assert failure.value.code == 'execution_replay_conflict'


def test_replay_metadata_upgrades_existing_unreleased_schema32_roots(tmp_path):
    repository = CoreRepository(tmp_path / 'core.db')
    original = ExecutionMonitor(repository)
    task = original.start_task(run_id='legacy-schema32')
    with repository._connect() as connection:
        connection.execute('ALTER TABLE execution_wait_tasks DROP COLUMN replay_kind')
        connection.execute('ALTER TABLE execution_wait_tasks DROP COLUMN replay_json')
    reopened = ExecutionMonitor(CoreRepository(repository.database_path))
    reopened.set_replay(task['id'], 'rag_search', {'action': 'library.search', 'params': {}})
    assert reopened.get_task(task['id'])['run_id'] == 'legacy-schema32'


def test_search_status_keeps_previous_failure_private_during_retry(tmp_path):
    service = RagService(CoreRepository(tmp_path / 'core.db'), Engines())
    monitor = service.execution_monitor
    root = service._create_search_root({'action': 'library.search', 'params': {'library_id': 'library', 'query': 'safety'}})
    monitor.finish_task(root['id'], 'failed', error_code='rag_model_error',
                        replay_outcome={'error': {'code': 'rag_model_error', 'message': 'previous attempt'}})
    failed = monitor.get_task(root['id'])
    monitor.control(root['id'], 'retry', revision=failed['revision'], idempotency_key='retry-active')
    status = service.search_status(root['id'])
    assert status['task']['status'] == 'running'
    assert status['error'] is None and status['result'] is None


def test_completed_search_status_is_durable_and_drops_withdrawn_source_content(tmp_path):
    async def scenario():
        repository = CoreRepository(tmp_path / 'core.db')
        service, library = await indexed_service(repository, Engines())
        try:
            result = await service.search_library(library, 'safety')
            with repository._connect() as connection:
                task_id = connection.execute('SELECT id FROM execution_wait_tasks').fetchone()[0]
            await service.close()
            fresh = RagService(CoreRepository(repository.database_path), Engines())
            try:
                restored = fresh.search_status(task_id)
                assert restored['result'] == result
                assert 'safety component requirement' not in str(restored['task'])
                await fresh.handle('document.withdraw', {'version_id': result['evidence'][0]['version_id']})
                invalidated = fresh.search_status(task_id)
                assert invalidated['result'] is None
                assert invalidated['error']['code'] == 'rag_evidence_stale'
                replay = fresh.execution_monitor.get_replay(task_id)
                assert 'outcome' not in replay
            finally:
                await fresh.close()
        finally:
            await service.close()
    asyncio.run(scenario())


def test_search_start_returns_identity_before_inference_and_is_idempotent(tmp_path, record_property):
    class BlockedEngine(Engines):
        def __init__(self):
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def rerank(self, model_id, query, texts):
            self.entered.set()
            await self.release.wait()
            return [.5] * len(texts)

    async def scenario():
        engine = BlockedEngine()
        service, library = await indexed_service(CoreRepository(tmp_path / 'core.db'), engine)
        params = {'search_action': 'library.search', 'library_id': library,
                  'query': 'safety', 'idempotency_key': 'stable-admission'}
        try:
            started = time.perf_counter()
            admitted = await service.handle('search.start', params)
            record_property('admission_seconds', time.perf_counter() - started)
            assert not engine.entered.is_set()
            task_id = admitted['task']['id']
            assert admitted['task']['status'] == 'running'
            await asyncio.wait_for(engine.entered.wait(), 5)
            duplicate = await service.handle('search.start', params)
            assert duplicate['task']['id'] == task_id
            with pytest.raises(CoreDataError) as failure:
                await service.handle('search.start', {**params, 'query': 'different'})
            assert failure.value.code == 'execution_replay_conflict'
            waiting = await service.handle('search.status', {'execution_task_id': task_id})
            assert waiting['result'] is None
            engine.release.set()
            await service._search_tasks[task_id]
            complete = await service.handle('search.status', {'execution_task_id': task_id})
            assert complete['task']['status'] == 'completed'
            assert len(complete['result']['evidence']) == 4
        finally:
            engine.release.set()
            await service.close()
    asyncio.run(scenario())


def test_search_start_uses_current_global_wait_policy_for_new_roots(tmp_path):
    async def scenario():
        service = RagService(CoreRepository(tmp_path / 'core.db'), Engines())
        service.execution_policy = lambda: {'mode': 'custom', 'max_total_seconds': 60}
        try:
            admitted = await service.handle('search.start', {'search_action': 'library.search',
                'library_id': 'library', 'query': 'safety', 'idempotency_key': 'custom-policy'})
            assert admitted['task']['policy']['mode'] == 'custom'
            assert admitted['task']['policy']['max_total_seconds'] == 60
        finally:
            await service.close()
    asyncio.run(scenario())


def test_async_search_owns_its_activity_when_admitted_from_another_stage(tmp_path):
    class RecordingEngine(Engines):
        task_ids = []

        async def rerank(self, model_id, query, texts):
            self.task_ids.append(current_activity.get().task_id)
            return [.5] * len(texts)

    async def scenario():
        engine = RecordingEngine()
        service, library = await indexed_service(CoreRepository(tmp_path / 'core.db'), engine)
        monitor = service.execution_monitor
        parent = monitor.start_task(run_id='parent-stage')
        try:
            async def admit(activity):
                reply = await service.handle('search.start', {'search_action': 'library.search',
                    'library_id': library, 'query': 'safety', 'idempotency_key': 'nested-admission'})
                return reply['task']['id']
            child_id = await monitor.execute_stage(parent['id'], 'dispatch', admit)
            task = service._search_tasks.get(child_id)
            if task is not None:
                await task
            assert engine.task_ids == [child_id]
            assert monitor.get_task(child_id)['stage'] == 'rag_retrieval'
        finally:
            await service.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('generation_changed', [False, True])
def test_execution_request_restarts_standalone_search_under_original_root(tmp_path, generation_changed):
    from agent.runtime import ChatRunService

    class CheckpointEngine(Engines):
        def __init__(self, crash):
            self.crash = crash
            self.scored = []

        async def rerank(self, model_id, query, texts, **options):
            activity = current_activity.get()
            identity = {'query': query, 'texts': texts, 'sources': options['checkpoint_context'],
                        'context': options['cache_key']}
            scores = activity.load_checkpoint('scores', identity) or {}
            for offset in range(0, len(texts), 2):
                if str(offset) in scores:
                    continue
                batch = texts[offset:offset + 2]
                self.scored.extend(batch)
                scores.update({str(index): .5 for index in range(offset, offset + len(batch))})
                assert activity.checkpoint('scores', identity, scores)
                if self.crash:
                    raise CoreDataError('rag_model_error', 'simulated process restart')
            return [scores[str(index)] for index in range(len(texts))]

    async def emit(event):
        pass

    async def scenario():
        repository = CoreRepository(tmp_path / 'core.db')
        first_engine = CheckpointEngine(True)
        original, library = await indexed_service(repository, first_engine)
        try:
            with pytest.raises(CoreDataError, match='simulated process restart'):
                await original.search_library(library, 'safety')
            with repository._connect() as connection:
                task_id = connection.execute('SELECT id FROM execution_wait_tasks').fetchone()[0]
            if generation_changed:
                await ready(original, library, title='Additional source',
                            content='25.2000 Additional source outside the published index.')
            await original.close()
            runtime = ChatRunService(CoreRepository(repository.database_path), emit)
            resumed_engine = CheckpointEngine(False)
            runtime.rag.models = resumed_engine
            try:
                root = runtime.execution_monitor.get_task(task_id)
                reply = await runtime.handle_execution('retry', {'task_id': task_id,
                    'revision': root['revision'], 'idempotency_key': 'retry-after-restart'})
                assert reply['task']['id'] == task_id
                await runtime.rag._search_tasks[task_id]
                status = await runtime.rag.handle('search.status', {'execution_task_id': task_id})
                assert status['task']['status'] == 'completed'
                assert len(first_engine.scored) == 2
                assert len(resumed_engine.scored) == (4 if generation_changed else 2)
                if not generation_changed:
                    assert set(first_engine.scored).isdisjoint(resumed_engine.scored)
                assert len(status['result']['evidence']) == 4
                with repository._connect() as connection:
                    assert connection.execute('SELECT count(*) FROM execution_wait_tasks').fetchone()[0] == 1
            finally:
                await runtime.shutdown()
        finally:
            await original.close()
    asyncio.run(scenario())
