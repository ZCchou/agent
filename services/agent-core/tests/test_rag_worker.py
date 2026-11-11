import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from agent.rag.worker_client import WorkerClient
from agent.rag.worker import dispatch, last_token_pool
from agent.storage import CoreDataError


def test_unknown_worker_action_rejected():
    with pytest.raises(ValueError, match="action"):
        dispatch({"action": "execute", "code": "print(1)"}, {})


def test_onnx_cuda_preloads_dependencies_before_provider_discovery(monkeypatch):
    from types import SimpleNamespace
    from agent.rag.worker import _engine
    calls = []
    monkeypatch.setattr("agent.rag.worker.importlib.util.find_spec", lambda name: object())
    ort = SimpleNamespace(
        preload_dlls=lambda **kwargs: calls.append(("preload", kwargs)),
        get_available_providers=lambda: calls.append(("providers", {})) or ["CUDAExecutionProvider", "CPUExecutionProvider"],
    )
    monkeypatch.setitem(sys.modules, "onnxruntime", ort)
    assert _engine("onnx", "cuda")["provider"] == "CUDAExecutionProvider"
    assert calls == [("preload", {"cuda": True, "cudnn": True, "msvc": True}), ("providers", {})]


def test_onnx_session_rejects_cuda_initialization_fallback(monkeypatch, tmp_path):
    from types import SimpleNamespace
    import agent.rag.worker as worker
    class Session:
        def get_providers(self):
            return ["CPUExecutionProvider"]
        def disable_fallback(self):
            pass
    monkeypatch.setitem(sys.modules, "onnxruntime", SimpleNamespace(InferenceSession=lambda *args, **kwargs: Session()))
    monkeypatch.setattr(worker, "_engine", lambda *args: {})
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(
        AutoTokenizer=SimpleNamespace(from_pretrained=lambda *args, **kwargs: object())))
    (tmp_path / "rag-model.json").write_text(json.dumps({"graph": "model.onnx"}))
    with pytest.raises(RuntimeError, match="CUDAExecutionProvider"):
        worker._load({"path": str(tmp_path), "profile": "bge-m3", "backend": "onnx", "device": "cuda"}, {})


def test_worker_client_exchanges_json_lines_and_closes(tmp_path):
    script = tmp_path / "worker.py"
    script.write_text("import sys,json\nfor line in sys.stdin:\n r=json.loads(line);print(json.dumps({'id':r['id'],'result':{'offline':True}}),flush=True)\n")
    async def exercise():
        client = WorkerClient([sys.executable, str(script)], timeout=2)
        assert await client.request("probe", {}) == {"offline": True}
        await client.close()
        assert client.process is None
    asyncio.run(exercise())


def test_worker_memory_failure_has_resource_code_and_stops_process(tmp_path):
    script = tmp_path / 'memory.py'
    script.write_text("import sys,json\nfor line in sys.stdin:\n r=json.loads(line);print(json.dumps({'id':r['id'],'error':'RuntimeException: BFCArena::AllocateRawInternal Failed to allocate memory for requested buffer of size 281129472'}),flush=True)\n")
    async def exercise():
        client = WorkerClient([sys.executable, str(script)])
        try:
            with pytest.raises(CoreDataError) as error:
                await client.request('embed', {'device': 'cuda', 'texts': ['a', 'b']})
            assert error.value.code == 'rag_resource_exhausted'
            from agent.execution.run_recovery import safe_code, failure_notice
            assert safe_code(error.value) == 'rag_resource_exhausted'
            assert 'internal_error' not in failure_notice(safe_code(error.value), [], terminal=True)
            assert client.process is None
        finally:
            await client.close()
    asyncio.run(exercise())


def test_worker_environment_isolates_native_and_python_search_paths(tmp_path, monkeypatch):
    script = tmp_path / "environment.py"
    script.write_text("import sys,json,os\nfor line in sys.stdin:\n r=json.loads(line);print(json.dumps({'id':r['id'],'result':{k:os.environ.get(k) for k in ['PATH','PYTHONPATH','PYTHONHOME','CUDA_PATH']}}),flush=True)\n")
    monkeypatch.setenv("PATH", os.environ.get("PATH", "") + os.pathsep + "C:/untrusted-dlls")
    monkeypatch.setenv("PYTHONPATH", "C:/untrusted-python")
    monkeypatch.setenv("CUDA_PATH", "C:/untrusted-cuda")
    async def exercise():
        client = WorkerClient([sys.executable, str(script)])
        try:
            result = await client.request("probe", {})
            assert "untrusted" not in json.dumps(result)
            assert result["CUDA_PATH"] is None and result["PYTHONHOME"] is None
        finally:
            await client.close()
    asyncio.run(exercise())


def test_worker_timeout_kills_and_next_request_restarts(tmp_path):
    script = tmp_path / "worker.py"
    script.write_text("import sys,time\nfor line in sys.stdin: time.sleep(10)\n")
    async def exercise():
        client = WorkerClient([sys.executable, str(script)], timeout=0.1)
        with pytest.raises(CoreDataError, match="timed out"):
            await client.request("probe", {})
        assert client.process is None
        await client.close()
    asyncio.run(exercise())


def test_worker_cancellation_kills_process(tmp_path):
    script = tmp_path / "worker.py"
    script.write_text("import sys,time\nfor line in sys.stdin: time.sleep(10)\n")
    async def exercise():
        client = WorkerClient([sys.executable, str(script)], timeout=20)
        task = asyncio.create_task(client.request("probe", {}))
        await asyncio.sleep(0.15)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client.process is None
        await client.close()
    asyncio.run(exercise())


def test_worker_serializes_overlapping_requests(tmp_path):
    script = tmp_path / "worker.py"
    script.write_text("import sys,json,time\nfor line in sys.stdin:\n r=json.loads(line);time.sleep(0.03);print(json.dumps({'id':r['id'],'result':r['params']['value']}),flush=True)\n")
    async def exercise():
        client = WorkerClient([sys.executable, str(script)], timeout=2)
        assert await asyncio.gather(*(client.request("probe", {"value": value}) for value in range(5))) == list(range(5))
        await client.close()
    asyncio.run(exercise())


def test_worker_restarts_after_timeout(tmp_path):
    script = tmp_path / "worker.py"
    script.write_text("import sys,json,time\nfor line in sys.stdin:\n r=json.loads(line)\n if r['params'].get('sleep'): time.sleep(10)\n print(json.dumps({'id':r['id'],'result':'restarted'}),flush=True)\n")
    async def exercise():
        client = WorkerClient([sys.executable, str(script)], timeout=0.3)
        with pytest.raises(CoreDataError, match="timed out"):
            await client.request("probe", {"sleep": True})
        client.timeout = 5
        assert await client.request("probe", {}) == "restarted"
        await client.close()
    asyncio.run(exercise())


def test_qwen_last_token_pool_matches_padding_reference():
    import torch
    hidden = torch.arange(24, dtype=torch.float32).reshape(2, 3, 4)
    right = torch.tensor([[1, 1, 0], [1, 1, 1]])
    assert torch.equal(last_token_pool(hidden, right), torch.stack([hidden[0, 1], hidden[1, 2]]))
    left = torch.tensor([[0, 1, 1], [1, 1, 1]])
    assert torch.equal(last_token_pool(hidden, left), hidden[:, -1])


def native_model(root, profile):
    import torch
    from tokenizers import Tokenizer
    from tokenizers.models import WordLevel
    from tokenizers.pre_tokenizers import Whitespace
    from transformers import PreTrainedTokenizerFast, Qwen3Config, Qwen3Model, Qwen3ForCausalLM, XLMRobertaConfig, XLMRobertaModel, XLMRobertaForSequenceClassification
    root.mkdir()
    vocabulary = {"[PAD]": 0, "[UNK]": 1, "[EOS]": 2, "yes": 3, "no": 4, "query": 5, "document": 6, "relevant": 7}
    backend = Tokenizer(WordLevel(vocabulary, unk_token="[UNK]"))
    backend.pre_tokenizer = Whitespace()
    tokenizer = PreTrainedTokenizerFast(tokenizer_object=backend, unk_token="[UNK]", eos_token="[EOS]", pad_token="[PAD]")
    tokenizer.save_pretrained(root)
    torch.manual_seed(7)
    if profile.startswith("qwen"):
        config = Qwen3Config(vocab_size=8, hidden_size=16, intermediate_size=24, num_hidden_layers=1, num_attention_heads=4, num_key_value_heads=2, head_dim=4, pad_token_id=0)
        model = Qwen3ForCausalLM(config) if "reranker" in profile else Qwen3Model(config)
    else:
        config = XLMRobertaConfig(vocab_size=8, hidden_size=16, intermediate_size=24, num_hidden_layers=1, num_attention_heads=4, pad_token_id=0, num_labels=1)
        model = XLMRobertaForSequenceClassification(config) if "reranker" in profile else XLMRobertaModel(config)
    model.eval().save_pretrained(root, safe_serialization=True)
    return tokenizer, model


@pytest.mark.parametrize("profile,official_config", [
    ("qwen3-embedding-0.6b", False), ("qwen3-embedding-0.6b", True),
    ("qwen3-reranker-0.6b", False), ("bge-m3", False), ("bge-reranker-v2-m3", False),
])
def test_native_tiny_models_follow_reference_pooling_and_scoring(tmp_path, profile, official_config):
    import torch
    from agent.rag.model_profiles import QUERY_INSTRUCTION, RERANK_PREFIX, RERANK_SUFFIX
    path = tmp_path / "native"
    tokenizer, model = native_model(path, profile)
    if official_config:
        config_path = path / "config.json"
        config = json.loads(config_path.read_text())
        config["architectures"] = ["Qwen3ForCausalLM"]
        config_path.write_text(json.dumps(config))
    params = {"path": str(path), "profile": profile, "backend": "transformers", "device": "cpu", "texts": ["document relevant", "document"], "query": "query"}
    action = "rerank" if "reranker" in profile else "embed"
    actual = dispatch({"action": action, "params": params}, {})
    if profile.startswith("qwen3"):
        tokenizer.padding_side = "left"
    with torch.inference_mode():
        if profile == "qwen3-reranker-0.6b":
            prompts = [RERANK_PREFIX + f"<Instruct>: {QUERY_INSTRUCTION}\n<Query>: query\n<Document>: {text}" + RERANK_SUFFIX for text in params["texts"]]
            inputs = tokenizer(prompts, padding=True, return_tensors="pt", add_special_tokens=False)
            expected = model(**inputs).logits[:, -1, [4, 3]].float().softmax(dim=-1)[:, 1]
        elif action == "rerank":
            inputs = tokenizer(["query"] * 2, params["texts"], padding=True, return_tensors="pt")
            expected = model(**inputs).logits.float().reshape(-1).sigmoid()
        else:
            texts = params["texts"]
            if profile.startswith("qwen3"):
                texts = [f"Instruct: {QUERY_INSTRUCTION}\nQuery: {text}" for text in texts]
            inputs = tokenizer(texts, padding=True, return_tensors="pt")
            hidden = model(**inputs).last_hidden_state
            expected = hidden[:, -1] if profile.startswith("qwen3") else hidden[:, 0]
            expected = torch.nn.functional.normalize(expected.float(), p=2, dim=1)
    assert torch.allclose(torch.tensor(actual), expected, atol=1e-6)
    if official_config:
        params["query"] = False
        actual_documents = dispatch({"action": "embed", "params": params}, {})
        inputs = tokenizer(params["texts"], padding=True, return_tensors="pt")
        with torch.inference_mode():
            expected_documents = torch.nn.functional.normalize(model(**inputs).last_hidden_state[:, -1].float(), p=2, dim=1)
        assert torch.allclose(torch.tensor(actual_documents), expected_documents, atol=1e-6)
        with pytest.raises(ValueError, match="role"):
            dispatch({"action": "rerank", "params": params}, {})


def test_incomplete_native_weights_fail_instead_of_random_initialization(tmp_path):
    import torch
    from safetensors.torch import save_file
    path = tmp_path / "incomplete"
    native_model(path, "qwen3-embedding-0.6b")
    save_file({"embed_tokens.weight": torch.zeros(8, 16)}, path / "model.safetensors")
    params = {"path": str(path), "profile": "qwen3-embedding-0.6b", "backend": "transformers", "device": "cpu"}
    with pytest.raises(ValueError, match="missing|incomplete"):
        dispatch({"action": "probe", "params": params}, {})



def test_worker_client_uses_ascii_json_frames_for_frozen_windows_stdio(tmp_path):
    script = tmp_path / "ascii.py"
    script.write_text("import sys,json\nfor line in sys.stdin:\n r=json.loads(line);print(json.dumps({'id':r['id'],'result':line.isascii()}),flush=True)\n")
    async def exercise():
        client = WorkerClient([sys.executable, str(script)])
        try:
            assert await client.request("probe", {"path": "适航审定/模型"}) is True
        finally:
            await client.close()
    asyncio.run(exercise())


def test_worker_response_uses_ascii_json_frames(monkeypatch, capsys):
    import io
    import agent.rag.worker as worker
    monkeypatch.setattr(worker.sys, "stdin", io.StringIO('{"id":1}\n'))
    monkeypatch.setattr(worker, "dispatch", lambda request, cache: {"text": "适航审定"})
    worker.main()
    output = capsys.readouterr().out
    assert output.isascii()
    assert json.loads(output)["result"]["text"] == "适航审定"

def test_qwen_reranker_computes_only_the_last_position_logits(tmp_path, monkeypatch):
    path = tmp_path / 'native'
    profile = 'qwen3-reranker-0.6b'
    tokenizer, model = native_model(path, profile)
    original = model.forward
    calls = []
    def last_position_forward(*args, **kwargs):
        calls.append(kwargs.get('logits_to_keep'))
        assert kwargs.get('logits_to_keep') == 1
        return original(*args, **kwargs)
    monkeypatch.setattr(model, 'forward', last_position_forward)
    params = {'path': str(path), 'profile': profile, 'backend': 'transformers',
              'device': 'cpu', 'texts': ['document relevant', 'document'], 'query': 'query'}
    cache = {'key': (str(path), profile, 'transformers', 'cpu', None),
             'loaded': (tokenizer, model, None)}
    scores = dispatch({'action': 'rerank', 'params': params}, cache)
    assert calls == [1]
    assert len(scores) == 2 and all(0 <= score <= 1 for score in scores)
