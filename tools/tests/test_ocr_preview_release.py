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
