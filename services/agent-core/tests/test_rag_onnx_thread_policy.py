import json
import sys
from types import SimpleNamespace

import pytest

from agent.rag.worker import _load


def fake_runtime(tmp_path, monkeypatch, provider):
    calls = []
    class Options:
        def __init__(self):
            self.intra_op_num_threads = 0
            self.inter_op_num_threads = 0
            self.entries = {}

        def add_session_config_entry(self, name, value):
            self.entries[name] = value

    class Session:
        def disable_fallback(self):
            pass

        def get_providers(self):
            return [provider]

    def session(*args, **kwargs):
        calls.append((args, kwargs))
        return Session()

    monkeypatch.setitem(sys.modules, 'onnxruntime', SimpleNamespace(
        InferenceSession=session, SessionOptions=Options))
    monkeypatch.setitem(sys.modules, 'transformers', SimpleNamespace(
        AutoTokenizer=SimpleNamespace(from_pretrained=lambda *args, **kwargs: object())))
    monkeypatch.setattr('agent.rag.worker._engine', lambda *args: {})
    (tmp_path / 'rag-model.json').write_text(json.dumps({'graph': 'model.onnx'}))
    return calls


@pytest.mark.parametrize('cpu_count,expected', [(None, 1), (1, 1), (4, 4), (32, 8)])
@pytest.mark.parametrize('profile', ['bge-m3', 'bge-reranker-v2-m3'])
def test_cuda_onnx_bounds_fallback_threads_without_changing_graph_provider_or_cache(
        tmp_path, monkeypatch, cpu_count, expected, profile):
    calls = fake_runtime(tmp_path, monkeypatch, 'CUDAExecutionProvider')
    monkeypatch.setattr('agent.rag.worker.os.cpu_count', lambda: cpu_count)
    params = {'path': str(tmp_path), 'profile': profile, 'backend': 'onnx',
              'device': 'cuda', 'digest': 'original-model'}
    cache = {}
    first = _load(params, cache)
    assert _load(params, cache) is first
    assert len(calls) == 1
    positional, kwargs = calls[0]
    assert positional == (str(tmp_path / 'model.onnx'),)
    assert kwargs['providers'] == ['CUDAExecutionProvider']
    options = kwargs.get('sess_options')
    assert options is not None
    assert options.intra_op_num_threads == expected
    assert options.inter_op_num_threads == 1
    assert options.entries == {'session.intra_op.allow_spinning': '0',
                               'session.inter_op.allow_spinning': '0'}


@pytest.mark.parametrize('profile', ['bge-m3', 'bge-reranker-v2-m3'])
def test_cpu_onnx_keeps_default_session_options(tmp_path, monkeypatch, profile):
    calls = fake_runtime(tmp_path, monkeypatch, 'CPUExecutionProvider')
    def forbidden():
        raise AssertionError('CPU inference must retain ORT defaults')
    monkeypatch.setattr(sys.modules['onnxruntime'], 'SessionOptions', forbidden)
    _load({'path': str(tmp_path), 'profile': profile, 'backend': 'onnx',
           'device': 'cpu', 'digest': 'original-model'}, {})
    assert calls[0][1] == {'providers': ['CPUExecutionProvider']}
