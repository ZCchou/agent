import struct

import pytest

from agent.storage import CoreDataError


def gguf_file(path, *, architecture="qwen3", template="{{ messages }}", split=None,
              tensor_name="weight", version=3, offset=0, tensor_type=12,
              shape=(256,), metadata_extra=None):
    metadata = {"general.architecture": architecture, "general.name": "小模型", "general.file_type": 15,
                "tokenizer.chat_template": template, f"{architecture}.context_length": 32768}
    if split:
        metadata.update({"split.no": split[0], "split.count": split[1], "split.tensors.count": split[1]})
    metadata.update(metadata_extra or {})

    def string(value):
        raw = value.encode("utf-8")
        return struct.pack("<Q", len(raw)) + raw

    content = bytearray(b"GGUF" + struct.pack("<IQQ", version, 1, len(metadata)))
    for key, value in metadata.items():
        content.extend(string(key))
        if isinstance(value, str):
            content.extend(struct.pack("<I", 8) + string(value))
        else:
            content.extend(struct.pack("<II", 4, value))
    content.extend(string(tensor_name) + struct.pack("<I", len(shape)))
    content.extend(struct.pack("<" + "Q" * len(shape), *shape))
    content.extend(struct.pack("<IQ", tensor_type, offset))
    content.extend(b"\0" * (-len(content) % 32))
    content.extend(b"\0" * 144)
    path.write_bytes(content)
    return path


def test_inspect_unicode_gguf_preserves_architecture_and_template(tmp_path):
    from agent.local_models.gguf import inspect_gguf
    path = gguf_file(tmp_path / "千问.gguf")
    result = inspect_gguf(path)
    assert result["architecture"] == "qwen3"
    assert result["quantization"] == "Q4_K_M"
    assert result["chat_template"] == "{{ messages }}"
    assert result["context_length"] == 32768
    assert result["size_bytes"] == path.stat().st_size


def test_inspection_exposes_fp16_kv_estimate_from_attention_metadata(tmp_path):
    from agent.local_models.gguf import inspect_gguf
    path = gguf_file(tmp_path / "model.gguf", metadata_extra={"qwen3.block_count": 32,
        "qwen3.embedding_length": 4096, "qwen3.attention.head_count": 32, "qwen3.attention.head_count_kv": 8})
    assert inspect_gguf(path)["kv_bytes_per_token"] == 4 * 32 * 8 * 128


@pytest.mark.parametrize("options,code", [({"architecture": "llama"}, "unsupported_architecture"),
    ({"version": 1}, "invalid_gguf"), ({"offset": 32}, "invalid_gguf"),
    ({"tensor_type": 999}, "invalid_gguf"), ({"shape": (255,)}, "invalid_gguf"),
    ({"template": ""}, "unsupported_template")])
def test_invalid_model_rejected_before_runtime(tmp_path, options, code):
    from agent.local_models.gguf import inspect_gguf
    with pytest.raises(CoreDataError) as failure:
        inspect_gguf(gguf_file(tmp_path / "bad.gguf", **options))
    assert failure.value.code == code


def test_bounded_metadata_and_truncated_tensor(tmp_path):
    from agent.local_models.gguf import inspect_gguf
    path = tmp_path / "bad.gguf"
    path.write_bytes(b"GGUF" + struct.pack("<IQQQ", 3, 1, 1, 2**63))
    with pytest.raises(CoreDataError):
        inspect_gguf(path)
    gguf_file(path)
    path.write_bytes(path.read_bytes()[:-1])
    with pytest.raises(CoreDataError):
        inspect_gguf(path)


def test_complete_split_set_resolves_only_exact_siblings(tmp_path):
    from agent.local_models.gguf import inspect_gguf
    first = gguf_file(tmp_path / "qwen-00001-of-00002.gguf", split=(0, 2), tensor_name="a")
    second = tmp_path / "qwen-00002-of-00002.gguf"
    with pytest.raises(CoreDataError) as failure:
        inspect_gguf(first)
    assert failure.value.code == "incomplete_shards"
    gguf_file(second, split=(1, 2), tensor_name="b")
    result = inspect_gguf(second)
    assert result["files"] == [str(first), str(second)]
    assert result["tensor_count"] == 2
    gguf_file(second, split=(1, 2), tensor_name="b", template="changed")
    with pytest.raises(CoreDataError) as failure:
        inspect_gguf(first)
    assert failure.value.code == "inconsistent_shards"


def test_split_filename_cannot_disguise_missing_metadata(tmp_path):
    from agent.local_models.gguf import inspect_gguf
    with pytest.raises(CoreDataError):
        inspect_gguf(gguf_file(tmp_path / "qwen-00001-of-00002.gguf"))


def test_import_source_hardlink_is_rejected(tmp_path):
    import os
    from agent.local_models.gguf import inspect_gguf
    original = gguf_file(tmp_path / "original.gguf")
    linked = tmp_path / "linked.gguf"
    os.link(original, linked)
    with pytest.raises(CoreDataError) as failure:
        inspect_gguf(linked)
    assert failure.value.code == "unsafe_path"


@pytest.mark.parametrize("limit,value", [("_MAX_MODEL_TENSORS", 1), ("_MAX_TENSOR_NAME_BYTES", 3)])
def test_complete_split_set_has_shared_tensor_and_name_budgets(tmp_path, monkeypatch, limit, value):
    import agent.local_models.gguf as gguf
    first = gguf_file(tmp_path / "qwen-00001-of-00002.gguf", split=(0, 2), tensor_name="aa")
    gguf_file(tmp_path / "qwen-00002-of-00002.gguf", split=(1, 2), tensor_name="bb")
    monkeypatch.setattr(gguf, limit, value, raising=False)
    with pytest.raises(CoreDataError) as error:
        gguf.inspect_gguf(first)
    assert error.value.code == "invalid_gguf"


def test_declared_split_tensor_limit_checked_before_reading_missing_siblings(tmp_path):
    from agent.local_models.gguf import inspect_gguf
    first = gguf_file(tmp_path / "qwen-00001-of-00002.gguf", split=(0, 2),
                      metadata_extra={"split.tensors.count": 100_001})
    with pytest.raises(CoreDataError) as error:
        inspect_gguf(first)
    assert error.value.code == "invalid_gguf"


def test_split_inspection_cancels_before_reading_next_shard(tmp_path, monkeypatch):
    import agent.local_models.gguf as gguf
    first = gguf_file(tmp_path / "qwen-00001-of-00002.gguf", split=(0, 2), tensor_name="aa")
    gguf_file(tmp_path / "qwen-00002-of-00002.gguf", split=(1, 2), tensor_name="bb")
    original = gguf._read_shard
    reads = []

    def tracked(*args, **kwargs):
        result = original(*args, **kwargs)
        reads.append(args[0])
        return result

    def cancel():
        if reads:
            raise RuntimeError("inspection cancelled")

    monkeypatch.setattr(gguf, "_read_shard", tracked)
    with pytest.raises(RuntimeError, match="inspection cancelled"):
        gguf.inspect_gguf(first, cancel=cancel)
    assert len(reads) == 1
