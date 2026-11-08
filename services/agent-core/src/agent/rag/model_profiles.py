"""Closed set of local inference adapters. Model files cannot select Python code."""
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelProfile:
    role: str
    model_type: str
    dimension: int | None
    pooling: str
    max_length: int
    architectures: tuple[str, ...]


PROFILES = {
    "qwen3-embedding-0.6b": ModelProfile("embedding", "qwen3", 1024, "last", 8192, ("Qwen3Model", "Qwen3ForCausalLM")),
    "qwen3-reranker-0.6b": ModelProfile("reranker", "qwen3", None, "yes_no", 8192, ("Qwen3ForCausalLM",)),
    "bge-m3": ModelProfile("embedding", "xlm-roberta", 1024, "cls", 8192, ("XLMRobertaModel",)),
    "bge-reranker-v2-m3": ModelProfile("reranker", "xlm-roberta", None, "sigmoid", 8192, ("XLMRobertaForSequenceClassification",)),
}
QUERY_INSTRUCTION = "Given a web search query, retrieve relevant passages that answer the query"
RERANK_PREFIX = '<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and the Instruct provided. Note that the answer can only be "yes" or "no".<|im_end|>\n<|im_start|>user\n'
RERANK_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"



def _package_file(root, name, base=None):
    from pathlib import Path
    relative = Path(name)
    if not name or relative.is_absolute() or (base is None and ".." in relative.parts) or ":" in name or "\\" in name:
        raise ValueError(f"Invalid model package path: {name}")
    target = (base or root) / relative
    if not target.resolve().is_relative_to(root.resolve()) or not target.is_file() or target.is_symlink():
        raise ValueError(f"Missing or linked model file: {name}")
    return target


def validate_onnx_package(root, metadata, profile):
    if profile not in {"bge-m3", "bge-reranker-v2-m3"}:
        raise ValueError("ONNX packages support the adapted BGE profiles only")
    expected = PROFILES[profile]
    if metadata.get("schema_version") != 1 or metadata.get("backend") != "onnx" or metadata.get("profile") != profile:
        raise ValueError("ONNX package requires schema_version=1, backend=onnx and matching profile")
    pooling = metadata.get("pooling")
    allowed = {"cls", "normalized_dense"} if expected.role == "embedding" else {"sigmoid"}
    if pooling not in allowed or metadata.get("dimension") != expected.dimension:
        raise ValueError("ONNX pooling/dimension does not match the supported profile")
    maximum = metadata.get("max_length")
    if type(maximum) is not int or not 1 <= maximum <= expected.max_length:
        raise ValueError("Invalid ONNX max_length")
    graph = _package_file(root, metadata.get("graph", ""))
    if graph.suffix != ".onnx" or not isinstance(metadata.get("output"), str):
        raise ValueError("ONNX package requires graph and output")
    import onnx
    model = onnx.load(str(graph), load_external_data=False)
    if any(item.domain not in {"", "ai.onnx", "ai.onnx.ml"} for item in model.opset_import):
        raise ValueError("Custom ONNX operator domains are unsupported")
    inputs = {item.name for item in model.graph.input}
    if not {"input_ids", "attention_mask"}.issubset(inputs) or not inputs.issubset({"input_ids", "attention_mask", "token_type_ids"}):
        raise ValueError("Unsupported ONNX input signature")
    if metadata["output"] not in {item.name for item in model.graph.output}:
        raise ValueError("ONNX output does not exist")
    for tensor in onnx.external_data_helper._get_all_tensors(model):
        if tensor.data_location != onnx.TensorProto.EXTERNAL:
            continue
        info = {entry.key: entry.value for entry in tensor.external_data}
        external = _package_file(root, info.get("location", ""), base=graph.parent)
        offset = int(info.get("offset", 0))
        length = int(info.get("length", external.stat().st_size - offset))
        if offset < 0 or length <= 0 or offset + length > external.stat().st_size:
            raise ValueError("Invalid ONNX external tensor byte range")
    onnx.checker.check_model(str(graph), full_check=False)
