"""Bounded GGUF v2/v3 inspection. No tensor payload is materialized."""
from __future__ import annotations

import hashlib
import math
import re
import struct
from pathlib import Path

from agent.storage import CoreDataError
from .paths import checked_path

# GGML type -> (elements per block, encoded bytes per block), from llama.cpp
# cd959bae99ff4bed22bef2604f45241c09bbb8c8 ggml.h, ggml.c and ggml-common.h.
# Removed/repacked-only type IDs deliberately have no on-disk representation.
_TYPES = {0: (1, 4), 1: (1, 2), 2: (32, 18), 3: (32, 20),
          6: (32, 22), 7: (32, 24), 8: (32, 34), 9: (32, 36),
          10: (256, 84), 11: (256, 110), 12: (256, 144),
          13: (256, 176), 14: (256, 210), 15: (256, 292),
          16: (256, 66), 17: (256, 74), 18: (256, 98), 19: (256, 50),
          20: (32, 18), 21: (256, 110), 22: (256, 82), 23: (256, 136),
          24: (1, 1), 25: (1, 2), 26: (1, 4), 27: (1, 8), 28: (1, 8),
          29: (256, 56), 30: (1, 2), 34: (256, 54), 35: (256, 66),
          39: (32, 17), 40: (64, 36), 41: (128, 18), 42: (64, 18), 43: (1, 1)}
_SCALARS = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i",
            6: "f", 7: "?", 10: "Q", 11: "q", 12: "d"}
_QUANTIZATIONS = {0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 7: "Q8_0",
                  8: "Q5_0", 9: "Q5_1", 10: "Q2_K", 11: "Q3_K_S",
                  12: "Q3_K_M", 13: "Q3_K_L", 14: "Q4_K_S", 15: "Q4_K_M",
                  16: "Q5_K_S", 17: "Q5_K_M", 18: "Q6_K", 32: "BF16"}
_SPLIT = re.compile(r"(.+)-(\d{5})-of-(\d{5})\.gguf", re.IGNORECASE)
_MAX_HEADER = 128 * 1024 * 1024
_MAX_MODEL_TENSORS = 100_000
_MAX_TENSOR_NAME_BYTES = 16 * 1024 * 1024


class _TensorBudget:
    def __init__(self):
        self.count = 0
        self.name_bytes = 0

    def retain(self, name):
        self.count += 1
        self.name_bytes += len(name.encode("utf-8"))
        if self.count > _MAX_MODEL_TENSORS or self.name_bytes > _MAX_TENSOR_NAME_BYTES:
            raise ValueError("The complete GGUF model exceeds its tensor or retained-name budget.")


class _Reader:
    def __init__(self, stream, size, cancel):
        self.stream = stream
        self.size = size
        self.elements = 0
        self.cancel = cancel

    def read(self, length):
        self.cancel()
        if length < 0 or self.stream.tell() + length > min(self.size, _MAX_HEADER):
            raise ValueError("GGUF metadata is truncated or exceeds its bounded header.")
        value = self.stream.read(length)
        if len(value) != length:
            raise ValueError("Truncated GGUF metadata.")
        return value

    def number(self, format):
        return struct.unpack("<" + format, self.read(struct.calcsize("<" + format)))[0]

    def string(self, limit=16 * 1024 * 1024):
        length = self.number("Q")
        if length > limit:
            raise ValueError("GGUF string exceeds its size limit.")
        return self.read(length).decode("utf-8")

    def value(self, kind, retain=True):
        if kind in _SCALARS:
            value = self.number(_SCALARS[kind])
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("Non-finite GGUF metadata value.")
            return value
        if kind == 8:
            return self.string()
        if kind == 9:
            subtype, count = self.number("I"), self.number("Q")
            self.elements += count
            if subtype == 9 or subtype not in {*_SCALARS, 8} or self.elements > 2_000_000:
                raise ValueError("Invalid or oversized GGUF metadata array.")
            if retain:
                if count > 65536:
                    raise ValueError("GGUF layer metadata array exceeds its size limit.")
                return [self.value(subtype, retain=False) for _ in range(count)]
            # Token vocabularies can be large. Validate each entry without keeping it.
            for _ in range(count):
                self.value(subtype, retain=False)
            return None
        raise ValueError("Unsupported GGUF metadata type.")


def _read_shard(path, *, budget, cancel):
    path = checked_path(path, file=True)
    size = path.stat().st_size
    with path.open("rb") as stream:
        reader = _Reader(stream, size, cancel)
        if reader.read(4) != b"GGUF" or reader.number("I") not in (2, 3):
            raise ValueError("Only GGUF v2 and v3 are supported.")
        tensors, count = reader.number("Q"), reader.number("Q")
        if not 1 <= tensors <= 100_000 or not 1 <= count <= 65_536:
            raise ValueError("Invalid or oversized GGUF metadata/tensor count.")
        metadata = {}
        keys = set()
        for _ in range(count):
            key = reader.string(1024)
            if not key or key in keys:
                raise ValueError("Duplicate or empty GGUF metadata key.")
            keys.add(key)
            value = reader.value(reader.number("I"), retain=key.endswith(".attention.recurrent_layers"))
            if key.startswith(("general.", "split.")) or key.endswith((".context_length", ".chat_template",
                    ".block_count", ".embedding_length", ".attention.head_count", ".attention.head_count_kv",
                    ".attention.key_length", ".attention.value_length", ".attention.recurrent_layers",
                    ".full_attention_interval", ".ssm.conv_kernel", ".ssm.inner_size",
                    ".ssm.state_size", ".ssm.group_count", ".nextn_predict_layers")):
                metadata[key] = value
        if metadata.get("split.count", 1) != 1:
            total = metadata.get("split.tensors.count")
            if type(total) is not int or not 1 <= total <= _MAX_MODEL_TENSORS:
                raise ValueError("Invalid or oversized complete GGUF split tensor count.")
        alignment = metadata.get("general.alignment", 32)
        if type(alignment) is not int or alignment < 1 or alignment > 4096 or alignment & (alignment - 1):
            raise ValueError("Invalid GGUF alignment.")
        ranges, names = [], set()
        for _ in range(tensors):
            name = reader.string(1024)
            dimensions = reader.number("I")
            if not name or name in names or not 1 <= dimensions <= 4:
                raise ValueError("Invalid GGUF tensor name or dimension count.")
            budget.retain(name)
            names.add(name)
            shape = [reader.number("Q") for _ in range(dimensions)]
            kind, offset = reader.number("I"), reader.number("Q")
            if kind not in _TYPES or any(not 1 <= part <= 2**40 for part in shape):
                raise ValueError("Unsupported GGUF tensor type or shape.")
            block, width = _TYPES[kind]
            if shape[0] % block or offset % alignment:
                raise ValueError("Misaligned GGUF tensor block or offset.")
            length = math.prod(shape) // block * width
            ranges.append((offset, offset + length))
        start = (stream.tell() + alignment - 1) // alignment * alignment
        previous = 0
        for begin, end in sorted(ranges):
            if begin < previous or end > size - start:
                raise ValueError("GGUF tensor overlaps or exceeds the file boundary.")
            previous = end
    return metadata, names, size


def _memory_estimate(metadata, architecture):
    """Default FP16 attention KV and FP32 recurrent state for one server slot."""
    prefix = architecture + "."
    layers = metadata.get(prefix + "block_count")
    full_layers = layers if type(layers) is int and 1 <= layers <= 65536 else None
    recurrent_layers, recurrent_bytes = 0, 0
    if architecture == "qwen35":
        mtp_layers = metadata.get(prefix + "nextn_predict_layers", 0)
        if type(mtp_layers) is not int or mtp_layers < 0 or (full_layers is not None and mtp_layers > layers):
            raise ValueError("Invalid GGUF next-token prediction layer count.")
        trunk_layers = layers - mtp_layers if full_layers is not None else None
        layout = metadata.get(prefix + "attention.recurrent_layers")
        interval = metadata.get(prefix + "full_attention_interval", 4)
        if layout is None:
            if type(interval) is not int or not 1 <= interval <= 65536:
                raise ValueError("Invalid GGUF full attention interval.")
            full_layers = trunk_layers // interval if trunk_layers is not None else None
        elif full_layers is not None:
            layout = layout if isinstance(layout, list) else [layout] * full_layers
            if len(layout) != full_layers or any(type(value) not in (bool, int) or value not in (0, 1) for value in layout):
                raise ValueError("Invalid GGUF recurrent layer layout.")
            full_layers = trunk_layers - sum(layout[:trunk_layers])
        recurrent_layers = trunk_layers - full_layers if full_layers is not None else None
        recurrent_bytes = None
        conv, inner, state, groups = (metadata.get(prefix + "ssm." + name)
                                     for name in ("conv_kernel", "inner_size", "state_size", "group_count"))
        if recurrent_layers == 0:
            recurrent_bytes = 0
        elif recurrent_layers is not None and all(type(value) is int and 1 <= value <= 65536
                                                  for value in (conv, inner, state, groups)):
            # llama-hparams.cpp n_embd_r()/n_embd_s(), llama-model.cpp F32
            # hybrid cache, --parallel 1 and default n_rs_seq=0.
            recurrent_bytes = 4 * recurrent_layers * ((conv - 1) * (inner + 2 * groups * state) + state * inner)
    embedding = metadata.get(prefix + "embedding_length")
    heads = metadata.get(prefix + "attention.head_count")
    kv_heads = metadata.get(prefix + "attention.head_count_kv", heads)
    kv_bytes = None
    if full_layers == 0:
        kv_bytes = 0
    elif full_layers is not None and all(type(value) is int and 1 <= value <= 65536
                                         for value in (embedding, heads, kv_heads)):
        key = metadata.get(prefix + "attention.key_length", embedding // heads)
        value = metadata.get(prefix + "attention.value_length", embedding // heads)
        if all(type(length) is int and 1 <= length <= 65536 for length in (key, value)):
            kv_bytes = 2 * full_layers * kv_heads * (key + value)
    unknowns = [name for name, value in (("kv_bytes_per_token", kv_bytes),
                ("recurrent_state_bytes", recurrent_bytes)) if value is None]
    return {"kv_bytes_per_token": kv_bytes, "recurrent_state_bytes": recurrent_bytes,
            "full_attention_layers": full_layers, "recurrent_layers": recurrent_layers,
            "memory_estimate_unknowns": unknowns}


def inspect_gguf(path: Path | str, *, cancel=lambda: None) -> dict:
    """Inspect a single file or exact sibling split set, with bounded allocations."""
    try:
        path = checked_path(path, file=True)
        budget = _TensorBudget()
        metadata, names, size = initial = _read_shard(path, budget=budget, cancel=cancel)
        split_count = metadata.get("split.count", 1)
        if type(split_count) is not int or not 1 <= split_count <= 1024:
            raise ValueError("Invalid GGUF split count.")
        files = [path]
        named_split = _SPLIT.fullmatch(path.name)
        if named_split and int(named_split[3]) != split_count:
            raise CoreDataError("inconsistent_shards", "GGUF split naming and metadata disagree.")
        if split_count > 1:
            match = _SPLIT.fullmatch(path.name)
            index = metadata.get("split.no")
            if not match or int(match[3]) != split_count or type(index) is not int or int(match[2]) != index + 1:
                raise CoreDataError("inconsistent_shards", "Split GGUF naming and metadata disagree.")
            files = [path.with_name(f"{match[1]}-{part + 1:05}-of-{split_count:05}.gguf")
                     for part in range(split_count)]
            names, size = set(), 0
            for part, sibling in enumerate(files):
                cancel()
                if not sibling.is_file():
                    raise CoreDataError("incomplete_shards", "A required GGUF sibling shard is missing.")
                other, other_names, other_size = initial if part == index else _read_shard(sibling, budget=budget, cancel=cancel)
                if other.get("split.no") != part or any(other.get(key) != value for key, value in metadata.items()
                                                        if key != "split.no") or names.intersection(other_names):
                    raise CoreDataError("inconsistent_shards", "GGUF sibling metadata or tensor names disagree.")
                names.update(other_names)
                size += other_size
            if metadata.get("split.tensors.count") != len(names):
                raise CoreDataError("inconsistent_shards", "GGUF split tensor count disagrees with its shards.")
        elif metadata.get("split.no", 0) != 0:
            raise ValueError("Invalid single GGUF split index.")
        architecture = metadata.get("general.architecture")
        if architecture not in {"qwen2", "qwen3", "qwen35"}:
            raise CoreDataError("unsupported_architecture", "Supported text architectures are qwen2, qwen3 and qwen35.")
        template = metadata.get("tokenizer.chat_template")
        if not isinstance(template, str) or not template.strip():
            raise CoreDataError("unsupported_template", "The GGUF must contain its actual chat template.")
        declared_context = metadata.get(f"{architecture}.context_length")
        context = declared_context if declared_context is not None else 8192
        if type(context) is not int or not 1 <= context <= 2**24:
            raise ValueError("Invalid model context length.")
        quantization = metadata.get("general.file_type")
        if quantization not in _QUANTIZATIONS:
            raise ValueError("Unsupported GGUF quantization profile.")
        return {"name": metadata.get("general.name") or path.stem, "architecture": architecture,
                "format": "gguf", "role": "generation", "quantization": _QUANTIZATIONS[quantization],
                "context_length": context, "declared_context_length": declared_context, "chat_template": template,
                "template_digest": hashlib.sha256(template.encode("utf-8")).hexdigest(),
                "size_bytes": size, "tensor_count": len(names), "files": [str(item) for item in files],
                **_memory_estimate(metadata, architecture)}
    except CoreDataError:
        raise
    except (OSError, ValueError, OverflowError, struct.error) as exc:
        raise CoreDataError("invalid_gguf", str(exc)) from exc
