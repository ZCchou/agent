import struct

import pytest

from agent.storage import CoreDataError


def gguf_file(path, *, architecture="qwen3", template="{{ messages }}", split=None,
              tensor_name="weight", version=3, offset=0, tensor_type=12,
              shape=(256,), metadata_extra=None, metadata_remove=(), payload_bytes=144,
              tensors=None):
    metadata = {"general.architecture": architecture, "general.name": "小模型", "general.file_type": 15,
                "tokenizer.chat_template": template, f"{architecture}.context_length": 32768}
    if split:
        metadata.update({"split.no": split[0], "split.count": split[1], "split.tensors.count": split[1]})
    metadata.update(metadata_extra or {})
    for key in metadata_remove:
        metadata.pop(key, None)

    def string(value):
        raw = value.encode("utf-8")
        return struct.pack("<Q", len(raw)) + raw

    tensors = tensors or [(tensor_name, shape, tensor_type, offset)]
    content = bytearray(b"GGUF" + struct.pack("<IQQ", version, len(tensors), len(metadata)))
    for key, value in metadata.items():
        content.extend(string(key))
        if isinstance(value, str):
            content.extend(struct.pack("<I", 8) + string(value))
        elif isinstance(value, list):
            content.extend(struct.pack("<IIQ", 9, 7, len(value)))
            content.extend(struct.pack("<" + "?" * len(value), *value))
        else:
            content.extend(struct.pack("<II", 4, value))
    for name, dimensions, kind, position in tensors:
        content.extend(string(name) + struct.pack("<I", len(dimensions)))
        content.extend(struct.pack("<" + "Q" * len(dimensions), *dimensions))
        content.extend(struct.pack("<IQ", kind, position))
    content.extend(b"\0" * (-len(content) % 32))
    content.extend(b"\0" * payload_bytes)
    path.write_bytes(content)
    return path


@pytest.mark.parametrize("kind,block,width", [
    (9, 32, 36), (16, 256, 66), (17, 256, 74), (18, 256, 98),
    (19, 256, 50), (20, 32, 18), (21, 256, 110), (22, 256, 82),
    (23, 256, 136), (24, 1, 1), (25, 1, 2), (26, 1, 4),
    (27, 1, 8), (28, 1, 8), (29, 256, 56), (30, 1, 2),
    (34, 256, 54), (35, 256, 66), (39, 32, 17), (40, 64, 36),
    (41, 128, 18), (42, 64, 18), (43, 1, 1),
])
def test_locked_ggml_type_sizes_enforce_exact_file_boundary(tmp_path, kind, block, width):
    from agent.local_models.gguf import inspect_gguf
    path = gguf_file(tmp_path / "dynamic.gguf", tensor_type=kind, shape=(block,), payload_bytes=width)
    assert inspect_gguf(path)["tensor_count"] == 1
    path.write_bytes(path.read_bytes()[:-1])
    with pytest.raises(CoreDataError) as error:
        inspect_gguf(path)
    assert error.value.code == "invalid_gguf"


def test_qwen35_accepts_dynamic_tensor_mixture_using_metadata(tmp_path):
    from agent.local_models.gguf import inspect_gguf
    path = gguf_file(tmp_path / "arbitrary-name.gguf", architecture="qwen35",
        tensors=[("a", (256,), 12, 0), ("b", (256,), 23, 160),
                 ("c", (32,), 8, 320), ("d", (16,), 30, 384)], payload_bytes=416)
    result = inspect_gguf(path)
    assert result["architecture"] == "qwen35"
    assert result["quantization"] == "Q4_K_M"
    assert result["tensor_count"] == 4


def test_qwen35_estimates_only_full_attention_kv_and_fp32_recurrent_state(tmp_path):
    from agent.local_models.gguf import inspect_gguf
    path = gguf_file(tmp_path / "hybrid.gguf", architecture="qwen35", metadata_extra={
        "qwen35.block_count": 64, "qwen35.embedding_length": 5120,
        "qwen35.attention.head_count": 24, "qwen35.attention.head_count_kv": 4,
        "qwen35.attention.key_length": 256, "qwen35.attention.value_length": 256,
        "qwen35.full_attention_interval": 4, "qwen35.ssm.conv_kernel": 4,
        "qwen35.ssm.inner_size": 6144, "qwen35.ssm.state_size": 128,
        "qwen35.ssm.group_count": 16})
    result = inspect_gguf(path)
    assert result["kv_bytes_per_token"] == 65536
    assert result["recurrent_state_bytes"] == 156893184
    assert result["full_attention_layers"] == 16
    assert result["recurrent_layers"] == 48


def test_qwen35_default_interval_matches_locked_engine_and_reports_missing_state_metadata(tmp_path):
    from agent.local_models.gguf import inspect_gguf
    path = gguf_file(tmp_path / "hybrid.gguf", architecture="qwen35", metadata_extra={
        "qwen35.block_count": 8, "qwen35.embedding_length": 4096,
        "qwen35.attention.head_count": 32, "qwen35.attention.head_count_kv": 8})
    result = inspect_gguf(path)
    assert result["kv_bytes_per_token"] == 8192
    assert result["recurrent_state_bytes"] is None
    assert "recurrent_state_bytes" in result["memory_estimate_unknowns"]


def test_qwen35_explicit_recurrent_layer_array_overrides_interval(tmp_path):
    from agent.local_models.gguf import inspect_gguf
    path = gguf_file(tmp_path / "hybrid.gguf", architecture="qwen35", metadata_extra={
        "qwen35.block_count": 4, "qwen35.embedding_length": 4096,
        "qwen35.attention.head_count": 32, "qwen35.attention.head_count_kv": 8,
        "qwen35.attention.recurrent_layers": [True, False, False, False]})
    result = inspect_gguf(path)
    assert result["kv_bytes_per_token"] == 12288
    assert result["recurrent_layers"] == 1


@pytest.mark.parametrize("explicit_layout", [False, True])
def test_qwen35_excludes_mtp_layers_from_default_server_memory(tmp_path, explicit_layout):
    from agent.local_models.gguf import inspect_gguf
    metadata = {"qwen35.block_count": 65, "qwen35.nextn_predict_layers": 1,
        "qwen35.embedding_length": 5120, "qwen35.attention.head_count": 24,
        "qwen35.attention.head_count_kv": 4, "qwen35.attention.key_length": 256,
        "qwen35.attention.value_length": 256}
    if explicit_layout:
        metadata["qwen35.attention.recurrent_layers"] = [True, True, True, False] * 16 + [False]
    result = inspect_gguf(gguf_file(tmp_path / "mtp.gguf", architecture="qwen35", metadata_extra=metadata))
    assert result["kv_bytes_per_token"] == 65536
    assert result["full_attention_layers"] == 16
    assert result["recurrent_layers"] == 48


@pytest.mark.parametrize("extra", [
    {"qwen35.full_attention_interval": 0},
    {"qwen35.attention.recurrent_layers": [True]},
    {"qwen35.nextn_predict_layers": 5},
])
def test_qwen35_invalid_layer_layout_is_rejected(tmp_path, extra):
    from agent.local_models.gguf import inspect_gguf
    path = gguf_file(tmp_path / "hybrid.gguf", architecture="qwen35",
        metadata_extra={"qwen35.block_count": 4, **extra})
    with pytest.raises(CoreDataError) as error:
        inspect_gguf(path)
    assert error.value.code == "invalid_gguf"


def test_qwen35_mtp_only_model_has_no_default_trunk_cache(tmp_path):
    from agent.local_models.gguf import inspect_gguf
    result = inspect_gguf(gguf_file(tmp_path / "mtp.gguf", architecture="qwen35",
        metadata_extra={"qwen35.block_count": 1, "qwen35.nextn_predict_layers": 1}))
    assert result["kv_bytes_per_token"] == 0
    assert result["recurrent_state_bytes"] == 0


def test_inspect_unicode_gguf_preserves_architecture_and_template(tmp_path):
    from agent.local_models.gguf import inspect_gguf
    path = gguf_file(tmp_path / "千问.gguf")
    result = inspect_gguf(path)
    assert result["architecture"] == "qwen3"
    assert result["quantization"] == "Q4_K_M"
    assert result["chat_template"] == "{{ messages }}"
    assert result["context_length"] == 32768
    assert result["size_bytes"] == path.stat().st_size


@pytest.mark.parametrize('declared', [True, False])
def test_inspection_distinguishes_declared_context_from_fallback(tmp_path, declared):
    from agent.local_models.gguf import inspect_gguf
    path = gguf_file(tmp_path / 'context.gguf', metadata_remove=() if declared else ('qwen3.context_length',))
    inspection = inspect_gguf(path)
    assert inspection['declared_context_length'] == (32768 if declared else None)
    assert inspection['context_length'] == (32768 if declared else 8192)


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
