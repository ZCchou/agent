import asyncio
import json
import sys

import pytest

from agent.rag.models import ModelStore
from agent.rag.worker_client import WorkerClient
from agent.storage import CoreDataError
from test_rag_models import model_directory


def store_with_models(tmp_path, monkeypatch, count=3, reranker=False):
    monkeypatch.setattr('agent.rag.models.system_memory', lambda: {'available_physical_bytes': 16 * 1024**3})
    store = ModelStore(tmp_path / 'data')
    records = []
    for number in range(count):
        source = model_directory(tmp_path / str(number))
        (source / 'tokenizer_config.json').write_text(json.dumps({'name': str(number)}))
        profile = 'bge-m3'
        if reranker:
            profile = 'bge-reranker-v2-m3'
            (source / 'config.json').write_text(json.dumps({'model_type': 'xlm-roberta',
                'architectures': ['XLMRobertaForSequenceClassification'], 'hidden_size': 1024}))
        records.append(asyncio.run(store.import_model(source, profile)))
    script = tmp_path / 'worker.py'
    script.write_text("import sys,json,os\nfor line in sys.stdin:\n r=json.loads(line);p=r['params'];v={'available':True,'pid':os.getpid()} if r['action']=='probe' else [len(t) for t in p['texts']] if r['action']=='rerank_lengths' else [float(t)/100 for t in p['texts']];print(json.dumps({'id':r['id'],'result':v}),flush=True)\n")
    monkeypatch.setattr(store, '_command', lambda: [sys.executable, str(script)])
    return store, records


def test_cpu_residency_preserves_two_models_and_evicts_lru(tmp_path, monkeypatch):
    store, records = store_with_models(tmp_path, monkeypatch)
    monkeypatch.setattr('agent.rag.models.system_memory', lambda: {'available_physical_bytes': 16 * 1024**3})
    async def exercise():
        try:
            first = await store.probe(records[0]['id'])
            second = await store.probe(records[1]['id'])
            assert (await store.probe(records[0]['id']))['pid'] == first['pid']
            await store.probe(records[2]['id'])
            assert (await store.probe(records[0]['id']))['pid'] == first['pid']
            assert (await store.probe(records[1]['id']))['pid'] != second['pid']
        finally:
            await store.close()
    asyncio.run(exercise())


def test_cpu_admission_reclaims_idle_model_to_keep_two_gib_reserve(tmp_path, monkeypatch):
    store, records = store_with_models(tmp_path, monkeypatch, count=2)
    available = [16 * 1024**3]
    monkeypatch.setattr('agent.rag.models.system_memory', lambda: {'available_physical_bytes': available[0]})
    async def exercise():
        try:
            await store.probe(records[0]['id'])
            worker = store._workers['cpu']
            old = worker.process
            original_close = worker.close
            async def release_memory():
                await original_close()
                available[0] = 16 * 1024**3
            monkeypatch.setattr(worker, 'close', release_memory)
            available[0] = 2 * 1024**3
            assert (await store.probe(records[1]['id']))['available']
            assert old.returncode is not None
        finally:
            await store.close()
    asyncio.run(exercise())


def test_rerank_deadline_covers_device_queue_without_stopping_another_request(tmp_path, monkeypatch):
    store, records = store_with_models(tmp_path, monkeypatch, count=1, reranker=True)
    async def exercise():
        try:
            async with store._device_locks['cpu']:
                with pytest.raises(CoreDataError) as failure:
                    await store.rerank(records[0]['id'], 'query', ['1'], timeout=0)
                assert failure.value.code == 'rag_rerank_timeout'
                assert failure.value.details['completed'] == 0
            assert await store.rerank(records[0]['id'], 'query', ['1']) == [0.01]
        finally:
            await store.close()
    asyncio.run(exercise())


def test_worker_client_consumes_progress_frames_and_keeps_response_framing(tmp_path):
    script = tmp_path / 'progress.py'
    script.write_text("import sys,json\nfor line in sys.stdin:\n r=json.loads(line);print(json.dumps({'id':r['id'],'progress':{'stage':'reranking','completed':2,'total':3}}),flush=True);print(json.dumps({'id':r['id'],'result':[0.1,0.2,0.3]}),flush=True)\n")
    async def exercise():
        client = WorkerClient([sys.executable, str(script)])
        events = []
        try:
            result = await client.request('rerank', {}, progress=events.append)
            assert result == [0.1, 0.2, 0.3]
            assert events == [{'stage': 'reranking', 'completed': 2, 'total': 3}]
        finally:
            await client.close()
    asyncio.run(exercise())


def test_token_budget_caps_reranker_but_preserves_embedding_ceiling():
    from agent.rag.worker import _token_limit
    assert _token_limit({'profile': 'bge-reranker-v2-m3'}) == 2048
    assert _token_limit({'profile': 'bge-reranker-v2-m3', 'token_budget': 9000}, {'max_length': 512}) == 512
    assert _token_limit({'profile': 'bge-m3', 'token_budget': 3}) == 8192


def test_length_aware_batches_use_tokenizer_and_preserve_score_order(monkeypatch):
    import agent.rag.worker as worker
    class Tokenizer:
        def __call__(self, texts, **kwargs):
            return {'input_ids': [[1] * int(text) for text in texts]}
    monkeypatch.setattr(worker, '_engine', lambda *args: {})
    monkeypatch.setattr(worker, '_load', lambda *args: (Tokenizer(), object(), {'max_length': 8192}))
    batches = []
    def score(params, tokenizer, model, metadata, texts):
        batches.append(texts)
        return [int(text) / 10000 for text in texts]
    monkeypatch.setattr(worker, '_onnx_batch', score)
    params = {'profile': 'bge-reranker-v2-m3', 'backend': 'onnx', 'device': 'cpu',
              'texts': ['1000', '2', '900', '3'], 'query': 'query'}
    assert worker.dispatch({'action': 'rerank', 'params': params}, {}) == [0.1, 0.0002, 0.09, 0.0003]
    assert batches == [['2', '3'], ['900', '1000']]


@pytest.mark.parametrize('backend,fail_pairs', [('onnx', False), ('onnx', True), ('transformers', False)])
def test_model_store_groups_actual_token_lengths_and_resumes_original_score_positions(tmp_path, monkeypatch, backend, fail_pairs):
    store, records = store_with_models(tmp_path, monkeypatch, count=1, reranker=True)
    store._state['models'][0]['backend'] = backend
    log = tmp_path / 'actual-batches.jsonl'
    script = tmp_path / 'length_worker.py'
    script.write_text(f'''import json
from pathlib import Path
from tokenizers import Tokenizer
from tokenizers.models import WordLevel
from tokenizers.pre_tokenizers import Whitespace
from transformers import PreTrainedTokenizerFast
from types import SimpleNamespace
import torch
from agent.rag import worker
backend = Tokenizer(WordLevel({{'[UNK]': 0, '[PAD]': 1, 'query': 2, 'document': 3}}, unk_token='[UNK]'))
backend.pre_tokenizer = Whitespace()
tokenizer = PreTrainedTokenizerFast(tokenizer_object=backend, unk_token='[UNK]', pad_token='[PAD]')
worker._engine = lambda *args: {{}}
def score_lengths(lengths):
    with Path({str(log)!r}).open('a') as stream:
        stream.write(json.dumps(lengths) + '\\n')
    if {fail_pairs!r} and len(lengths) > 1:
        raise RuntimeError('Failed to allocate memory')
    return [length / 10000 for length in lengths]
def score(params, tokenizer, model, metadata, texts):
    return score_lengths([len(text.split()) for text in texts])
class Classifier:
    def __call__(self, **inputs):
        lengths = (inputs['input_ids'] == 3).sum(dim=1).tolist()
        scores = torch.tensor(score_lengths(lengths), dtype=torch.float32)
        return SimpleNamespace(logits=torch.logit(scores).reshape(-1, 1))
worker._load = lambda *args: (tokenizer, Classifier(), {{'max_length': 8192}})
worker._onnx_batch = score
worker.main()
''', encoding='utf-8')
    monkeypatch.setattr(store, '_command', lambda: [sys.executable, str(script)])
    async def exercise():
        lengths = [1000, 2, 900, 3] if backend == 'onnx' else [1000, 2, 900, 3, 800, 4, 700, 1]
        texts = ['document ' * length for length in lengths]
        async def cancel_checkpoint(event):
            if event['completed'] == (2 if backend == 'onnx' else 4):
                raise asyncio.CancelledError()
        try:
            with pytest.raises(asyncio.CancelledError):
                await store.rerank(records[0]['id'], 'query', texts, progress=cancel_checkpoint)
            assert await store.rerank(records[0]['id'], 'query', texts) == pytest.approx([length / 10000 for length in lengths])
            batches = [json.loads(line) for line in log.read_text().splitlines()]
            expected = ([[2, 3], [2], [3], [900, 1000], [900], [1000]] if fail_pairs
                        else [[2, 3], [900, 1000]])
            if backend == 'transformers':
                expected = [[1, 2, 3, 4], [700, 800, 900, 1000]]
            assert batches == expected
        finally:
            await store.close()
    asyncio.run(exercise())


def test_rerank_retry_reuses_only_completed_current_identity_batches(tmp_path, monkeypatch):
    store, records = store_with_models(tmp_path, monkeypatch, count=1, reranker=True)
    store._state['models'][0]['backend'] = 'onnx'
    log = tmp_path / 'calls.jsonl'
    script = tmp_path / 'resume.py'
    script.write_text(f"import sys,json\nfrom pathlib import Path\nfor line in sys.stdin:\n r=json.loads(line);texts=r['params']['texts']\n if r['action']=='rerank_lengths':\n  print(json.dumps({{'id':r['id'],'result':[len(t) for t in texts]}}),flush=True);continue\n p=Path({str(log)!r});p.open('a').write(json.dumps(texts)+'\\n');print(json.dumps({{'id':r['id'],'result':[float(t)/100 for t in texts]}}),flush=True)\n")
    monkeypatch.setattr(store, '_command', lambda: [sys.executable, str(script)])
    async def exercise():
        events = []
        async def cancel_after_batch(event):
            events.append(event)
            if event['completed'] == 2:
                raise asyncio.CancelledError()
        try:
            with pytest.raises(asyncio.CancelledError):
                await store.rerank(records[0]['id'], 'query', ['1', '2', '3'], progress=cancel_after_batch, cache_key='index-generation-1')
            assert await store.rerank(records[0]['id'], 'query', ['1', '2', '3'], cache_key='index-generation-1') == [0.01, 0.02, 0.03]
            assert [json.loads(line) for line in log.read_text().splitlines()] == [['1', '2'], ['3']]
            await store.rerank(records[0]['id'], 'query', ['1', '2', '3'], cache_key='index-generation-2')
            assert [json.loads(line) for line in log.read_text().splitlines()][-2:] == [['1', '2'], ['3']]
        finally:
            await store.close()
    asyncio.run(exercise())


def test_device_change_invalidates_resident_worker_and_partial_scores(tmp_path, monkeypatch):
    store, records = store_with_models(tmp_path, monkeypatch, count=1)
    async def exercise():
        try:
            assert (await store.probe(records[0]['id']))['available']
            worker = store._workers['cpu']
            store.set_device(records[0]['id'], 'cuda')
            assert worker._closed
            assert not store._resident_workers
            assert not store._loaded_models
        finally:
            await store.close()
    asyncio.run(exercise())


def test_concurrent_probes_ignore_registry_status_only_changes(tmp_path, monkeypatch):
    store, records = store_with_models(tmp_path, monkeypatch, count=1)
    async def exercise():
        try:
            results = await asyncio.gather(*(store.probe(records[0]['id']) for _ in range(3)))
            assert all(result['available'] for result in results)
            assert len({result['pid'] for result in results}) == 1
        finally:
            await store.close()
    asyncio.run(exercise())


def test_measured_peak_memory_is_used_for_cpu_admission(tmp_path, monkeypatch):
    store, records = store_with_models(tmp_path, monkeypatch, count=1)
    monkeypatch.setattr('agent.rag.models.system_memory', lambda: {'available_physical_bytes': 5 * 1024**3})
    store._model_peaks[(records[0]['id'], records[0]['digest'])] = 4 * 1024**3
    async def exercise():
        try:
            result = await store.probe(records[0]['id'])
            assert not result['available']
            assert not store._resident_workers
        finally:
            await store.close()
    asyncio.run(exercise())


def test_deadline_after_a_completed_batch_returns_counts_and_resumes(tmp_path, monkeypatch):
    import agent.rag.models as models
    store, records = store_with_models(tmp_path, monkeypatch, count=1, reranker=True)
    store._state['models'][0]['backend'] = 'onnx'
    original_timeout = asyncio.timeout
    deadlines = []
    def controlled_timeout(delay):
        context = original_timeout(delay)
        deadlines.append(context)
        return context
    monkeypatch.setattr(models.asyncio, 'timeout', controlled_timeout)
    async def exercise():
        async def expire_after_batch(event):
            if event['completed'] == 2:
                deadlines[-1].reschedule(asyncio.get_running_loop().time() - 1)
        try:
            with pytest.raises(CoreDataError) as failure:
                await store.rerank(records[0]['id'], 'query', ['1', '2', '3'], progress=expire_after_batch, timeout=90)
            assert failure.value.code == 'rag_rerank_timeout'
            assert failure.value.details['completed'] == 2
            assert not store._resident_workers
            assert await store.rerank(records[0]['id'], 'query', ['1', '2', '3']) == [0.01, 0.02, 0.03]
        finally:
            await store.close()
    asyncio.run(exercise())


@pytest.mark.parametrize('scope,expected', [('focused', 20), ('broad', 40), (None, 40)])
def test_search_scope_sets_candidate_budget_without_changing_evidence_text(tmp_path, scope, expected):
    from agent.rag.service import RagService
    from agent.storage import CoreRepository
    from test_rag_service import Engines, ready, wait_job
    class RecordingEngine(Engines):
        inputs = []
        async def rerank(self, model_id, query, texts, **kwargs):
            self.inputs.extend(texts)
            return [0.5] * len(texts)
    async def exercise():
        core = CoreRepository(tmp_path / 'agent.db')
        engine = RecordingEngine()
        service = RagService(core, engine)
        project = core.create_project('Project', 'work')['id']
        try:
            library = (await service.handle('library.create', {'name': 'Rules', 'embedding_model_id': 'embed', 'reranker_model_id': 'rank'}))['library']['id']
            content = '\n\n'.join(f'25.{1000 + index} The safety requirement for component {index}.' for index in range(45))
            await ready(service, library, content=content)
            assert (await wait_job(service, await service.handle('index.build', {'library_id': library})))['status'] == 'completed'
            await service.handle('library.bind', {'library_id': library, 'project_id': project, 'enabled': True})
            result = await service.search(project, 'safety requirement', retrieval_scope=scope)
            assert len(engine.inputs) == expected
            assert result['evidence'] and all(row['text'] in content for row in result['evidence'])
        finally:
            await service.close()
    asyncio.run(exercise())


def test_service_deadline_retains_candidates_and_publishes_only_complete_ranking(tmp_path, monkeypatch):
    from contextlib import closing
    import agent.rag.service as module
    from agent.storage import CoreRepository
    from test_rag_service import Engines, ready, wait_job
    class PausedEngine(Engines):
        paused = True
        async def rerank(self, model_id, query, texts):
            if self.paused:
                await asyncio.Event().wait()
            return [0.5] * len(texts)
    async def exercise():
        core = CoreRepository(tmp_path / 'agent.db')
        engine = PausedEngine()
        service = module.RagService(core, engine)
        project = core.create_project('Project', 'work')['id']
        try:
            library = (await service.handle('library.create', {'name': 'Rules', 'embedding_model_id': 'embed', 'reranker_model_id': 'rank'}))['library']['id']
            await ready(service, library)
            assert (await wait_job(service, await service.handle('index.build', {'library_id': library})))['status'] == 'completed'
            await service.handle('library.bind', {'library_id': library, 'project_id': project, 'enabled': True})
            service.rerank_timeout = 0
            with pytest.raises(CoreDataError) as failure:
                await service.search(project, '失效')
            assert failure.value.code == 'rag_rerank_timeout'
            assert failure.value.details['total'] == 1
            assert len(service._rerank_attempts) == 1
            with closing(service.repository.connect()) as connection:
                assert connection.execute('SELECT count(*) FROM rag_searches').fetchone()[0] == 0
                assert connection.execute('SELECT count(*) FROM rag_evidence').fetchone()[0] == 0
            engine.paused = False
            service.rerank_timeout = None
            assert len((await service.search(project, '失效'))['evidence']) == 1
            assert not service._rerank_attempts
        finally:
            await service.close()
    asyncio.run(exercise())
