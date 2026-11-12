import hashlib
import importlib.util
import json
from pathlib import Path
import zipfile

import pytest


def module():
    path = Path(__file__).resolve().parents[1] / 'program_release.py'
    spec = importlib.util.spec_from_file_location('program_release', path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_program_archive_contains_only_four_verified_binaries_and_contract(tmp_path):
    release = module()
    source = tmp_path / 'source'
    source.mkdir()
    files = {}
    for name in release.PROGRAM_FILES:
        data = name.encode()
        (source / name).write_bytes(data)
        files[name] = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
    runtime = source / 'llm-runtime/runtime.json'
    runtime.parent.mkdir()
    runtime.write_bytes(b'runtime')
    manifest = {'schema_version': 1, 'application_id': release.APPLICATION_ID, 'version': '0.16.0-preview.1',
                'files': files, 'runtime_manifests': {'llm-runtime/runtime.json': {
                    'bytes': 7, 'sha256': hashlib.sha256(b'runtime').hexdigest()}},
                'source_commit': 'a' * 40,
                'core_contract': {'schema_version': 30, 'protocol_version': 23, 'build_id': 'schema30-agent-unified-v1'}}
    (source / 'application.json').write_text(json.dumps(manifest))
    (source / 'private.gguf').write_bytes(b'private')
    destination = tmp_path / 'program.zip'
    release.build_archive(source, destination)
    with zipfile.ZipFile(destination) as archive:
        assert set(archive.namelist()) == {'update.json', *(f'payload/{name}' for name in files)}
        assert json.loads(archive.read('update.json')) == manifest
    (source / 'agent-core.exe').write_bytes(b'changed')
    with pytest.raises(ValueError, match='identity'):
        release.build_archive(source, tmp_path / 'changed.zip')
    assert not (tmp_path / 'changed.zip').exists()


def test_feed_requires_signed_program_package_on_independent_channel():
    release = module()
    feed = release.feed('0.16.0-preview.1', 'signed-envelope', 'preview', '发布说明')
    assert feed['platforms']['windows-x86_64']['signature'] == 'signed-envelope'
    assert feed['platforms']['windows-x86_64']['url'].endswith(
        '/v0.16.0-preview.1/agent-0.16.0-preview.1-windows-x86_64-program.zip')
    with pytest.raises(ValueError):
        release.feed('0.16.0-preview.1', 'signature', 'punkagent', '')


def test_release_signer_is_noninteractive_and_bounded(tmp_path, monkeypatch):
    from types import SimpleNamespace
    release = module()
    calls = []
    path = tmp_path / 'program.zip'
    path.write_bytes(b'fixture')
    def execute(command, **options):
        calls.append((command, options))
        if 'signer' in command:
            Path(str(path) + '.sig').write_text('signature')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(release.subprocess, 'run', execute)
    assert release.sign(path, tmp_path / 'private.key') == 'signature'
    command, options = calls[0]
    assert command[command.index('--password') + 1] == ''
    assert options['timeout'] == 120
