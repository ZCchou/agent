"""Opt-in complete generated NSIS lifecycle with a tiny real Core ping fixture."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMPILER = Path('C:/Users/Administrator/AppData/Local/tauri/NSIS/makensis.exe')
HELPER = Path(os.environ.get('AGENT_INSTALLER_HELPER', 'F:/agent-unified-v016-20261111/cargo-target/debug/agent-installer.exe'))
pytestmark = pytest.mark.skipif(os.environ.get('AGENT_TEST_REAL_NSIS') != '1', reason='Opt-in real native NSIS lifecycle')


def identity(path):
    data = path.read_bytes()
    return {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def wait_native(path, arguments):
    # NSIS uninstaller relocates itself and launches a child. PowerShell -Wait
    # waits for the descendant tree, whereas subprocess.wait observes the stub.
    command = "$p=Start-Process -FilePath $env:FIXTURE_EXE -ArgumentList $env:FIXTURE_ARGS -Wait -PassThru -WindowStyle Hidden; exit $p.ExitCode"
    result = subprocess.run(['powershell.exe', '-NoProfile', '-Command', command],
        env=dict(os.environ, FIXTURE_EXE=str(path), FIXTURE_ARGS=' '.join(arguments)),
        capture_output=True, timeout=180)
    assert result.returncode == 0, (result.stdout + result.stderr).decode(errors='replace')


def test_real_generated_nsis_fresh_differential_upgrade_and_uninstall(tmp_path):
    import winreg
    for key in [r'Software\agentAirworthiness', r'Software\Microsoft\Windows\CurrentVersion\Uninstall\agentAirworthiness']:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key):
                pytest.skip('Preserve the existing independent installation registration')
        except FileNotFoundError:
            pass
    sys.path.insert(0, str(ROOT / 'tools'))
    spec = importlib.util.spec_from_file_location('native_release_fixture', ROOT / 'tools/ocr_preview_release.py')
    release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release)
    source = tmp_path / 'source'
    source.mkdir()
    response = json.dumps({'v': 23, 'id': 'installer-health', 'result': {
        'core_version': '0.16.0-preview.3', 'protocol_version': 23,
        'schema_version': 30, 'build_id': 'tiny-fixture'}})
    code = source / 'core.cs'
    code.write_text('using System; class TinyCore { static void Main() { '
        'string data = Environment.GetEnvironmentVariable("AGENT_DATA_DIR"); '
        'if (data == null || !data.EndsWith("health-data")) Environment.Exit(22); '
        'System.IO.File.WriteAllText(System.IO.Path.Combine(data, "isolated-health.txt"), "ok"); '
        'Console.ReadLine(); Console.WriteLine(@"'
        + response.replace('"', '""') + '"); } }', encoding='utf-8')
    subprocess.run(['C:/Windows/Microsoft.NET/Framework64/v4.0.30319/csc.exe', '/nologo',
        '/out:' + str(source / 'agent-core.exe'), str(code)], check=True, capture_output=True)
    for name in ('agent-desktop.exe', 'agent_desktop_lib.dll', 'agent-updater.exe'):
        (source / name).write_bytes(b'native NSIS fixture')
    (source / 'application.json').write_text(json.dumps({'schema_version': 1,
        'application_id': 'io.github.zcchou.agent.airworthiness', 'version': '0.16.0-preview.3',
        'core_contract': {'protocol_version': 23, 'schema_version': 30, 'build_id': 'tiny-fixture'}}))
    (source / 'delivery-manifest.json').write_text(json.dumps({
        'application_id': 'io.github.zcchou.agent.airworthiness',
        'files': {name: identity(source / name) for name in release.UNIFIED_APPLICATION}}))

    def compile_package(label, changed):
        output = tmp_path / label
        (output / 'build').mkdir(parents=True)
        (output / '安装与使用说明.md').write_text('Synthetic installer lifecycle fixture', encoding='utf-8')
        files = {'rag-runtime/unchanged': b'same', 'rag-runtime/changed': changed}
        parts = []
        for index, (name, data) in enumerate(files.items()):
            archive = output / f'{index}.zip'
            with zipfile.ZipFile(archive, 'w') as stream:
                stream.writestr(name, data)
            parts.append({'name': archive.name, **identity(archive)})
        payload = output / 'build/payload.json'
        payload.write_text(json.dumps({'schema_version': 1, 'release_id': label, 'prerequisites': {},
            'files': {name: {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()} for name, data in files.items()}}))
        volumes = output / 'build/parts.json'
        volumes.write_text(json.dumps({'archives': parts}))
        return release.write_installer(source, output, payload, volumes, COMPILER,
            version='0.16.0-preview.3', unified=True, helper=HELPER)

    first = compile_package('first', b'old')
    second = compile_package('second', b'new')
    install = tmp_path / 'installed'
    try:
        wait_native(first, ['/S', '/D=' + str(install)])
        unchanged = install / 'rag-runtime/unchanged'
        inode = unchanged.stat().st_ino
        (second.parent / '0.zip').unlink()
        wait_native(second, ['/S', '/D=' + str(install)])
        assert unchanged.stat().st_ino == inode
        assert (install / 'rag-runtime/changed').read_bytes() == b'new'
        assert not (install / '.managed-runtime/native-transaction').exists()
        wait_native(install / 'uninstall.exe', ['/S'])
        deadline = time.monotonic() + 15
        while install.exists() and time.monotonic() < deadline:
            time.sleep(.1)
        assert not install.exists(), 'Waited for actual cleanup, but installed files remain'
    finally:
        if (install / 'uninstall.exe').exists():
            wait_native(install / 'uninstall.exe', ['/S'])
