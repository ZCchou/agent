"""Offline inference subprocess. stdout is exclusively framed JSONL responses."""
from __future__ import annotations

import contextlib
import importlib.util
import json
import math
import os
import struct
import sys
from pathlib import Path

from agent.rag.model_profiles import PROFILES, QUERY_INSTRUCTION, RERANK_PREFIX, RERANK_SUFFIX


def _owned_state_dict(root):
    """Read verified tensor ranges into owned buffers, avoiding Windows mmap views."""
    import torch
    from agent.rag.models import _json, _safe_relative, _validate_safetensors
    root = Path(root)
    if (root / "model.safetensors").is_file():
        names = ["model.safetensors"]
    else:
        mapping = _json(root / "model.safetensors.index.json").get("weight_map")
        if not isinstance(mapping, dict) or not mapping or any(not isinstance(name, str) for name in mapping.values()):
            raise ValueError("Invalid safetensors shard index")
        names = sorted(set(mapping.values()))
    dtypes = {"BOOL": torch.bool, "U8": torch.uint8, "I8": torch.int8,
              "I16": torch.int16, "U16": torch.uint16, "I32": torch.int32, "U32": torch.uint32,
              "I64": torch.int64, "U64": torch.uint64, "F16": torch.float16, "BF16": torch.bfloat16,
              "F32": torch.float32, "F64": torch.float64,
              "F8_E4M3": torch.float8_e4m3fn, "F8_E5M2": torch.float8_e5m2}
    state = {}
    for name in names:
        if not name.endswith(".safetensors"):
            raise ValueError("Weight shards must be safetensors files")
        path = _safe_relative(root, name)
        header, header_size = _validate_safetensors(path, include_header=True)
        keys = set(header) - {"__metadata__"}
        with path.open("rb") as source:
            if struct.unpack("<Q", source.read(8))[0] != header_size or json.loads(source.read(header_size)) != header:
                raise ValueError("Weight shard header changed while reading")
            expected_size = 8 + header_size + max(header[key]["data_offsets"][1] for key in keys)
            if os.fstat(source.fileno()).st_size != expected_size:
                raise ValueError("Weight shard size changed while reading")
            for key in sorted(keys):
                if key in state:
                    raise ValueError("Duplicate tensor across weight shards")
                item = header[key]
                begin, end = item["data_offsets"]
                source.seek(8 + header_size + begin)
                buffer = bytearray(end - begin)
                if source.readinto(buffer) != len(buffer):
                    raise ValueError("Weight shard changed or was truncated while reading")
                dtype = dtypes[item["dtype"]]
                state[key] = torch.frombuffer(buffer, dtype=dtype).reshape(item["shape"]) if buffer else torch.empty(item["shape"], dtype=dtype)
    return state


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
    if device == "cuda":
        ort.preload_dlls(cuda=True, cudnn=True, msvc=True)
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
        model.disable_fallback()
        if not model.get_providers() or model.get_providers()[0] != provider:
            raise RuntimeError(f"Requested ONNX provider {provider} failed to initialize")
        loaded = (tokenizer, model, metadata)
    else:
        from transformers import AutoModel, AutoModelForCausalLM, AutoModelForSequenceClassification
        factory = AutoModel
        if profile.pooling == "yes_no":
            factory = AutoModelForCausalLM
        elif profile.role == "reranker":
            factory = AutoModelForSequenceClassification
        checkpoint = str(path)
        loading_options = {"local_files_only": True, "trust_remote_code": False,
                           "use_safetensors": True, "output_loading_info": True}
        if os.name == "nt":
            import torch
            import transformers
            native_classes = {"qwen3-embedding-0.6b": "Qwen3Model", "qwen3-reranker-0.6b": "Qwen3ForCausalLM",
                              "bge-m3": "XLMRobertaModel", "bge-reranker-v2-m3": "XLMRobertaForSequenceClassification"}
            factory = getattr(transformers, native_classes[params["profile"]])
            loading_options.pop("trust_remote_code")
            loading_options.update(config=transformers.AutoConfig.from_pretrained(str(path), local_files_only=True, trust_remote_code=False),
                                   state_dict=_owned_state_dict(path), dtype=torch.float32)
            checkpoint = None
        model, loading = factory.from_pretrained(checkpoint, **loading_options)
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


def _token_limit(params, metadata=None):
    profile = PROFILES[params['profile']]
    ceiling = min(profile.max_length, (metadata or {}).get('max_length', profile.max_length))
    if profile.role != 'reranker':
        return ceiling
    budget = params.get('token_budget', 2048)
    if type(budget) is not int or budget < 64:
        raise ValueError('Reranking token budget must be an integer of at least 64')
    return min(ceiling, budget)


def _torch_batch(params, tokenizer, model, texts):
    import torch
    import torch.nn.functional as functional
    profile = PROFILES[params["profile"]]
    if profile.pooling == "yes_no":
        prefix = tokenizer.encode(RERANK_PREFIX, add_special_tokens=False)
        suffix = tokenizer.encode(RERANK_SUFFIX, add_special_tokens=False)
        inputs = tokenizer(texts, padding=False, truncation=True, max_length=_token_limit(params) - len(prefix) - len(suffix), add_special_tokens=False)
        inputs["input_ids"] = [prefix + row + suffix for row in inputs["input_ids"]]
        inputs.pop("attention_mask", None)
        inputs = tokenizer.pad(inputs, padding=True, return_tensors="pt")
        model_options = {"logits_to_keep": 1}
    else:
        inputs = tokenizer(texts, padding=True, truncation=True, max_length=_token_limit(params), return_tensors="pt")
        model_options = {}
    inputs = {name: tensor.to(params["device"]) for name, tensor in inputs.items()}
    with torch.inference_mode():
        outputs = model(**inputs, **model_options)
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
        inputs = tokenizer([params["query"]] * len(texts), texts, padding=True, truncation=True, max_length=_token_limit(params, metadata), return_tensors="np")
    else:
        inputs = tokenizer(texts, padding=True, truncation=True, max_length=_token_limit(params, metadata), return_tensors="np")
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
    progress = request.get('_progress')
    load_key = (params.get('path'), params['profile'], params['backend'], params['device'], params.get('digest'))
    if progress and cache.get('key') != load_key:
        progress({'stage': 'model_loading', 'completed': 0, 'total': len(params.get('texts', []))})
    tokenizer, model, metadata = _load(params, cache)
    if action == "probe":
        return {"available": True, **availability}
    profile = PROFILES[params["profile"]]
    if (action == "embed") != (profile.role == "embedding"):
        raise ValueError("Requested action does not match model role")
    if progress:
        progress({'stage': 'reranking' if action == 'rerank' else 'embedding',
                  'completed': 0, 'total': len(params.get('texts', []))})
    texts = params["texts"]
    if not isinstance(texts, list) or any(not isinstance(text, str) for text in texts):
        raise ValueError("texts must be a list of strings")
    if params["profile"] == "qwen3-embedding-0.6b" and params.get("query"):
        texts = [f"Instruct: {QUERY_INSTRUCTION}\nQuery: {text}" for text in texts]
    if params["profile"] == "qwen3-reranker-0.6b":
        texts = [f"<Instruct>: {QUERY_INSTRUCTION}\n<Query>: {params['query']}\n<Document>: {text}" for text in texts]
    # Tokenize with the actual model tokenizer before grouping lengths. All
    # scoring truncation applies only to these worker-local inputs.
    lengths = tokenizer(texts, padding=False, truncation=True,
                        max_length=_token_limit(params, metadata))['input_ids'] if texts else []
    ordered = sorted(range(len(texts)), key=lambda index: len(lengths[index]))
    batch_size = 2 if params['backend'] == 'onnx' else 8
    results = [None] * len(texts)
    for start in range(0, len(texts), batch_size):
        indices = ordered[start:start + batch_size]
        batch = [texts[index] for index in indices]
        if params["backend"] == "onnx":
            result = _onnx_batch(params, tokenizer, model, metadata, batch)
        elif profile.role == "reranker" and profile.pooling != "yes_no":
            import torch
            inputs = tokenizer([params["query"]] * len(batch), batch, padding=True, truncation=True, max_length=_token_limit(params), return_tensors="pt")
            inputs = {name: tensor.to(params["device"]) for name, tensor in inputs.items()}
            with torch.inference_mode():
                result = model(**inputs).logits.float().reshape(-1).sigmoid().cpu().tolist()
        else:
            result = _torch_batch(params, tokenizer, model, batch)
        for index, value in zip(indices, result):
            results[index] = value
    return results


def main():
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_DATASETS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1")
    cache = {}
    for line in sys.stdin:
        request_id = None
        try:
            request = json.loads(line)
            request_id = request.get("id")
            if request.get('params', {}).get('report_progress'):
                def emit_progress(event):
                    frame = {'id': request_id, 'progress': event}
                    output.write(json.dumps(frame, ensure_ascii=True, allow_nan=False) + '\n')
                    output.flush()
                output = sys.stdout
                request['_progress'] = emit_progress
            with contextlib.redirect_stdout(sys.stderr):
                result = dispatch(request, cache)
            response = {"id": request_id, "result": result, 'peak_memory_bytes': _peak_memory_bytes()}
        except Exception as exc:
            response = {"id": request_id, "error": f"{type(exc).__name__}: {exc}"}
        sys.stdout.write(json.dumps(response, ensure_ascii=True, allow_nan=False) + "\n")
        sys.stdout.flush()


def _peak_memory_bytes():
    if os.name == 'nt':
        import ctypes
        class Counters(ctypes.Structure):
            _fields_ = [('cb', ctypes.c_uint32), ('faults', ctypes.c_uint32)] + [
                (name, ctypes.c_size_t) for name in ('peak_working_set', 'working_set', 'peak_paged_pool',
                    'paged_pool', 'peak_nonpaged_pool', 'nonpaged_pool', 'pagefile', 'peak_pagefile')]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        query = ctypes.WinDLL('psapi').GetProcessMemoryInfo
        query.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]
        query.restype = ctypes.c_int
        if query(ctypes.c_void_p(-1), ctypes.byref(counters), counters.cb):
            return max(counters.peak_working_set, counters.peak_pagefile)
        return 0
    import resource
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == 'darwin' else peak * 1024


if __name__ == "__main__":
    main()
