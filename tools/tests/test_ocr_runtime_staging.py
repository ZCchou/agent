import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


def builder():
    path = Path(__file__).resolve().parents[1] / 'build_ocr_runtime.py'
    spec = importlib.util.spec_from_file_location('ocr_runtime_builder', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def inputs(tmp_path):
    old = tmp_path / 'previous'
    package = old / 'python/Lib/site-packages'
    (package / 'agent/ocr').mkdir(parents=True)
    (old / 'python/python.exe').write_bytes(b'verified-python')
    (package / 'native_dependency.pyd').write_bytes(b'verified-native')
    (package / 'agent/ocr/worker.py').write_text('old_source = True\n')
    (old / 'requirements.lock').write_text('dependency==1\n')
    manifest = {'schema_version': 1, 'profile': 'ocr-preview-cpu-v1', 'python': '3.12.10',
        'python_archive_sha256': builder().PYTHON_ARCHIVE_SHA256,
        'files': {path.relative_to(old).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in old.rglob('*') if path.is_file()}}
    (old / 'runtime.json').write_text(json.dumps(manifest))
    repository = tmp_path / 'repository'
    agent = repository / 'services/agent-core/src/agent/ocr'
    agent.mkdir(parents=True)
    (agent / 'worker.py').write_text('current_source = True\n')
    (repository / 'services/agent-core/requirements-ocr-preview.lock').write_text('dependency==1\n')
    return old, repository


def test_staging_reuses_verified_dependencies_and_owns_current_agent_source(tmp_path):
    module = builder()
    old, repository = inputs(tmp_path)
    destination = tmp_path / 'new/ocr-runtime'
    module.stage_existing(old, destination, repository=repository)
    native = 'python/Lib/site-packages/native_dependency.pyd'
    worker = 'python/Lib/site-packages/agent/ocr/worker.py'
    assert (old / native).samefile(destination / native)
    assert (destination / worker).stat().st_nlink == 1
    (destination / worker).write_text('new_local_edit = True\n')
    assert (old / worker).read_text() == 'old_source = True\n'
    descriptor = json.loads((destination / 'runtime.json').read_text())
    assert descriptor['protocol_version'] == 2
    assert descriptor['build_identity']['services/agent-core/src/agent/ocr/worker.py'] == hashlib.sha256(
        b'current_source = True\n').hexdigest()


def test_staging_rejects_tampered_dependency_before_creating_output(tmp_path):
    module = builder()
    old, repository = inputs(tmp_path)
    (old / 'python/python.exe').write_bytes(b'tampered')
    destination = tmp_path / 'new/ocr-runtime'
    with pytest.raises(ValueError, match='hash mismatch'):
        module.stage_existing(old, destination, repository=repository)
    assert not destination.exists()


def test_staging_rejects_unlisted_source_files(tmp_path):
    module = builder()
    old, repository = inputs(tmp_path)
    (old / 'python/unlisted.py').write_text('pass\n')
    with pytest.raises(ValueError, match='Unlisted'):
        module.stage_existing(old, tmp_path / 'new/ocr-runtime', repository=repository)


def test_source_refresh_unlinks_shared_targets_and_preserves_other_stages(tmp_path):
    import os
    module = builder()
    old, repository = inputs(tmp_path)
    destination = tmp_path / 'new/ocr-runtime'
    module.stage_existing(old, destination, repository=repository)
    relative = 'python/Lib/site-packages/agent/ocr/worker.py'
    shared = tmp_path / 'other-stage-worker.py'
    os.link(destination / relative, shared)
    current = repository / 'services/agent-core/src/agent/ocr/worker.py'
    current.write_text('frozen_source = True\n')
    module.refresh_sources(destination, repository=repository)
    assert shared.read_text() == 'current_source = True\n'
    assert (destination / relative).read_text() == 'frozen_source = True\n'
    assert (destination / relative).stat().st_nlink == 1
    assert (old / relative).read_text() == 'old_source = True\n'
    module.verify_existing(destination)
