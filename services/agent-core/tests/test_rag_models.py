import asyncio
import hashlib
import json
import struct
from pathlib import Path

import pytest

from agent.rag.models import ModelStore
from agent.storage import CoreDataError


@pytest.mark.parametrize('fail_pairs', [False, True])
@pytest.mark.parametrize('action', ['embed', 'rerank'])
def test_onnx_small_batches_and_memory_retry_preserve_results(tmp_path, monkeypatch, fail_pairs, action):
    import sys
    script = tmp_path / 'batch_worker.py'
    log = tmp_path / 'batches.jsonl'
    script.write_text(f'''import sys
import json
from pathlib import Path

for line in sys.stdin:
    request = json.loads(line)
    texts = request['params'].get('texts', [])
    with Path({str(log)!r}).open('a') as stream:
        stream.write(json.dumps(texts) + '\\n')
    if {fail_pairs!r} and len(texts) > 1:
        response = {{'id': request['id'], 'error': 'Failed to allocate memory for requested buffer'}}
    else:
        values = [[float(text)] + [0.0] * 1023 for text in texts] if request['action'] == 'embed' else [float(text) / 10 for text in texts]
        response = {{'id': request['id'], 'result': values}}
    print(json.dumps(response), flush=True)
''', encoding='utf-8')
    store = ModelStore(tmp_path / 'data')
    source = model_directory(tmp_path / 'source')
    profile = 'bge-m3'
    if action == 'rerank':
        profile = 'bge-reranker-v2-m3'
        (source / 'config.json').write_text(json.dumps({'model_type': 'xlm-roberta',
            'architectures': ['XLMRobertaForSequenceClassification'], 'hidden_size': 1024, 'num_labels': 1}))
    record = asyncio.run(store.import_model(source, profile))
    store._state['models'][0]['backend'] = 'onnx'
    monkeypatch.setattr(store, '_command', lambda: [sys.executable, str(script)])
    async def exercise():
        try:
            texts = ['0', '1', '2', '3', '4']
            if action == 'embed':
                vectors = await store.embed(record['id'], texts)
                assert [vector[0] for vector in vectors] == [0, 1, 2, 3, 4]
            else:
                assert await store.rerank(record['id'], 'query', texts) == [0, 0.1, 0.2, 0.3, 0.4]
        finally:
            await store.close()
    asyncio.run(exercise())
    batches = [json.loads(line) for line in log.read_text().splitlines()]
    assert max(map(len, batches)) <= 2
    assert batches == ([['0', '1'], ['0'], ['1'], ['2'], ['3'], ['4']] if fail_pairs else [['0', '1'], ['2', '3'], ['4']])


def model_directory(root, profile="bge-m3"):
    root.mkdir()
    config = {"model_type": "xlm-roberta", "architectures": ["XLMRobertaModel"], "hidden_size": 1024}
    (root / "config.json").write_text(json.dumps(config))
    (root / "tokenizer_config.json").write_text("{}")
    (root / "tokenizer.json").write_text('{"version":"1.0"}')
    header = json.dumps({"test": {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]}}).encode()
    (root / "model.safetensors").write_bytes(struct.pack("<Q", len(header)) + header + b"\0" * 4)
    return root


def test_inspect_validates_supported_directory_and_import_is_owned_copy(tmp_path):
    source = model_directory(tmp_path / "source")
    store = ModelStore(tmp_path / "data")
    inspection = store.inspect(source, "bge-m3")
    assert inspection["valid"] and inspection["profile"] == "bge-m3"
    record = asyncio.run(store.import_model(source, "bge-m3"))
    assert record["dimension"] == 1024
    assert record["digest"] and record["status"] == "imported"
    assert store.list_models()[0]["id"] == record["id"]
    source.joinpath("config.json").unlink()
    assert store.inspect(store.root / record["id"], "bge-m3")["valid"]
    assert ModelStore(tmp_path / "data").list_models() == store.list_models()


@pytest.mark.parametrize("filename", ["pytorch_model.bin", "custom.py", "weights.pkl"])
def test_inspect_rejects_unsafe_files(tmp_path, filename):
    source = model_directory(tmp_path / "source")
    source.joinpath(filename).write_bytes(b"unsafe")
    result = ModelStore(tmp_path / "data").inspect(source, "bge-m3")
    assert not result["valid"]
    assert result["errors"]


def test_inspect_rejects_missing_shard_and_remote_code(tmp_path):
    source = model_directory(tmp_path / "source")
    source.joinpath("model.safetensors.index.json").write_text(json.dumps({"weight_map": {"x": "missing.safetensors"}}))
    result = ModelStore(tmp_path / "data").inspect(source, "bge-m3")
    assert not result["valid"]
    source.joinpath("model.safetensors.index.json").unlink()
    source.joinpath("config.json").write_text(json.dumps({"model_type": "xlm-roberta", "auto_map": {"AutoModel": "evil.Model"}}))
    assert not ModelStore(tmp_path / "data").inspect(source, "bge-m3")["valid"]


def test_inspect_rejects_truncated_safetensors(tmp_path):
    source = model_directory(tmp_path / "source")
    source.joinpath("model.safetensors").write_bytes(b"broken")
    assert not ModelStore(tmp_path / "data").inspect(source, "bge-m3")["valid"]


def test_device_validation_and_root_migration_preserve_models(tmp_path):
    store = ModelStore(tmp_path / "data")
    record = asyncio.run(store.import_model(model_directory(tmp_path / "source"), "bge-m3"))
    with pytest.raises(CoreDataError):
        store.set_device(record["id"], "auto")
    store.set_root(tmp_path / "new-root")
    assert store.root.joinpath(record["id"], "model.safetensors").is_file()
    assert store.list_models()[0]["digest"] == record["digest"]
    store.remove(record["id"])
    assert store.list_models() == []


def test_modified_managed_model_cannot_run(tmp_path):
    store = ModelStore(tmp_path / "data")
    record = asyncio.run(store.import_model(model_directory(tmp_path / "source"), "bge-m3"))
    store.root.joinpath(record["id"], "config.json").write_text("{}")
    with pytest.raises(CoreDataError, match="hash|integrity|changed"):
        asyncio.run(store.embed(record["id"], ["hi"]))


def test_probe_reports_missing_engine_without_marking_model_ready(tmp_path):
    store = ModelStore(tmp_path / "data")
    record = asyncio.run(store.import_model(model_directory(tmp_path / "source"), "bge-m3"))
    async def exercise():
        result = await store.probe(record["id"])
        assert result["available"] is False
        assert result["error"]
        await store.close()
    asyncio.run(exercise())



def test_runtime_manifest_rejects_tampered_executable(tmp_path, monkeypatch):
    import agent.rag.models as models
    base = tmp_path / "app"
    runtime = base / "rag-runtime"
    runtime.mkdir(parents=True)
    executable = runtime / "rag-worker.exe"
    executable.write_bytes(b"trusted build")
    manifest = {"schema_version": 1, "protocol_version": 1, "executable": "rag-worker.exe", "files": {"rag-worker.exe": hashlib.sha256(executable.read_bytes()).hexdigest()}}
    (runtime / "runtime.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(models.sys, "frozen", True, raising=False)
    monkeypatch.setattr(models.sys, "executable", str(base / "agent-core.exe"))
    store = ModelStore(tmp_path / "data")
    assert store._command() == [str(executable)]
    executable.write_bytes(b"changed")
    with pytest.raises(CoreDataError, match="hash mismatch"):
        store._command()


def test_model_config_profile_mismatch_is_rejected(tmp_path):
    source = model_directory(tmp_path / "source")
    inspection = ModelStore(tmp_path / "data").inspect(source, "qwen3-embedding-0.6b")
    assert not inspection["valid"]


@pytest.mark.parametrize("architecture", ["Qwen3ForCausalLM", "Qwen3Model"])
def test_explicit_qwen_embedding_import_accepts_official_config(tmp_path, architecture):
    source = model_directory(tmp_path / "source")
    source.joinpath("config.json").write_text(json.dumps({
        "model_type": "qwen3", "architectures": [architecture],
        "hidden_size": 1024, "torch_dtype": "bfloat16", "tie_word_embeddings": True,
    }))
    header = json.dumps({"embed_tokens.weight": {
        "dtype": "BF16", "shape": [1], "data_offsets": [0, 2],
    }}).encode()
    source.joinpath("model.safetensors").write_bytes(struct.pack("<Q", len(header)) + header + b"\0\0")
    store = ModelStore(tmp_path / "data")
    record = asyncio.run(store.import_model(source, "qwen3-embedding-0.6b"))
    assert record["role"] == "embedding"
    assert record["dimension"] == 1024
    assert store.inspect(store.root / record["id"], "qwen3-embedding-0.6b")["valid"]
    automatic = store.inspect(source)
    if architecture == "Qwen3ForCausalLM":
        assert not automatic["valid"]
        assert "explicit" in " ".join(automatic["errors"]).lower()
    else:
        assert automatic["profile"] == "qwen3-embedding-0.6b"


def test_retry_import_returns_same_immutable_identity(tmp_path):
    store = ModelStore(tmp_path / "data")
    source = model_directory(tmp_path / "source")
    first = asyncio.run(store.import_model(source, "bge-m3"))
    second = asyncio.run(store.import_model(source, "bge-m3"))
    assert first == second
    assert len(store.list_models()) == 1


def test_onnx_manifest_rejects_path_traversal(tmp_path):
    source = model_directory(tmp_path / "source")
    manifest = {"schema_version": 1, "backend": "onnx", "profile": "bge-m3", "dimension": 1024, "pooling": "cls", "max_length": 512, "graph": "../escape.onnx", "output": "last_hidden_state"}
    (source / "rag-model.json").write_text(json.dumps(manifest))
    inspection = ModelStore(tmp_path / "data").inspect(source)
    assert not inspection["valid"]
    assert "path" in inspection["errors"][0]


def test_runtime_rejects_unlisted_executable_files(tmp_path, monkeypatch):
    import agent.rag.models as models
    base = tmp_path / "app"
    runtime = base / "rag-runtime"
    runtime.mkdir(parents=True)
    executable = runtime / "rag-worker.exe"
    executable.write_bytes(b"trusted build")
    (runtime / "runtime.json").write_text(json.dumps({"schema_version": 1, "protocol_version": 1, "executable": "rag-worker.exe", "files": {"rag-worker.exe": hashlib.sha256(executable.read_bytes()).hexdigest()}}))
    (runtime / "injected.dll").write_bytes(b"unlisted")
    monkeypatch.setattr(models.sys, "frozen", True, raising=False)
    monkeypatch.setattr(models.sys, "executable", str(base / "agent-core.exe"))
    with pytest.raises(CoreDataError, match="unlisted or missing"):
        ModelStore(tmp_path / "data")._command()


def test_model_store_close_prevents_new_inference_workers(tmp_path):
    store = ModelStore(tmp_path / "data")
    record = asyncio.run(store.import_model(model_directory(tmp_path / "source"), "bge-m3"))
    async def exercise():
        await store.close()
        with pytest.raises(CoreDataError, match="closed"):
            await store.embed(record["id"], ["query"])
    asyncio.run(exercise())


@pytest.mark.parametrize("graph_folder", ["", "onnx"])
def test_adapted_onnx_bge_package_embeds_through_real_worker(tmp_path, graph_folder):
    import numpy as np
    import onnx
    from onnx import TensorProto, helper, numpy_helper
    from tokenizers import Tokenizer
    from tokenizers.models import WordLevel
    from transformers import PreTrainedTokenizerFast
    source = model_directory(tmp_path / "onnx")
    tokenizer = PreTrainedTokenizerFast(tokenizer_object=Tokenizer(WordLevel({"[UNK]": 0, "[PAD]": 1, "hello": 2}, unk_token="[UNK]")), unk_token="[UNK]", pad_token="[PAD]")
    tokenizer.save_pretrained(source)
    graph = helper.make_graph([
        helper.make_node("Cast", ["input_ids"], ["float_ids"], to=TensorProto.FLOAT),
        helper.make_node("Unsqueeze", ["float_ids", "axes"], ["expanded"]),
        helper.make_node("Tile", ["expanded", "repeats"], ["tiled"]),
        helper.make_node("Add", ["tiled", "bias"], ["last_hidden_state"]),
    ], "tiny_bge_contract", [helper.make_tensor_value_info("input_ids", TensorProto.INT64, [None, None]), helper.make_tensor_value_info("attention_mask", TensorProto.INT64, [None, None])], [helper.make_tensor_value_info("last_hidden_state", TensorProto.FLOAT, [None, None, 1024])], [numpy_helper.from_array(np.array([2], dtype=np.int64), "axes"), numpy_helper.from_array(np.array([1, 1, 1024], dtype=np.int64), "repeats"), numpy_helper.from_array(np.zeros(1024, dtype=np.float32), "bias")])
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)], ir_version=10)
    graph_root = source / graph_folder
    graph_root.mkdir(exist_ok=True)
    onnx.save_model(model, graph_root / "model.onnx", save_as_external_data=True, all_tensors_to_one_file=True, location="weights.data", size_threshold=1024)
    (source / "model.safetensors").unlink()
    (source / "rag-model.json").write_text(json.dumps({"schema_version": 1, "profile": "bge-m3", "backend": "onnx", "graph": (Path(graph_folder) / "model.onnx").as_posix(), "output": "last_hidden_state", "pooling": "cls", "dimension": 1024, "max_length": 512}))
    store = ModelStore(tmp_path / "data")
    inspection = store.inspect(source)
    assert inspection["valid"], inspection["errors"]
    async def exercise():
        record = await store.import_model(source, "bge-m3")
        try:
            probe = await store.probe(record["id"])
            assert probe["available"], probe
            vectors = await store.embed(record["id"], ["hello", "hello"])
            assert len(vectors) == 2
            assert len(vectors[0]) == 1024
            assert np.allclose(np.linalg.norm(vectors, axis=1), 1)
            assert np.allclose(vectors[0], np.full(1024, 1 / 32))
        finally:
            await store.close()
    asyncio.run(exercise())



def test_malformed_onnx_graph_returns_inspection_errors(tmp_path):
    source = model_directory(tmp_path / "malformed")
    (source / "broken.onnx").write_bytes(b"invalid protobuf")
    (source / "rag-model.json").write_text(json.dumps({"schema_version": 1, "backend": "onnx", "profile": "bge-m3", "dimension": 1024, "pooling": "cls", "max_length": 512, "graph": "broken.onnx", "output": "last_hidden_state"}))
    result = ModelStore(tmp_path / "data").inspect(source)
    assert result["valid"] is False
    assert result["errors"]


def test_remove_cleanup_failure_does_not_block_reimport(tmp_path, monkeypatch):
    import agent.rag.models as models
    store = ModelStore(tmp_path / "data")
    source = model_directory(tmp_path / "source")
    record = asyncio.run(store.import_model(source, "bge-m3"))
    original = models.shutil.rmtree
    def denied(path):
        raise PermissionError("file held by another application")
    monkeypatch.setattr(models.shutil, "rmtree", denied)
    result = store.remove(record["id"])
    assert result["removed"] == record["id"]
    assert result["cleanup_pending"]
    assert store.list_models() == []
    monkeypatch.setattr(models.shutil, "rmtree", original)
    assert asyncio.run(store.import_model(source, "bge-m3"))["id"] == record["id"]


def test_embedding_batches_reuse_hash_verification_and_keep_loop_responsive(tmp_path, monkeypatch):
    import time
    import agent.rag.models as models
    script = tmp_path / "stdio.py"
    script.write_text("import sys,json\nfor line in sys.stdin:\n r=json.loads(line);v=[[1.0]+[0.0]*1023 for text in r['params'].get('texts',[])];print(json.dumps({'id':r['id'],'result':v}),flush=True)\n")
    store = ModelStore(tmp_path / "data")
    record = asyncio.run(store.import_model(model_directory(tmp_path / "source"), "bge-m3"))
    monkeypatch.setattr(store, "_command", lambda: [__import__('sys').executable, str(script)])
    original = models._hash
    hashes = []
    def slow_hash(path):
        hashes.append(path.name)
        time.sleep(0.03)
        return original(path)
    monkeypatch.setattr(models, "_hash", slow_hash)
    async def exercise():
        ticks = []
        finished = asyncio.Event()
        async def heartbeat():
            while not finished.is_set():
                ticks.append(time.monotonic())
                await asyncio.sleep(0.005)
        ticker = asyncio.create_task(heartbeat())
        await asyncio.sleep(0)
        try:
            await store.embed(record["id"], ["one"])
            first_count = len(hashes)
            assert first_count == 4
            assert len(ticks) >= 8
            assert max(b - a for a, b in zip(ticks, ticks[1:])) < 0.1
            await store.embed(record["id"], ["two"])
            await store.embed(record["id"], ["three"])
            assert len(hashes) == first_count
            path = store.root / record["id"] / "config.json"
            os = __import__('os')
            stamp = path.stat()
            os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1000000))
            await store.embed(record["id"], ["changed stat"])
            assert len(hashes) == first_count * 2
        finally:
            finished.set()
            await ticker
            await store.close()
    asyncio.run(exercise())


def test_import_copy_does_not_hold_registry_read_lock(tmp_path, monkeypatch):
    import time
    import threading
    import agent.rag.models as models
    source = model_directory(tmp_path / "source")
    store = ModelStore(tmp_path / "data")
    entered = threading.Event()
    release = threading.Event()
    original = models.shutil.copyfile
    def slow_copy(source, target):
        entered.set()
        release.wait(timeout=0.25)
        return original(source, target)
    monkeypatch.setattr(models.shutil, "copyfile", slow_copy)
    async def exercise():
        task = asyncio.create_task(store.import_model(source, "bge-m3"))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            start = time.monotonic()
            store.list_models()
            assert time.monotonic() - start < 0.1
        finally:
            release.set()
            await task
    asyncio.run(exercise())


def test_runtime_status_is_lightweight_but_worker_admission_checks_hashes(tmp_path, monkeypatch):
    import agent.rag.models as models
    base = tmp_path / "app"
    runtime = base / "rag-runtime"
    runtime.mkdir(parents=True)
    executable = runtime / "rag-worker.exe"
    executable.write_bytes(b"trusted build")
    (runtime / "runtime.json").write_text(json.dumps({"schema_version": 1, "protocol_version": 1, "executable": "rag-worker.exe", "files": {"rag-worker.exe": hashlib.sha256(executable.read_bytes()).hexdigest()}}))
    monkeypatch.setattr(models.sys, "frozen", True, raising=False)
    monkeypatch.setattr(models.sys, "executable", str(base / "agent-core.exe"))
    store = ModelStore(tmp_path / "data")
    original = models._hash
    hashes = []
    def counted_hash(path):
        hashes.append(path)
        return original(path)
    monkeypatch.setattr(models, "_hash", counted_hash)
    assert store.runtime_status()["available"]
    assert store.runtime_status()["available"]
    assert hashes == []
    store._command()
    assert len(hashes) == 1


def test_runtime_status_never_walks_component_directories(tmp_path, monkeypatch):
    import agent.rag.models as models
    base = tmp_path / 'app'
    runtime = base / 'rag-runtime'
    runtime.mkdir(parents=True)
    (runtime / 'rag-worker.exe').write_bytes(b'worker')
    (runtime / 'runtime.json').write_text(json.dumps({'schema_version': 1, 'protocol_version': 1,
        'executable': 'rag-worker.exe', 'files': {'rag-worker.exe': hashlib.sha256(b'worker').hexdigest()}}))
    monkeypatch.setattr(models.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(models.sys, 'executable', str(base / 'agent-core.exe'))
    def forbidden_walk(*args, **kwargs):
        raise AssertionError('Page status must not scan the runtime directory')
    monkeypatch.setattr(models.os, 'walk', forbidden_walk)
    status = ModelStore(tmp_path / 'data').runtime_status()
    assert status['available']
    assert status['integrity_check'] == 'on_load'

def test_cpu_worker_is_reused_only_for_the_same_model_identity(tmp_path, monkeypatch):
    import sys
    script = tmp_path / 'worker.py'
    script.write_text("import sys,json,os\nfor line in sys.stdin:\n r=json.loads(line); print(json.dumps({'id':r['id'],'result':{'available':True,'pid':os.getpid()}}),flush=True)\n")
    store = ModelStore(tmp_path / 'data')
    first_source = model_directory(tmp_path / 'first')
    second_source = model_directory(tmp_path / 'second')
    (second_source / 'tokenizer_config.json').write_text('{"name":"second"}')
    first = asyncio.run(store.import_model(first_source, 'bge-m3'))
    second = asyncio.run(store.import_model(second_source, 'bge-m3'))
    monkeypatch.setattr(store, '_command', lambda: [sys.executable, str(script)])

    async def exercise():
        try:
            first_probe = await store.probe(first['id'])
            old_worker = store._workers['cpu']
            old_process = old_worker.process
            assert (await store.probe(first['id']))['pid'] == first_probe['pid']
            second_probe = await store.probe(second['id'])
            assert second_probe['available']
            assert old_process.returncode is not None
            assert store._workers['cpu'] is not old_worker
            assert second_probe['pid'] != first_probe['pid']
            third_probe = await store.probe(first['id'])
            assert third_probe['pid'] != second_probe['pid']
        finally:
            await store.close()
    asyncio.run(exercise())

def test_shutdown_during_model_switch_cannot_start_a_replacement_worker(tmp_path, monkeypatch):
    import sys
    from agent.rag.worker_client import WorkerClient
    script = tmp_path / 'worker.py'
    script.write_text("import sys,json\nfor line in sys.stdin:\n r=json.loads(line);print(json.dumps({'id':r['id'],'result':{'available':True}}),flush=True)\n")
    store = ModelStore(tmp_path / 'data')
    first_source = model_directory(tmp_path / 'first')
    second_source = model_directory(tmp_path / 'second')
    (second_source / 'tokenizer_config.json').write_text('{"name":"second"}')
    first = asyncio.run(store.import_model(first_source, 'bge-m3'))
    second = asyncio.run(store.import_model(second_source, 'bge-m3'))
    monkeypatch.setattr(store, '_command', lambda: [sys.executable, str(script)])

    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        original = WorkerClient.close
        async def paused_close(worker):
            await original(worker)
            entered.set()
            await release.wait()
        try:
            assert (await store.probe(first['id']))['available']
            monkeypatch.setattr(WorkerClient, 'close', paused_close)
            switching = asyncio.create_task(store.probe(second['id']))
            await asyncio.wait_for(entered.wait(), 2)
            await store.close()
            release.set()
            assert not (await switching)['available']
            assert store._workers == {} and store._loaded_models == {}
        finally:
            release.set()
            await store.close()
    asyncio.run(exercise())
