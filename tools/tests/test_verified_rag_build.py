import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


def builder():
    path = Path(__file__).resolve().parents[1] / 'build_verified_rag_runtime.py'
    spec = importlib.util.spec_from_file_location('verified_rag_builder', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source_runtime(tmp_path, version=1):
    root = tmp_path / 'previous' / 'rag-runtime'
    (root / '_internal/torch').mkdir(parents=True)
    (root / 'rag-worker.exe').write_bytes(b'previous-worker')
    (root / '_internal/torch/__init__.py').write_text('version = "cuda"\n')
    files = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in root.rglob('*') if path.is_file()}
    (root / 'runtime.json').write_text(json.dumps({'schema_version': 1, 'protocol_version': version,
        'device_bundle': 'cuda', 'executable': 'rag-worker.exe', 'files': files}))
    return root


def test_verified_build_source_accepts_legacy_manifest_and_checks_every_hash(tmp_path):
    module = builder()
    root = source_runtime(tmp_path)
    facts = module.verify_source(root)
    assert facts['protocol_version'] == 1 and facts['file_count'] == 2
    (root / '_internal/torch/__init__.py').write_text('tampered = True\n')
    with pytest.raises(ValueError, match='hash mismatch'):
        module.verify_source(root)


def test_verified_build_source_rejects_unlisted_files(tmp_path):
    module = builder()
    root = source_runtime(tmp_path)
    (root / '_internal/unlisted.py').write_text('pass\n')
    with pytest.raises(ValueError, match='Unlisted'):
        module.verify_source(root)


def test_verified_build_source_rejects_cpu_runtime(tmp_path):
    module = builder()
    root = source_runtime(tmp_path)
    path = root / 'runtime.json'
    manifest = json.loads(path.read_text())
    manifest['device_bundle'] = 'cpu'
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='CUDA'):
        module.verify_source(root)


def test_verified_overlay_has_priority_in_main_and_pyinstaller_isolated_child(tmp_path):
    module = builder()
    overlay = tmp_path / 'overlay'
    overlay.mkdir()
    (overlay / 'build_dependency_marker.py').write_text('value = "verified-overlay"\n')
    local = tmp_path / 'local'
    local.mkdir()
    (local / 'build_dependency_marker.py').write_text('value = "wrong-local"\n')
    environment = module.build_environment(tmp_path / 'build', overlay, tmp_path / 'src')
    code = '''import sys,json
from PyInstaller import isolated
import build_dependency_marker
def child():
 import build_dependency_marker
 return build_dependency_marker.value, build_dependency_marker.__file__
print(json.dumps({'main':[build_dependency_marker.value,build_dependency_marker.__file__],
                  'child':isolated.call(child)}))
'''
    result = subprocess.run([sys.executable, '-c', code], cwd=local, env=environment,
                            capture_output=True, text=True, check=True)
    facts = json.loads(result.stdout)
    assert facts['main'][0] == facts['child'][0] == 'verified-overlay'
    assert Path(facts['main'][1]).parent == overlay
    assert Path(facts['child'][1]).parent == overlay
