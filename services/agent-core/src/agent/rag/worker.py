"""Offline inference subprocess. stdout is exclusively framed JSONL responses."""
from __future__ import annotations

import contextlib
import importlib.util
import json
import math
import os
import sys
from pathlib import Path

from agent.rag.model_profiles import PROFILES, QUERY_INSTRUCTION, RERANK_PREFIX, RERANK_SUFFIX


def last_token_pool(hidden, attention_mask):
    """Qwen reference pooling supports left and right padding."""
    import torch
    if attention_mask[:, -1].sum() == attention_mask.shape[0]:
        return hidden[:, -1]
    lengths = attention_mask.sum(dim=1) - 1
    return hidden[torch.arange(hidden.shape[0], device=hidden.device), lengths]


def _engine(backend, device):
    if device != "cpu" and device != "cuda":
        raise ValueError("Device must be cpu or cuda")
    libraries = ("torch", "transformers", "safetensors") if backend == "transformers" else ("onnxruntime", "transformers", "onnx")
    missing = [name for name in libraries if importlib.util.find_spec(name) is None]
    if missing:
        raise RuntimeError("Inference dependencies unavailable: " + ", ".join(missing))
    if backend == "transformers":
        import torch
        if device == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("CUDA is unavailable in the installed inference runtime")
        return {"engine": "transformers", "device": device, "cuda": torch.cuda.is_available()}
    import onnxruntime as ort
    provider = "CUDAExecutionProvider" if device == "cuda" else "CPUExecutionProvider"
    if provider not in ort.get_available_providers():
        raise RuntimeError(f"Requested provider {provider} is unavailable")
    return {"engine": "onnxruntime", "device": device, "provider": provider}


def _load(params, cache):
    key = (params["path"], params["profile"], params["backend"], params["device"], params.get("digest"))
    if cache.get("key") == key:
        return cache["loaded"]
    cache.clear()
    profile = PROFILES[params["profile"]]
    _engine(params["backend"], params["device"])
    from transformers import AutoTokenizer
    path = Path(params["path"])
    tokenizer = AutoTokenizer.from_pretrained(str(path), local_files_only=True, trust_remote_code=False)
    if params["profile"].startswith("qwen3"):
        tokenizer.padding_side = "left"
        if tokenizer.pad_token_id is None:
            tokenizer.pad_token = tokenizer.eos_token
    if params["backend"] == "onnx":
        import onnxruntime as ort
        metadata = json.loads((path / "rag-model.json").read_text(encoding="utf-8"))
        provider = "CUDAExecutionProvider" if params["device"] == "cuda" else "CPUExecutionProvider"
        model = ort.InferenceSession(str(path / metadata["graph"]), providers=[provider])
        if provider not in model.get_providers():
            raise RuntimeError(f"Requested ONNX provider {provider} failed to initialize")
        loaded = (tokenizer, model, metadata)
    else:
        from transformers import AutoModel, AutoModelForCausalLM, AutoModelForSequenceClassification
        factory = AutoModel
        if profile.pooling == "yes_no":
            factory = AutoModelForCausalLM
        elif profile.role == "reranker":
            factory = AutoModelForSequenceClassification
        model, loading = factory.from_pretrained(str(path), local_files_only=True, trust_remote_code=False, use_safetensors=True, output_loading_info=True)
        missing = loading.get("missing_keys", [])
        if params["profile"] == "bge-m3":
            # Dense CLS embeddings never consume the optional pooler projection.
            missing = [name for name in missing if not name.startswith("pooler.")]
        if missing or loading.get("mismatched_keys") or loading.get("error_msgs"):
            raise ValueError("Model checkpoint is incomplete or mismatched; missing keys: " + ", ".join(missing[:10]))
        model.to(params["device"]).eval()
        loaded = (tokenizer, model, None)
    cache.update(key=key, loaded=loaded)
    return loaded


def _torch_batch(params, tokenizer, model, texts):
    import torch
    import torch.nn.functional as functional
    profile = PROFILES[params["profile"]]
    if profile.pooling == "yes_no":
        prefix = tokenizer.encode(RERANK_PREFIX, add_special_tokens=False)
        suffix = tokenizer.encode(RERANK_SUFFIX, add_special_tokens=False)
        inputs = tokenizer(texts, padding=False, truncation=True, max_length=profile.max_length - len(prefix) - len(suffix), add_special_tokens=False)
        inputs["input_ids"] = [prefix + row + suffix for row in inputs["input_ids"]]
        inputs.pop("attention_mask", None)
        inputs = tokenizer.pad(inputs, padding=True, return_tensors="pt")
    else:
        inputs = tokenizer(texts, padding=True, truncation=True, max_length=profile.max_length, return_tensors="pt")
    inputs = {name: tensor.to(params["device"]) for name, tensor in inputs.items()}
    with torch.inference_mode():
        outputs = model(**inputs)
        if profile.pooling == "yes_no":
            yes = tokenizer.convert_tokens_to_ids("yes")
            no = tokenizer.convert_tokens_to_ids("no")
            if yes == tokenizer.unk_token_id or no == tokenizer.unk_token_id:
                raise ValueError("Reranker tokenizer lacks yes/no tokens")
            result = outputs.logits[:, -1, [no, yes]].float().softmax(dim=-1)[:, 1]
        elif profile.role == "reranker":
            result = outputs.logits.float().reshape(-1).sigmoid()
        else:
            hidden = outputs.last_hidden_state
            result = last_token_pool(hidden, inputs["attention_mask"]) if profile.pooling == "last" else hidden[:, 0]
            result = functional.normalize(result.float(), p=2, dim=1)
    return result.cpu().tolist()


def _onnx_batch(params, tokenizer, model, metadata, texts):
    import numpy as np
    profile = PROFILES[params["profile"]]
    if profile.role == "reranker":
        inputs = tokenizer([params["query"]] * len(texts), texts, padding=True, truncation=True, max_length=metadata["max_length"], return_tensors="np")
    else:
        inputs = tokenizer(texts, padding=True, truncation=True, max_length=metadata["max_length"], return_tensors="np")
    feed = {}
    for item in model.get_inputs():
        value = inputs.get(item.name)
        if value is None and item.name == "token_type_ids":
            value = np.zeros_like(inputs["input_ids"])
        if value is None:
            raise ValueError(f"Unsupported ONNX input {item.name}")
        dtype = np.int64 if item.type == "tensor(int64)" else np.int32
        feed[item.name] = value.astype(dtype)
    value = model.run([metadata["output"]], feed)[0].astype(np.float32)
    if profile.role == "reranker":
        value = value.reshape(-1)
        return (1 / (1 + np.exp(-np.clip(value, -80, 80)))).tolist()
    if metadata["pooling"] == "cls":
        value = value[:, 0]
    value = value / np.maximum(np.linalg.norm(value, axis=1, keepdims=True), 1e-12)
    return value.tolist()


def dispatch(request, cache):
    action = request.get("action")
    if action not in {"probe", "embed", "rerank"}:
        raise ValueError("Unsupported worker action")
    params = request["params"]
    if params.get("profile") not in PROFILES:
        raise ValueError("Unsupported model profile")
    if params.get("backend") not in {"transformers", "onnx"}:
        raise ValueError("Unsupported inference backend")
    availability = _engine(params["backend"], params["device"])
    tokenizer, model, metadata = _load(params, cache)
    if action == "probe":
        return {"available": True, **availability}
    profile = PROFILES[params["profile"]]
    if (action == "embed") != (profile.role == "embedding"):
        raise ValueError("Requested action does not match model role")
    texts = params["texts"]
    if not isinstance(texts, list) or any(not isinstance(text, str) for text in texts):
        raise ValueError("texts must be a list of strings")
    if params["profile"] == "qwen3-embedding-0.6b" and params.get("query"):
        texts = [f"Instruct: {QUERY_INSTRUCTION}\nQuery: {text}" for text in texts]
    if params["profile"] == "qwen3-reranker-0.6b":
        texts = [f"<Instruct>: {QUERY_INSTRUCTION}\n<Query>: {params['query']}\n<Document>: {text}" for text in texts]
    results = []
    for start in range(0, len(texts), 8):
        batch = texts[start:start + 8]
        if params["backend"] == "onnx":
            result = _onnx_batch(params, tokenizer, model, metadata, batch)
        elif profile.role == "reranker" and profile.pooling != "yes_no":
            import torch
            inputs = tokenizer([params["query"]] * len(batch), batch, padding=True, truncation=True, max_length=profile.max_length, return_tensors="pt")
            inputs = {name: tensor.to(params["device"]) for name, tensor in inputs.items()}
            with torch.inference_mode():
                result = model(**inputs).logits.float().reshape(-1).sigmoid().cpu().tolist()
        else:
            result = _torch_batch(params, tokenizer, model, batch)
        results.extend(result)
    return results


def main():
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_DATASETS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1")
    cache = {}
    for line in sys.stdin:
        request_id = None
        try:
            request = json.loads(line)
            request_id = request.get("id")
            with contextlib.redirect_stdout(sys.stderr):
                result = dispatch(request, cache)
            response = {"id": request_id, "result": result}
        except Exception as exc:
            response = {"id": request_id, "error": f"{type(exc).__name__}: {exc}"}
        sys.stdout.write(json.dumps(response, ensure_ascii=True, allow_nan=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
