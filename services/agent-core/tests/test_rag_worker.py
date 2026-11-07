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


def test_worker_client_exchanges_json_lines_and_closes(tmp_path):
    script = tmp_path / "worker.py"
    script.write_text("import sys,json\nfor line in sys.stdin:\n r=json.loads(line);print(json.dumps({'id':r['id'],'result':{'offline':True}}),flush=True)\n")
    async def exercise():
        client = WorkerClient([sys.executable, str(script)], timeout=2)
        assert await client.request("probe", {}) == {"offline": True}
        await client.close()
        assert client.process is None
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


@pytest.mark.parametrize("profile", ["qwen3-embedding-0.6b", "qwen3-reranker-0.6b", "bge-m3", "bge-reranker-v2-m3"])
def test_native_tiny_models_follow_reference_pooling_and_scoring(tmp_path, profile):
    import torch
    from agent.rag.model_profiles import QUERY_INSTRUCTION, RERANK_PREFIX, RERANK_SUFFIX
    path = tmp_path / "native"
    tokenizer, model = native_model(path, profile)
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
