import sys
from types import SimpleNamespace

import pytest

from agent.rag import worker


@pytest.fixture
def fake_ort(monkeypatch):
    calls = []
    monkeypatch.setattr(worker, '_cuda_dlls_preloaded', False, raising=False)
    monkeypatch.setattr(worker.importlib.util, 'find_spec', lambda name: object())
    runtime = SimpleNamespace(
        preload_dlls=lambda **kwargs: calls.append(('preload', kwargs)),
        get_available_providers=lambda: calls.append(('providers', {})) or [
            'CUDAExecutionProvider', 'CPUExecutionProvider'])
    monkeypatch.setitem(sys.modules, 'onnxruntime', runtime)
    return runtime, calls


def test_cuda_preloads_once_but_checks_providers_on_every_request(fake_ort):
    _, calls = fake_ort
    for _ in range(4):
        assert worker._engine('onnx', 'cuda')['provider'] == 'CUDAExecutionProvider'
    assert calls == [('preload', {'cuda': True, 'cudnn': True, 'msvc': True})] + [
        ('providers', {})] * 4


def test_failed_preload_is_retried_and_success_is_cached(fake_ort):
    runtime, calls = fake_ort
    attempts = []
    def preload(**kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise OSError('CUDA dependency unavailable')
    runtime.preload_dlls = preload
    with pytest.raises(OSError, match='dependency unavailable'):
        worker._engine('onnx', 'cuda')
    assert calls == []
    worker._engine('onnx', 'cuda')
    worker._engine('onnx', 'cuda')
    assert len(attempts) == 2
    assert calls == [('providers', {})] * 2


def test_cached_preload_does_not_cache_provider_availability(fake_ort):
    runtime, calls = fake_ort
    worker._engine('onnx', 'cuda')
    runtime.get_available_providers = lambda: ['CPUExecutionProvider']
    for _ in range(2):
        with pytest.raises(RuntimeError, match='CUDAExecutionProvider is unavailable'):
            worker._engine('onnx', 'cuda')
    assert [kind for kind, _ in calls].count('preload') == 1


def test_cpu_never_preloads_cuda_and_keeps_provider_checks(fake_ort):
    _, calls = fake_ort
    for _ in range(3):
        assert worker._engine('onnx', 'cpu')['provider'] == 'CPUExecutionProvider'
    assert calls == [('providers', {})] * 3
    assert not worker._cuda_dlls_preloaded


def test_torch_does_not_use_onnx_preload(fake_ort, monkeypatch):
    _, calls = fake_ort
    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: True)))
    assert worker._engine('transformers', 'cuda')['engine'] == 'transformers'
    assert calls == []
    assert not worker._cuda_dlls_preloaded
