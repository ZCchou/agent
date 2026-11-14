import asyncio
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from agent.execution import execution_wait
from agent.rag import models
from agent.storage import CoreDataError, CoreRepository
from test_rag_models import model_directory


@pytest.mark.parametrize('component', ['runtime', 'inventory', 'model', 'model_inventory'])
def test_integrity_reads_keep_review_running_after_previous_worker_heartbeat(
        tmp_path, monkeypatch, component):
    store = models.ModelStore(tmp_path / 'data')
    if component in {'runtime', 'inventory'}:
        runtime = tmp_path / 'app/rag-runtime'
        runtime.mkdir(parents=True)
        target = runtime / 'rag-worker.exe'
        target.write_bytes(b'x' * (8 * 1024**2) if component == 'runtime' else b'worker')
        payloads = {target.name: hashlib.sha256(target.read_bytes()).hexdigest()}
        if component == 'inventory':
            for index in range(7):
                asset = runtime / f'asset-{index}.dll'
                asset.write_bytes(b'asset')
                payloads[asset.name] = hashlib.sha256(b'asset').hexdigest()
            original_linked = models._linked

            def linked(path):
                if path.parent == runtime and path.name != 'runtime.json':
                    time.sleep(.02)
                return original_linked(path)

            monkeypatch.setattr(models, '_linked', linked)
        (runtime / 'runtime.json').write_text(json.dumps({
            'schema_version': 1, 'protocol_version': 2, 'executable': target.name,
            'files': payloads,
        }))
        monkeypatch.setattr(models, 'sys', SimpleNamespace(
            frozen=True, executable=str(runtime.parent / 'agent-core.exe')))
        verify = store._command
    else:
        source = model_directory(tmp_path / 'source')
        # The package validator accepts supplementary immutable tokenizer assets.
        (source / 'vocabulary.txt').write_bytes(b'x' * (8 * 1024**2))
        record = asyncio.run(store.import_model(source, 'bge-m3'))
        target = store.root / record['id'] / 'vocabulary.txt'
        verify = lambda: store._verified_path(record, store.root, True)
        if component == 'model_inventory':
            original_linked = models._linked

            def linked(path):
                if path.parent == target.parent:
                    time.sleep(.02)
                return original_linked(path)

            monkeypatch.setattr(models, '_linked', linked)

    original_open = Path.open

    class SlowReader:
        def __init__(self, stream):
            self.stream = stream

        def read(self, size):
            time.sleep(.02)
            return self.stream.read(size)

    @contextmanager
    def open_file(path, *args, **kwargs):
        with original_open(path, *args, **kwargs) as stream:
            yield SlowReader(stream) if path == target and args == ('rb',) else stream

    monkeypatch.setattr(Path, 'open', open_file)
    monkeypatch.setattr(execution_wait, 'HEARTBEAT_LOSS_SECONDS', .08)
    monkeypatch.setattr(models, 'INTEGRITY_REPORT_INTERVAL_SECONDS', .01, raising=False)
    events = []

    async def tick(seconds):
        await asyncio.sleep(.005)

    repository = CoreRepository(tmp_path / 'core.db')
    monitor = execution_wait.ExecutionMonitor(repository, sleep=tick, on_event=events.append)
    root = monitor.start_task(run_id='review-with-integrity-' + component)

    async def operation(activity):
        activity.heartbeat(worker='previous-embedding-request')
        return await asyncio.to_thread(verify)

    asyncio.run(monitor.execute_stage(root['id'], 'document_review', operation))
    assert not [event for event in events if event['event'] == 'waiting_recovery']
    with repository._connect() as connection:
        progress = [json.loads(row[0]) for row in connection.execute(
            "SELECT payload_json FROM execution_wait_activity WHERE kind='progress'")]
    if component == 'inventory':
        assert any(item.get('integrity_phase') == 'runtime_inventory'
                   and item.get('integrity_files_processed') == 8 for item in progress)
    else:
        assert any(item.get('integrity_bytes_read', 0) >= 4 * 1024**2 for item in progress)
    assert monitor.get_task(root['id'])['auto_recoveries'] == 0

    # Progress reporting must preserve the actual integrity decision.
    with original_open(target, 'ab') as stream:
        stream.write(b'tampered')
    with pytest.raises(CoreDataError, match='hash|integrity|changed'):
        verify()
