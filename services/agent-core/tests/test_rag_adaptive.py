import asyncio
import json
import sys

import pytest

from agent.rag.worker_client import WorkerClient


def test_v2_stdout_sequence_remains_monotonic_across_requests(monkeypatch, capsys):
    import io
    import agent.rag.worker as worker
    requests = [{'id': number, 'protocol_version': 2,
        'execution_identity': {'task_id': 'root', 'revision': 1, 'attempt': 1}, 'params': {}}
        for number in (1, 2)]
    monkeypatch.setattr(worker.sys, 'stdin', io.StringIO(''.join(json.dumps(request) + '\n' for request in requests)))
    def dispatch(request, cache):
        request['_progress']({'stage': 'reranking'})
        return [.5]
    monkeypatch.setattr(worker, 'dispatch', dispatch)
    worker.main()
    frames = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [frame['sequence'] for frame in frames] == [1, 2, 3, 4]
    assert [frame['id'] for frame in frames] == [1, 1, 2, 2]


@pytest.mark.parametrize('action', ['library.remove', 'document.withdraw'])
def test_source_removal_clears_durable_scores_and_preserves_root_audit(tmp_path, action):
    from agent.execution.execution_wait import current_activity
    from agent.rag.service import RagService
    from agent.storage import CoreRepository
    from test_rag_service import Engines, ready, wait_job
    class CheckpointEngine(Engines):
        async def rerank(self, model_id, query, texts, **options):
            sources = options.get('checkpoint_context')
            assert sources and sources['library_ids'] and sources['version_ids']
            assert current_activity.get().checkpoint('batch', {'sources': sources}, {'0': .5})
            return [.5] * len(texts)
    core = CoreRepository(tmp_path / 'core.db')
    service = RagService(core, CheckpointEngine())
    async def scenario():
        try:
            library = (await service.handle('library.create', {'name': 'Sources',
                'embedding_model_id': 'embed', 'reranker_model_id': 'rank'}))['library']['id']
            version = await ready(service, library)
            assert (await wait_job(service, await service.handle('index.build', {'library_id': library})))['status'] == 'completed'
            assert (await service.search_library(library, '失效'))['evidence']
            with core._connect() as connection:
                assert connection.execute('SELECT count(*) FROM execution_wait_checkpoints').fetchone()[0] == 1
            await service.handle(action, {'library_id': library, 'version_id': version})
            with core._connect() as connection:
                assert connection.execute('SELECT count(*) FROM execution_wait_checkpoints').fetchone()[0] == 0
                assert connection.execute('SELECT count(*) FROM execution_wait_tasks').fetchone()[0] == 1
        finally:
            await service.close()
    asyncio.run(scenario())


def test_continue_preserves_captured_v2_request_identity(tmp_path):
    from agent.execution.execution_wait import ExecutionMonitor
    from agent.storage import CoreRepository
    monitor = ExecutionMonitor(CoreRepository(tmp_path / 'core.db'))
    root = monitor.start_task(run_id='continue-live-worker')
    script = tmp_path / 'continue.py'
    script.write_text('''import time,os
import agent.rag.worker as worker
worker.HEARTBEAT_INTERVAL = .02
def dispatch(request, cache):
 time.sleep(.4)
 return {'pid':os.getpid(), 'revision':request['execution_identity']['revision']}
worker.dispatch=dispatch
worker.main()
''')
    async def scenario():
        client = WorkerClient([sys.executable, str(script)])
        revisions = []
        process_ids = []
        controlled = []
        async def operation(activity):
            captured = activity.revision
            def progress(event):
                revisions.append(event['revision'])
                process_ids.append(client.process.pid)
                if not controlled:
                    controlled.append(monitor.control(root['id'], 'continue', revision=captured,
                                                       idempotency_key='continue-once'))
            return await client.request('rerank', {'protocol_version': 2,
                'execution_identity': {'task_id':activity.task_id, 'revision':captured,
                                       'attempt':activity.attempt}}, progress=progress)
        try:
            result = await monitor.execute_stage(root['id'], 'rerank', operation)
            assert set(process_ids) == {client.process.pid}
            assert len(revisions) >= 2 and set(revisions) == {result['revision']}
            assert controlled[0]['revision'] == result['revision'] + 1
        finally:
            await client.close()
    asyncio.run(scenario())


def test_worker_progress_follows_loading_tokenization_inference_validation(monkeypatch):
    import agent.rag.worker as worker
    class Tokenizer:
        def __call__(self, texts, **options):
            return {'input_ids': [[1] for text in texts]}
    monkeypatch.setattr(worker, '_engine', lambda *args: {})
    monkeypatch.setattr(worker, '_load', lambda *args: (Tokenizer(), object(), {}))
    monkeypatch.setattr(worker, '_onnx_batch', lambda *args: [[1.0]])
    events = []
    result = worker.dispatch({'action': 'embed', '_progress': events.append,
        'params': {'profile': 'bge-m3', 'backend': 'onnx', 'device': 'cpu',
                   'path': 'model', 'texts': ['document']}}, {})
    assert result == [[1.0]]
    assert [event['stage'] for event in events] == [
        'model_loading', 'tokenization', 'embedding', 'result_validation']


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows process counters')
@pytest.mark.parametrize('device,busy,expected', [('cpu', False, False), ('cuda', False, None), ('cpu', True, True)])
def test_worker_diagnostics_measure_child_activity_and_preserve_unknown_cuda(tmp_path, monkeypatch, device, busy, expected):
    import agent.rag.worker_client as module
    from agent.execution.execution_wait import ExecutionMonitor
    from agent.storage import CoreRepository
    core = CoreRepository(tmp_path / 'core.db')
    monitor = ExecutionMonitor(core)
    root = monitor.start_task(run_id='idle-counters')
    monkeypatch.setattr(module, 'RESOURCE_INTERVAL', .05, raising=False)
    script = tmp_path / 'idle.py'
    work = "end=time.monotonic()+.6\n while time.monotonic()<end: pass" if busy else "time.sleep(.6)"
    script.write_text("import sys,json,time\nfor line in sys.stdin:\n r=json.loads(line);print(json.dumps({'id':r['id'],'progress':{'ready':True}}),flush=True)\n " + work + "\n print(json.dumps({'id':r['id'],'result':True}),flush=True)\n")
    async def scenario():
        client = WorkerClient([sys.executable, str(script)])
        async def operation(activity):
            ready = asyncio.Event()
            pending = asyncio.create_task(client.request('probe', {'device': device}, progress=lambda event: ready.set()))
            try:
                await asyncio.wait_for(ready.wait(), 5)
                await asyncio.sleep(.15)
                observations = []
                for index in range(3):
                    observations.append(client.diagnose().activity_observed)
                    await asyncio.sleep(.06)
                assert any(observed is expected for observed in observations)
                assert await pending is True
            finally:
                if not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
        try:
            await monitor.execute_stage(root['id'], 'idle', operation)
        finally:
            await client.close()
    asyncio.run(scenario())


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows Job ownership')
def test_replacing_exited_worker_closes_old_job_before_starting(tmp_path):
    script = tmp_path / 'worker.py'
    script.write_text("import sys,json\nfor line in sys.stdin:\n r=json.loads(line);print(json.dumps({'id':r['id'],'result':True}),flush=True)\n")
    async def scenario():
        client = WorkerClient([sys.executable, str(script)])
        try:
            assert await client.request('probe', {}) is True
            job = client._job
            old = client.process
            old.kill()
            await old.wait()
            assert await client.request('probe', {}) is True
            assert job._closed
            assert client.process.pid != old.pid
        finally:
            await client.close()
    asyncio.run(scenario())


def test_monitored_legacy_worker_uses_adaptive_deadline(tmp_path):
    from agent.execution.execution_wait import ExecutionMonitor
    from agent.migrations.v32_execution_wait import migrate
    from agent.storage import CoreRepository
    core = CoreRepository(tmp_path / 'core.db')
    with core._connect() as connection:
        migrate(connection)
    monitor = ExecutionMonitor(core)
    task = monitor.start_task(run_id='legacy-adaptive')
    script = tmp_path / 'legacy.py'
    script.write_text("import sys,json,time\nfor line in sys.stdin:\n r=json.loads(line);time.sleep(.05);print(json.dumps({'id':r['id'],'result':True}),flush=True)\n")
    async def scenario():
        client = WorkerClient([sys.executable, str(script)], timeout=0.001)
        try:
            async def operation(activity):
                return await client.request('probe', {})
            assert await monitor.execute_stage(task['id'], 'ocr', operation) is True
        finally:
            await client.close()
    asyncio.run(scenario())


def test_model_store_automatically_restarts_after_second_batch_process_exit(tmp_path, monkeypatch):
    from agent.execution.execution_wait import ExecutionMonitor
    from agent.migrations.v32_execution_wait import migrate
    from agent.storage import CoreRepository
    from test_rag_performance import store_with_models
    store, records = store_with_models(tmp_path, monkeypatch, count=1, reranker=True)
    store._state['models'][0]['backend'] = 'onnx'
    core = CoreRepository(tmp_path / 'core.db')
    with core._connect() as connection:
        migrate(connection)
    clock = [0.0]
    async def tick(seconds):
        await asyncio.sleep(.005)
    monitor = ExecutionMonitor(core, clock=lambda: clock[0], active_clock=lambda: clock[0], sleep=tick)
    store.execution_monitor = monitor
    script = tmp_path / 'crash.py'
    log = tmp_path / 'batches.jsonl'
    crashed = tmp_path / 'crashed'
    script.write_text(f'''import sys,json,os
from pathlib import Path
for line in sys.stdin:
 r=json.loads(line);texts=r['params']['texts']
 if r['action']=='rerank_lengths':
  result=[len(t) for t in texts]
 else:
  Path({str(log)!r}).open('a').write(json.dumps(texts)+'\\n')
  if texts==['3','4'] and not Path({str(crashed)!r}).exists():
   Path({str(crashed)!r}).touch();os._exit(9)
  result=[float(t)/10 for t in texts]
 print(json.dumps({{'id':r['id'],'result':result}}),flush=True)
''')
    monkeypatch.setattr(store, '_command', lambda: [sys.executable, str(script)])
    async def scenario():
        async def progress(event):
            if event.get('completed'):
                clock[0] += 350
        try:
            assert await store.rerank(records[0]['id'], 'query', ['1','2','3','4','5'], progress=progress) == [.1,.2,.3,.4,.5]
            assert clock[0] > 300
            assert [json.loads(line) for line in log.read_text().splitlines()] == [['1','2'], ['3','4'], ['3','4'], ['5']]
            with core._connect() as connection:
                assert connection.execute('SELECT count(*) FROM execution_wait_stages').fetchone()[0] == 2
                assert connection.execute('SELECT status FROM execution_wait_tasks').fetchone()[0] == 'completed'
        finally:
            await store.close()
    asyncio.run(scenario())


def test_managed_runtime_requires_v2_manifest(tmp_path, monkeypatch):
    import hashlib
    import agent.rag.models as models
    base = tmp_path / 'app'
    runtime = base / 'rag-runtime'
    runtime.mkdir(parents=True)
    (runtime / 'rag-worker.exe').write_bytes(b'legacy')
    (runtime / 'runtime.json').write_text(json.dumps({'schema_version': 1, 'protocol_version': 1,
        'executable': 'rag-worker.exe', 'files': {'rag-worker.exe': hashlib.sha256(b'legacy').hexdigest()}}))
    monkeypatch.setattr(models.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(models.sys, 'executable', str(base / 'agent-core.exe'))
    store = models.ModelStore(tmp_path / 'data')
    assert store.runtime_status()['available'] is False


def test_public_library_search_default_is_adaptive_and_persists_root(tmp_path, monkeypatch):
    from agent.rag.service import RagService
    from agent.storage import CoreRepository
    from agent.migrations.v32_execution_wait import migrate
    from test_rag_service import Engines, ready, wait_job
    core = CoreRepository(tmp_path / 'core.db')
    with core._connect() as connection:
        migrate(connection)
    service = RagService(core, Engines())
    original_timeout = asyncio.timeout
    seen = []
    def timeout(seconds):
        seen.append(seconds)
        return original_timeout(seconds)

    async def scenario():
        try:
            library = (await service.handle('library.create', {'name': 'Adaptive',
                'embedding_model_id': 'embed', 'reranker_model_id': 'rank'}))['library']['id']
            await ready(service, library)
            assert (await wait_job(service, await service.handle('index.build', {'library_id': library})))['status'] == 'completed'
            monkeypatch.setattr(asyncio, 'timeout', timeout)
            assert (await service.handle('library.search', {'library_id': library, 'query': '失效'}))['evidence']
            assert all(value is None for value in seen)
            with core._connect() as connection:
                rows = connection.execute('SELECT status FROM execution_wait_tasks').fetchall()
                assert [row['status'] for row in rows] == ['completed']
        finally:
            await service.close()
    asyncio.run(scenario())


def test_validated_batch_checkpoint_survives_model_store_restart(tmp_path, monkeypatch):
    from agent.execution.execution_wait import ExecutionMonitor
    from agent.migrations.v32_execution_wait import migrate
    from agent.storage import CoreRepository
    from agent.rag.models import ModelStore
    from test_rag_performance import store_with_models
    store, records = store_with_models(tmp_path, monkeypatch, count=1, reranker=True)
    store._state['models'][0]['backend'] = 'onnx'
    store._save()
    core = CoreRepository(tmp_path / 'core.db')
    with core._connect() as connection:
        migrate(connection)
    monitor = ExecutionMonitor(core)
    root = monitor.start_task(run_id='checkpoint-restart')
    script = tmp_path / 'checkpoint.py'
    log = tmp_path / 'scored.jsonl'
    script.write_text(f'''import sys,json
from pathlib import Path
for line in sys.stdin:
 r=json.loads(line);texts=r['params']['texts']
 if r['action']=='rerank_lengths':
  result=[len(t) for t in texts]
 else:
  Path({str(log)!r}).open('a').write(json.dumps(texts)+'\\n')
  result=[float(t)/10 for t in texts]
 print(json.dumps({{'id':r['id'],'result':result}}),flush=True)
''')
    monkeypatch.setattr(store, '_command', lambda: [sys.executable, str(script)])

    async def scenario():
        async def cancelled(activity):
            async def stop_after_batch(event):
                if event.get('completed') == 2:
                    raise RuntimeError('simulated restart')
            await store.rerank(records[0]['id'], 'query', ['1', '2', '3'],
                               cache_key=('project', 1), progress=stop_after_batch)
        try:
            with pytest.raises(RuntimeError, match='simulated restart'):
                await monitor.execute_stage(root['id'], 'rerank', cancelled)
            await store.close()
            fresh = ModelStore(store.data_dir)
            monkeypatch.setattr(fresh, '_command', lambda: [sys.executable, str(script)])
            async def resume(activity):
                return await fresh.rerank(records[0]['id'], 'query', ['1', '2', '3'],
                                          cache_key=('project', 1))
            try:
                result = await monitor.execute_stage(root['id'], 'rerank', resume)
                assert result == [0.1, 0.2, 0.3]
                assert [json.loads(line) for line in log.read_text().splitlines()] == [['1', '2'], ['3']]
            finally:
                await fresh.close()
        finally:
            await store.close()
    asyncio.run(scenario())


def test_rerank_default_has_no_absolute_deadline(tmp_path, monkeypatch):
    from test_rag_performance import store_with_models
    store, records = store_with_models(tmp_path, monkeypatch, count=1, reranker=True)
    original_timeout = asyncio.timeout
    observed = []
    def capture_timeout(seconds):
        observed.append(seconds)
        return original_timeout(seconds)
    monkeypatch.setattr(asyncio, 'timeout', capture_timeout)

    async def scenario():
        try:
            assert await store.rerank(records[0]['id'], 'query', ['1']) == [0.01]
            assert all(value is None for value in observed)
        finally:
            await store.close()
    asyncio.run(scenario())


def test_v2_worker_heartbeats_are_independent_and_identity_fenced(tmp_path):
    script = tmp_path / 'heartbeat.py'
    script.write_text('''import time
import agent.rag.worker as worker
worker.HEARTBEAT_INTERVAL = 0.02
def slow(request, cache):
    time.sleep(0.4)
    return [0.75]
worker.dispatch = slow
worker.main()
''')

    async def scenario():
        client = WorkerClient([sys.executable, str(script)])
        events = []
        try:
            result = await client.request('rerank', {
                'protocol_version': 2, 'execution_identity': {
                    'task_id': 'root', 'revision': 3, 'attempt': 2},
            }, progress=events.append)
            assert result == [0.75]
            assert sum(event.get('kind') == 'heartbeat' for event in events) >= 2
            assert all(event['cpu_seconds'] >= 0 and event['resource_active'] in (None, True)
                       for event in events)
            assert any(event['resource_active'] is None for event in events)
            assert all(event['task_id'] == 'root' and event['revision'] == 3
                       and event['attempt'] == 2 for event in events)
            assert [event['sequence'] for event in events] == list(range(1, len(events) + 1))
        finally:
            await client.close()

    asyncio.run(scenario())


def test_worker_default_allows_adaptive_wait_and_explicit_timeout_still_stops(tmp_path):
    script = tmp_path / 'slow.py'
    script.write_text("import sys,json,time\nfor line in sys.stdin:\n r=json.loads(line);time.sleep(.05);print(json.dumps({'id':r['id'],'result':True}),flush=True)\n")

    async def scenario():
        default = WorkerClient([sys.executable, str(script)])
        bounded = WorkerClient([sys.executable, str(script)], timeout=0.001)
        try:
            assert default.timeout is None
            assert await default.request('probe', {}) is True
            from agent.storage import CoreDataError
            with pytest.raises(CoreDataError, match='timed out'):
                await bounded.request('probe', {})
            assert bounded.process is None
        finally:
            await default.close()
            await bounded.close()

    asyncio.run(scenario())
