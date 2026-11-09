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
