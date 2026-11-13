"""Real native helper transactions on tiny independent offline volumes."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import zipfile

import pytest

APP = 'io.github.zcchou.agent.airworthiness'
HELPER = Path(os.environ.get('AGENT_INSTALLER_HELPER', 'F:/agent-unified-v016-20261111/cargo-target/debug/agent-installer.exe'))
pytestmark = pytest.mark.skipif(os.name != 'nt' or not HELPER.is_file(), reason='Build the native Windows installer helper first')


def identity(data):
    return {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def package(tmp_path, files, label='new'):
    downloads = tmp_path / label
    downloads.mkdir()
    records, archives = {}, []
    for index, (name, data) in enumerate(files.items()):
        volume = downloads / f'{index}.zip'
        with zipfile.ZipFile(volume, 'w', zipfile.ZIP_DEFLATED) as stream:
            stream.writestr(name, data)
        archives.append({'name': volume.name, **identity(volume.read_bytes())})
        records[name] = {**identity(data), 'volume': volume.name}
    manifest = downloads / 'manifest.json'
    manifest.write_text(json.dumps({'schema_version': 2, 'application_id': APP,
        'release_id': label, 'files': records, 'archives': archives}), encoding='utf-8')
    return manifest


def run(root, manifest=None, action='install', extra=()):
    args = [str(HELPER), '--action', action, '--root', str(root)]
    if manifest:
        args += ['--manifest', str(manifest), '--sha256', identity(manifest.read_bytes())['sha256'],
                 '--archives', str(manifest.parent), '--embedded', str(manifest.parent)]
    return subprocess.run(args + list(extra), capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60)


def ok(result):
    assert result.returncode == 0, result.stdout + result.stderr


def test_verified_unchanged_file_keeps_identity_and_never_reads_its_volume(tmp_path):
    root = tmp_path / 'installed'
    old = package(tmp_path, {'rag-runtime/same': b'same', 'agent-core.exe': b'old'}, 'old')
    ok(run(root, old))
    inode = (root / 'rag-runtime/same').stat().st_ino
    new = package(tmp_path, {'rag-runtime/same': b'same', 'agent-core.exe': b'new'})
    (new.parent / '0.zip').unlink()
    result = run(root, new)
    ok(result)
    assert (root / 'rag-runtime/same').stat().st_ino == inode
    assert (root / 'agent-core.exe').read_bytes() == b'new'
    assert 'completed' in result.stdout and '"percent":100' in result.stdout


def test_modified_owned_file_is_repaired_and_unknown_data_survives(tmp_path):
    root = tmp_path / 'installed'
    manifest = package(tmp_path, {'rag-runtime/file': b'valid'})
    ok(run(root, manifest))
    (root / 'rag-runtime/file').write_bytes(b'broken')
    (root / 'rag-runtime/personal.txt').write_text('personal')
    ok(run(root, manifest))
    assert (root / 'rag-runtime/file').read_bytes() == b'valid'
    ok(run(root, action='uninstall'))
    assert (root / 'rag-runtime/personal.txt').read_text() == 'personal'
    assert not (root / 'rag-runtime/file').exists()


@pytest.mark.parametrize('fault', ['before-prepare-journal', 'after-prepare-journal',
    'before-prepare-rename', 'after-prepare-rename', 'before-backup:0', 'after-backup:0', 'before-publish:0',
    'after-publish:0', 'before-backup:1', 'after-backup:1', 'before-publish:1',
    'after-publish:1', 'before-backup:2', 'after-backup:2', 'before-publish:2',
    'after-publish:2', 'before-inventory', 'after-inventory', 'before-commit', 'after-commit',
    'before-cleanup', 'after-cleanup-rename'])
def test_crash_recovery_preserves_transaction_atomicity(tmp_path, fault):
    root = tmp_path / 'installed'
    old = package(tmp_path, {'agent-core.exe': b'old', 'rag-runtime/deleted': b'delete'}, 'old')
    ok(run(root, old))
    new = package(tmp_path, {'agent-core.exe': b'new', 'rag-runtime/added': b'added'})
    interrupted = run(root, new, extra=['--fault', fault])
    assert interrupted.returncode == 97, interrupted.stdout + interrupted.stderr
    ok(run(root, action='recover'))
    if fault in ('after-commit', 'before-cleanup', 'after-cleanup-rename'):
        assert (root / 'agent-core.exe').read_bytes() == b'new'
        assert not (root / 'rag-runtime/deleted').exists()
    else:
        assert (root / 'agent-core.exe').read_bytes() == b'old'
        assert (root / 'rag-runtime/deleted').read_bytes() == b'delete'
        assert not (root / 'rag-runtime/added').exists()


def test_postflight_failure_rolls_back_application_and_runtime(tmp_path):
    root = tmp_path / 'installed'
    old = package(tmp_path, {'agent-core.exe': b'old', 'rag-runtime/a': b'old'}, 'old')
    ok(run(root, old))
    new = package(tmp_path, {'agent-core.exe': b'new', 'rag-runtime/a': b'new'})
    script = tmp_path / 'fail.ps1'
    script.write_text('param($InstallDirectory, $ManifestPath, $HealthDirectory)\nexit 1\n')
    result = run(root, new, extra=['--postflight', str(script)])
    assert result.returncode != 0
    assert (root / 'agent-core.exe').read_bytes() == b'old'
    assert (root / 'rag-runtime/a').read_bytes() == b'old'


@pytest.mark.skipif(os.name != 'nt', reason='Windows reparse contracts')
def test_junction_and_hardlink_cannot_redirect_owned_resources(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    root = tmp_path / 'installed'
    root.mkdir()
    subprocess.run(['cmd', '/c', 'mklink', '/J', str(root / 'rag-runtime'), str(outside)], check=True, capture_output=True)
    manifest = package(tmp_path, {'rag-runtime/a': b'new'})
    result = run(root, manifest)
    assert result.returncode != 0 and 'reparse' in result.stderr and not (outside / 'a').exists()
    (root / 'rag-runtime').rmdir()
    ok(run(root, manifest))
    os.link(root / 'rag-runtime/a', outside / 'a')
    result = run(root, manifest)
    assert result.returncode != 0 and 'linked' in result.stderr
    assert (outside / 'a').read_bytes() == b'new'


def test_missing_volumes_lists_all_required_identities(tmp_path):
    manifest = package(tmp_path, {'rag-runtime/a': b'a', 'rag-runtime/b': b'b'})
    for path in manifest.parent.glob('*.zip'):
        path.unlink()
    result = run(tmp_path / 'installed', manifest)
    assert result.returncode != 0
    assert '0.zip' in result.stderr and '1.zip' in result.stderr
    assert not (tmp_path / 'installed/rag-runtime/a').exists()


def test_old_volume_filename_resolves_by_identity(tmp_path):
    manifest = package(tmp_path, {'rag-runtime/a': b'a'})
    (manifest.parent / '0.zip').rename(manifest.parent / 'previous-version.part01.zip')
    ok(run(tmp_path / 'installed', manifest))


@pytest.mark.parametrize('name', ['../escape', 'rag-runtime/../escape', 'rag-runtime/NUL',
    'rag-runtime/a:stream', 'rag-runtime/trailing.', 'rag-runtime/UPPER/../x'])
def test_unsafe_paths_are_rejected_before_publication(tmp_path, name):
    manifest = package(tmp_path, {name: b'bad'})
    result = run(tmp_path / 'installed', manifest)
    assert result.returncode != 0 and 'unsafe' in result.stderr
    assert not (tmp_path / 'escape').exists()


def test_unowned_existing_file_and_wrong_owner_are_not_replaced(tmp_path):
    root = tmp_path / 'installed'
    (root / 'rag-runtime').mkdir(parents=True)
    (root / 'rag-runtime/a').write_bytes(b'personal')
    manifest = package(tmp_path, {'rag-runtime/a': b'new'})
    result = run(root, manifest)
    assert result.returncode != 0 and 'unowned destination' in result.stderr
    assert (root / 'rag-runtime/a').read_bytes() == b'personal'
    managed = root / '.managed-runtime'
    managed.mkdir(exist_ok=True)
    (managed / 'owner.txt').write_text('other-app')
    result = run(root, manifest)
    assert result.returncode != 0 and 'wrong managed ownership' in result.stderr


def test_live_installer_holds_exclusive_lock_until_postflight_exits(tmp_path):
    root = tmp_path / 'installed'
    manifest = package(tmp_path, {'rag-runtime/a': b'new'})
    script = tmp_path / 'pause.ps1'
    script.write_text('param($InstallDirectory, $ManifestPath, $HealthDirectory)\n'
        '[IO.File]::WriteAllText((Join-Path $InstallDirectory "ready"), "ready")\n'
        'Start-Sleep -Seconds 6\nexit 0\n')
    process = subprocess.Popen([str(HELPER), '--action', 'install', '--root', str(root),
        '--manifest', str(manifest), '--sha256', identity(manifest.read_bytes())['sha256'],
        '--archives', str(manifest.parent), '--embedded', str(manifest.parent), '--postflight', str(script)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 30
        while not (root / 'ready').exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.1)
        assert (root / 'ready').exists()
        result = run(root, manifest)
        assert result.returncode != 0 and 'installation lock' in result.stderr
        assert (root / 'rag-runtime/a').read_bytes() == b'new'
        assert process.wait(timeout=30) == 0
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


def test_legacy_preview_inventory_is_adopted_without_full_payload_copy(tmp_path):
    root = tmp_path / 'installed'
    (root / '.managed-runtime').mkdir(parents=True)
    (root / 'rag-runtime').mkdir()
    (root / 'rag-runtime/a').write_bytes(b'old')
    (root / 'agent-core.exe').write_bytes(b'legacy')
    (root / '.managed-runtime/owner.txt').write_text(APP + '.offline-runtime.v1')
    (root / '.managed-runtime/active.json').write_text(json.dumps({'schema_version': 1,
        'release_id': 'agent-0.16.0-preview.2', 'files': {'rag-runtime/a': identity(b'old')}}))
    manifest = package(tmp_path, {'rag-runtime/a': b'old', 'agent-core.exe': b'new'})
    (manifest.parent / '0.zip').unlink()
    ok(run(root, manifest))
    assert (root / 'agent-core.exe').read_bytes() == b'new'


@pytest.mark.parametrize('unknown', [False, True])
def test_legacy_backup_cleanup_uses_recorded_inventory_and_preserves_unknowns(tmp_path, unknown):
    root = tmp_path / 'installed'
    version = root / '.managed-runtime/versions' / ('a' * 32)
    (version / 'previous/rag-runtime').mkdir(parents=True)
    (root / 'rag-runtime').mkdir()
    (root / 'rag-runtime/a').write_bytes(b'old')
    (version / 'previous/rag-runtime/a').write_bytes(b'backup')
    (root / '.managed-runtime/owner.txt').write_text(APP + '.offline-runtime.v1')
    (version / 'owner.txt').write_text(APP + '.offline-runtime.v1')
    active = {'schema_version': 1, 'release_id': 'agent-0.16.0-preview.2',
        'files': {'rag-runtime/a': identity(b'old')}}
    (root / '.managed-runtime/active.json').write_text(json.dumps(active))
    (version / 'payload-manifest.json').write_text(json.dumps(active))
    (version / 'previous-active.json').write_text(json.dumps({**active,
        'files': {'rag-runtime/a': identity(b'backup')}}))
    if unknown:
        (version / 'previous/personal.txt').write_text('personal')
    manifest = package(tmp_path, {'rag-runtime/a': b'new'})
    ok(run(root, manifest))
    assert not (version / 'previous/rag-runtime/a').exists()
    if unknown:
        assert (version / 'previous/personal.txt').read_text() == 'personal'
    else:
        assert not version.exists()
        ok(run(root, action='uninstall'))
        assert not (root / '.managed-runtime').exists()


@pytest.mark.skipif(os.name != 'nt', reason='Windows sharing contracts')
def test_locked_owned_file_blocks_update(tmp_path):
    import ctypes
    root = tmp_path / 'installed'
    manifest = package(tmp_path, {'rag-runtime/a': b'old'})
    ok(run(root, manifest))
    new = package(tmp_path, {'rag-runtime/a': b'new'}, 'next')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    handle = kernel.CreateFileW(str(root / 'rag-runtime/a'), 0x80000000, 1, None, 3, 0, None)
    assert handle != ctypes.c_void_p(-1).value
    try:
        assert run(root, new).returncode != 0
        assert (root / 'rag-runtime/a').read_bytes() == b'old'
    finally:
        kernel.CloseHandle(ctypes.c_void_p(handle))
