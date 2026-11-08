import json
import os
import struct

import pytest


def test_owned_safetensors_loads_shards_dtype_and_zero_sized_tensors(tmp_path):
    import torch
    from safetensors.torch import save_file
    from agent.rag.worker import _owned_state_dict
    values = {"float": torch.arange(6, dtype=torch.float32).reshape(2, 3),
              "bf16": torch.tensor([1, 2], dtype=torch.bfloat16),
              "empty": torch.empty(0), "integer": torch.tensor(7, dtype=torch.int64)}
    save_file({key: values[key] for key in ("float", "bf16")}, tmp_path / "first.safetensors")
    save_file({key: values[key] for key in ("empty", "integer")}, tmp_path / "second.safetensors")
    (tmp_path / "model.safetensors.index.json").write_text(json.dumps({"weight_map": {
        "float": "first.safetensors", "bf16": "first.safetensors", "empty": "second.safetensors", "integer": "second.safetensors"}}))
    actual = _owned_state_dict(tmp_path)
    assert actual.keys() == values.keys()
    for key, expected in values.items():
        assert actual[key].dtype == expected.dtype and torch.equal(actual[key], expected)
    # Deleting/replacing source files cannot invalidate these owned buffers.
    (tmp_path / "first.safetensors").unlink()
    assert torch.equal(actual["float"], values["float"])


def test_owned_safetensors_rejects_truncated_ranges_and_unsafe_index_paths(tmp_path):
    from agent.rag.worker import _owned_state_dict
    header = json.dumps({"x": {"dtype": "F32", "shape": [4], "data_offsets": [0, 16]}}).encode()
    (tmp_path / "model.safetensors").write_bytes(struct.pack("<Q", len(header)) + header + b"short")
    with pytest.raises(ValueError):
        _owned_state_dict(tmp_path)
    (tmp_path / "model.safetensors").unlink()
    (tmp_path / "model.safetensors.index.json").write_text('{"weight_map":{"x":"../outside.safetensors"}}')
    with pytest.raises(ValueError):
        _owned_state_dict(tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="Windows owned weight loading policy")
def test_windows_native_load_passes_owned_state_to_fixed_model_class(tmp_path, monkeypatch):
    import torch
    from transformers import XLMRobertaModel
    from agent.rag.worker import dispatch
    from test_rag_worker import native_model
    path = tmp_path / "native"
    native_model(path, "bge-m3")
    original = XLMRobertaModel.from_pretrained
    observed = []
    def load(checkpoint, **kwargs):
        observed.append(checkpoint)
        assert checkpoint is None
        assert kwargs["state_dict"] and all(isinstance(value, torch.Tensor) for value in kwargs["state_dict"].values())
        return original(checkpoint, **kwargs)
    monkeypatch.setattr(XLMRobertaModel, "from_pretrained", load)
    actual = dispatch({"action": "embed", "params": {"path": str(path), "profile": "bge-m3",
        "backend": "transformers", "device": "cpu", "texts": ["document relevant"]}}, {})
    assert observed == [None]
    assert len(actual[0]) == 16


def test_owned_reader_rejects_header_change_after_validation(tmp_path, monkeypatch):
    import torch
    from safetensors.torch import save_file
    import agent.rag.models as models
    from agent.rag.worker import _owned_state_dict
    path = tmp_path / "model.safetensors"
    save_file({"weight": torch.ones(2)}, path)
    original = models._validate_safetensors
    def changed(file, **kwargs):
        result = original(file, **kwargs)
        content = path.read_bytes()
        path.write_bytes(struct.pack("<Q", 101 * 1024**2) + content[8:])
        return result
    monkeypatch.setattr(models, "_validate_safetensors", changed)
    with pytest.raises(ValueError, match="changed"):
        _owned_state_dict(tmp_path)
