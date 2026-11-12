import hashlib
import json

import pytest

from agent.ocr.client import verify_runtime
from agent.storage import CoreDataError


def test_ocr_runtime_rejects_modified_bundled_files(tmp_path):
    executable = tmp_path / 'python' / 'python.exe'
    executable.parent.mkdir()
    executable.write_bytes(b'fixture')
    manifest = {'schema_version': 1, 'profile': 'ocr-preview-cpu-v1', 'files': {
        'python/python.exe': hashlib.sha256(b'fixture').hexdigest()}}
    (tmp_path / 'runtime.json').write_text(json.dumps(manifest))
    verify_runtime(tmp_path)
    executable.write_bytes(b'corrupt')
    with pytest.raises(CoreDataError, match='hash'):
        verify_runtime(tmp_path)


def test_client_sends_canonical_managed_paths(tmp_path):
    import asyncio
    from agent.ocr.client import OcrClient
    (tmp_path / 'nested').mkdir()
    root = tmp_path / 'nested' / '..' / 'data'
    root.mkdir()
    original = root / 'original.pdf'
    original.write_bytes(b'fixture')
    class Worker:
        async def request(self, action, params):
            return params
    async def run():
        client = OcrClient(root)
        client.worker = Worker()
        params = await client.request('scan', original)
        assert params['data_dir'] == str(root.resolve())
        assert params['path'] == str(original.resolve())
    asyncio.run(run())


def test_packaged_ocr_command_prevents_runtime_bytecode_mutation(tmp_path, monkeypatch):
    import sys
    from agent.ocr.client import runtime_command
    executable = tmp_path / 'ocr-runtime/python/python.exe'
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b'fixture')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(tmp_path / 'agent-core.exe'))
    assert runtime_command() == [str(executable), '-B', '-m', 'agent.ocr.worker']
