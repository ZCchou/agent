import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import zipfile

import pytest

TOOLS = Path(__file__).resolve().parents[1]


def module():
    import sys
    sys.path.insert(0, str(TOOLS))
    spec = importlib.util.spec_from_file_location('ocr_preview_release', TOOLS / 'ocr_preview_release.py')
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_volume_plan_stays_below_asset_limit_and_rejects_oversized_file():
    release = module()
    records = [{'name': f'ocr-runtime/{n}', 'bytes': 40} for n in range(5)]
    assert [len(group) for group in release.plan_volumes(records, limit=90)] == [2, 2, 1]
    with pytest.raises(ValueError):
        release.plan_volumes([{'name': 'oversized', 'bytes': 91}], limit=90)


def test_public_selection_excludes_local_qa_and_user_data():
    release = module()
    names = ['agent-desktop.exe', 'agent-core.exe', 'agent_desktop_lib.dll',
             'ocr-runtime/runtime.json', 'ocr-model-package/ocr-model.json',
             'do160-local-acceptance.json', 'agent.db', 'private.pdf', 'ocr-runtime/__pycache__/x.pyc']
    assert [name for name in names if release.is_public_file(name)] == names[:5]


def test_changed_tested_application_is_rejected(tmp_path):
    release = module()
    files = {}
    for name in release.APPLICATION:
        (tmp_path / name).write_bytes(b'tested')
        files[name] = {'bytes': 6, 'sha256': hashlib.sha256(b'tested').hexdigest()}
    release.verify_application(tmp_path, {'application_id': 'io.github.zcchou.agent.ocrpreview', 'files': files})
    (tmp_path / 'agent-core.exe').write_bytes(b'changed')
    with pytest.raises(ValueError, match='mismatch'):
        release.verify_application(tmp_path, {'application_id': 'io.github.zcchou.agent.ocrpreview', 'files': files})


def test_ocr_installer_rejects_document_preview_application_identity(tmp_path):
    release = module()
    files = {}
    for name in release.APPLICATION:
        (tmp_path / name).write_bytes(b'tested')
        files[name] = {'bytes': 6, 'sha256': hashlib.sha256(b'tested').hexdigest()}
    with pytest.raises(ValueError, match='identity'):
        release.verify_application(tmp_path, {'application_id': 'io.github.zcchou.agent.documentpreview', 'files': files})


def test_uninstaller_cleans_owned_extraction_stage_before_components(tmp_path, monkeypatch):
    release = module()
    source, output = tmp_path / 'source', tmp_path / 'output'
    source.mkdir()
    (output / 'build').mkdir(parents=True)
    for name in release.APPLICATION:
        (source / name).write_bytes(b'fixture')
    (source / 'delivery-manifest.json').write_text(json.dumps({'application_id': 'io.github.zcchou.agent.ocrpreview', 'files': {
        name: {'bytes': 7, 'sha256': hashlib.sha256(b'fixture').hexdigest()} for name in release.APPLICATION}}))
    payload, parts = tmp_path / 'payload.json', tmp_path / 'parts.json'
    payload.write_text('{}')
    parts.write_text('{}')
    monkeypatch.setattr(release.subprocess, 'run', lambda *args, **kwargs: None)
    release.write_installer(source, output, payload, parts, tmp_path / 'compiler')
    script = (output / 'build/setup.nsi').read_text(encoding='utf-8-sig')
    uninstall = script.split('Section Uninstall', 1)[1]
    assert uninstall.index('-Action Cleanup') < uninstall.index('NSIS_HOOK_PREUNINSTALL')
    registry_line = next(line for line in script.splitlines() if '"UninstallString"' in line)
    assert "'\"$INSTDIR\\uninstall.exe\"'" in registry_line


def test_ocr_hook_explicitly_enables_ocr_components(tmp_path):
    import sys
    sys.path.insert(0, str(TOOLS))
    from offline_delivery import installer_hook
    manifest = tmp_path / 'payload-manifest.json'
    application = tmp_path / 'application.json'
    manifest.write_text('{}')
    application.write_text('{}')
    hook = tmp_path / 'hook.nsh'
    installer_hook(manifest, hook, application_manifest=application, include_ocr=True)
    text = hook.read_text(encoding='utf-8-sig')
    assert '-IncludeOcr -Action Install' in text
    assert '-IncludeOcr -Action Commit' in text
    assert '-IncludeOcr -Action Uninstall' in text


def test_new_preview_version_is_used_in_installer_identity(tmp_path, monkeypatch):
    release = module()
    source, output = tmp_path / 'source', tmp_path / 'output'
    source.mkdir()
    (output / 'build').mkdir(parents=True)
    for name in release.APPLICATION:
        (source / name).write_bytes(b'fixture')
    (source / 'delivery-manifest.json').write_text(json.dumps({'application_id': 'io.github.zcchou.agent.ocrpreview', 'files': {
        name: {'bytes': 7, 'sha256': hashlib.sha256(b'fixture').hexdigest()} for name in release.APPLICATION}}))
    payload, parts = tmp_path / 'payload.json', tmp_path / 'parts.json'
    payload.write_text('{}')
    parts.write_text('{}')
    monkeypatch.setattr(release.subprocess, 'run', lambda *args, **kwargs: None)
    result = release.write_installer(source, output, payload, parts, tmp_path / 'compiler', version='0.15.0-ocr-preview.2')
    assert result.name == 'agent-0.15.0-ocr-preview.2-windows-x64-setup.exe'
    script = (output / 'build/setup.nsi').read_text(encoding='utf-8-sig')
    assert 'VIProductVersion "0.15.0.2"' in script
    assert '0.15.0-ocr-preview.1' not in script


def test_archive_reuse_checks_payload_and_archive_identities(tmp_path):
    release = module()
    old, output = tmp_path / 'old', tmp_path / 'new'
    (old / 'build').mkdir(parents=True)
    output.mkdir()
    payload = {'files': {'rag-runtime/fixture': {'bytes': 1, 'sha256': 'a' * 64}}}
    (old / 'build/payload-manifest.json').write_text(json.dumps(payload))
    archive = old / 'agent-0.15.0-ocr-preview.1-offline.part01.zip'
    archive.write_bytes(b'fixture archive')
    previous = {'name': archive.name, 'bytes': archive.stat().st_size,
                'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}
    (old / 'build/parts.json').write_text(json.dumps({'archives': [previous]}))
    result = release.reuse_archives(old, output, payload, '0.15.0-ocr-preview.2')
    assert result[0]['name'] == 'agent-0.15.0-ocr-preview.2-offline.part01.zip'
    assert (output / result[0]['name']).read_bytes() == archive.read_bytes()
    with pytest.raises(ValueError, match='payload'):
        release.reuse_archives(old, output, {'files': {}}, '0.15.0-ocr-preview.2')
    archive.write_bytes(b'changed archive')
    with pytest.raises(ValueError, match='mismatch'):
        release.reuse_archives(old, tmp_path / 'other', payload, '0.15.0-ocr-preview.3')


@pytest.mark.parametrize('fault', ['', 'long', 'missing', 'corrupt', 'escape', 'oversized'])
def test_local_archive_preparation_is_bounded_and_verified(tmp_path, fault):
    source = tmp_path / 'downloads'
    source.mkdir()
    data = b'offline OCR fixture'
    name = 'ocr-runtime/python/file.txt'
    if fault == 'long':
        name = 'ocr-runtime/' + 'nested/' * 22 + '组件 空格文件.txt'
    archive = source / 'payload.part01.zip'
    with zipfile.ZipFile(archive, 'w') as package:
        package.writestr('../escape.txt' if fault == 'escape' else name, data)
    payload = tmp_path / 'payload-manifest.json'
    payload.write_text(json.dumps({'schema_version': 1, 'release_id': 'ocr-preview-test', 'files': {
        name: {'bytes': 1 if fault == 'oversized' else len(data), 'sha256': hashlib.sha256(data).hexdigest()}}}))
    parts = tmp_path / 'parts.json'
    parts.write_text(json.dumps({'schema_version': 1, 'archives': [{'name': archive.name,
        'bytes': archive.stat().st_size, 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}]}))
    if fault == 'missing':
        archive.unlink()
    if fault == 'corrupt':
        archive.write_bytes(b'corrupt')
    install = tmp_path / 'install'
    result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
        str(TOOLS / 'installer/prepare-ocr-payload.ps1'), '-Action', 'Prepare',
        '-ArchiveDirectory', str(source), '-PartsManifestPath', str(parts),
        '-PayloadManifestPath', str(payload), '-InstallDirectory', str(install)], capture_output=True, timeout=30)
    if fault not in ('', 'long'):
        assert result.returncode != 0
        assert not (tmp_path / 'escape.txt').exists()
    else:
        assert result.returncode == 0, result.stderr.decode(errors='replace')
        assert (install / '.ocr-preview-payload/payload' / name).read_bytes() == data
