import importlib.util
from pathlib import Path

import pytest


def assets():
    source = Path(__file__).resolve().parents[1] / 'rag_runtime_assets.py'
    spec = importlib.util.spec_from_file_location('runtime_assets_under_test', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bundle(tmp_path):
    root = tmp_path / 'rag-runtime'
    (root / '_internal/torch/lib').mkdir(parents=True)
    (root / 'rag-worker.exe').write_bytes(b'worker')
    return root


def test_prune_deduplicates_identical_root_dll_and_preserves_torch_layout(tmp_path):
    root = bundle(tmp_path)
    canonical = root / '_internal/torch/lib/torch_cuda.dll'
    duplicate = root / '_internal/torch_cuda.dll'
    canonical.write_bytes(b'cuda-runtime')
    duplicate.write_bytes(canonical.read_bytes())
    report = assets().prune(root)
    assert canonical.read_bytes() == b'cuda-runtime'
    assert not duplicate.exists()
    assert report['deduplicated_dll_bytes'] == len(b'cuda-runtime')
    assert report['removed_files'][0]['canonical_path'] == '_internal/torch/lib/torch_cuda.dll'
    assert report['removed_bytes'] == report['deduplicated_dll_bytes']
    assert assets().prune(root)['removed_bytes'] == 0


def test_prune_preserves_same_name_dlls_with_different_contents(tmp_path):
    root = bundle(tmp_path)
    canonical = root / '_internal/torch/lib/cublas64_12.dll'
    duplicate = root / '_internal/cublas64_12.dll'
    canonical.write_bytes(b'version-a')
    duplicate.write_bytes(b'version-b')
    report = assets().prune(root)
    assert canonical.read_bytes() == b'version-a'
    assert duplicate.read_bytes() == b'version-b'
    assert report['deduplicated_dll_bytes'] == 0


def test_prune_preserves_dlls_without_a_canonical_torch_copy(tmp_path):
    root = bundle(tmp_path)
    dll = root / '_internal/onnxruntime_providers_shared.dll'
    dll.write_bytes(b'onnx-runtime')
    assert assets().prune(root)['removed_bytes'] == 0
    assert dll.read_bytes() == b'onnx-runtime'


def test_prune_rejects_linked_runtime_root(tmp_path):
    root = bundle(tmp_path)
    alias = tmp_path / 'alias/rag-runtime'
    alias.parent.mkdir()
    try:
        alias.symlink_to(root, target_is_directory=True)
    except OSError:
        pytest.skip('Directory symlinks require platform support')
    with pytest.raises(ValueError, match='[Ll]ink'):
        assets().prune(alias)
