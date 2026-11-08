"""Bounded GGUF v2/v3 inspection. No tensor payload is materialized."""
from __future__ import annotations

import hashlib
import math
import re
import struct
from pathlib import Path

from agent.storage import CoreDataError
from .paths import checked_path

# ggml type -> (elements per block, encoded bytes per block).
_TYPES = {0: (1, 4), 1: (1, 2), 2: (32, 18), 3: (32, 20),
          6: (32, 22), 7: (32, 24), 8: (32, 34), 9: (32, 40),
          10: (256, 84), 11: (256, 110), 12: (256, 144),
          13: (256, 176), 14: (256, 210), 15: (256, 292), 30: (1, 2)}
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
            value = reader.value(reader.number("I"))
            if key.startswith(("general.", "split.")) or key.endswith((".context_length", ".chat_template",
                    ".block_count", ".embedding_length", ".attention.head_count", ".attention.head_count_kv",
                    ".attention.key_length", ".attention.value_length")):
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
        if architecture not in {"qwen2", "qwen3"}:
            raise CoreDataError("unsupported_architecture", "Supported text architectures are qwen2 and qwen3.")
        template = metadata.get("tokenizer.chat_template")
        if not isinstance(template, str) or not template.strip():
            raise CoreDataError("unsupported_template", "The GGUF must contain its actual chat template.")
        context = metadata.get(f"{architecture}.context_length", 8192)
        if type(context) is not int or not 1 <= context <= 2**24:
            raise ValueError("Invalid model context length.")
        quantization = metadata.get("general.file_type")
        if quantization not in _QUANTIZATIONS:
            raise ValueError("Unsupported GGUF quantization profile.")
        layers = metadata.get(f"{architecture}.block_count")
        embedding = metadata.get(f"{architecture}.embedding_length")
        heads = metadata.get(f"{architecture}.attention.head_count")
        kv_heads = metadata.get(f"{architecture}.attention.head_count_kv", heads)
        kv_bytes = None
        if all(type(value) is int and 1 <= value <= 65536 for value in (layers, embedding, heads, kv_heads)):
            key_length = metadata.get(f"{architecture}.attention.key_length", embedding // heads)
            value_length = metadata.get(f"{architecture}.attention.value_length", embedding // heads)
            if all(type(value) is int and 1 <= value <= 65536 for value in (key_length, value_length)):
                kv_bytes = 2 * layers * kv_heads * (key_length + value_length)
        return {"name": metadata.get("general.name") or path.stem, "architecture": architecture,
                "format": "gguf", "role": "generation", "quantization": _QUANTIZATIONS[quantization],
                "context_length": context, "chat_template": template,
                "template_digest": hashlib.sha256(template.encode("utf-8")).hexdigest(),
                "size_bytes": size, "tensor_count": len(names), "files": [str(item) for item in files],
                "kv_bytes_per_token": kv_bytes}
    except CoreDataError:
        raise
    except (OSError, ValueError, OverflowError, struct.error) as exc:
        raise CoreDataError("invalid_gguf", str(exc)) from exc
