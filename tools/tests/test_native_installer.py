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


def test_long_staging_paths_publish_rollback_and_uninstall(tmp_path):
    root = tmp_path / 'installed'
    name = 'rag-runtime/' + '/'.join(['long-package-name'] * 6) + '/checkpoint_conversion_helper_with_long_name.py'
    assert len(str(root / '.managed-runtime/native-transaction/stage' / name)) > 260
    old = package(tmp_path, {name: b'old'}, 'long-old')
    ok(run(root, old))
    assert (root / name).read_bytes() == b'old'
    new = package(tmp_path, {name: b'new'}, 'long-new')
    assert run(root, new, extra=['--fault', 'before-commit']).returncode == 97
    ok(run(root, action='recover'))
    assert (root / name).read_bytes() == b'old'
    ok(run(root, new))
    assert (root / name).read_bytes() == b'new'
    ok(run(root, action='uninstall'))
    assert not (root / name).exists()


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


def test_progress_reports_target_current_file_and_count_driven_percent(tmp_path):
    root = tmp_path / 'installed'
    manifest = package(tmp_path, {'rag-runtime/a': b'a' * 10000, 'rag-runtime/b': b'b' * 20000})
    result = run(root, manifest)
    ok(result)
    statuses = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
    assert all(item['target_directory'] == str(root) for item in statuses)
    extraction = [item for item in statuses if item['phase'] == 'extraction']
    assert any(item['current_file'] == 'rag-runtime/a' for item in extraction)
    assert any(40 < item['percent'] < 60 for item in extraction)
    assert all(item['percent'] < 100 for item in statuses if item['phase'] != 'completed')
    interrupted = run(root, package(tmp_path, {'rag-runtime/a': b'new'}, 'update'), extra=['--fault', 'before-commit'])
    assert interrupted.returncode == 97
    assert all(json.loads(line)['percent'] < 100 for line in interrupted.stdout.splitlines() if line.startswith('{'))


def test_explicit_installer_hwnds_receive_live_progress_and_status(tmp_path):
    import ctypes
    from ctypes import wintypes
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.CreateWindowExW.restype = wintypes.HWND
    user.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    user.SendMessageW.restype = ctypes.c_ssize_t
    user.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.DestroyWindow.argtypes = [wintypes.HWND]
    ctypes.WinDLL('comctl32').InitCommonControls()
    parent = user.CreateWindowExW(0, 'STATIC', 'test installer', 0, 0, 0, 500, 100, None, None, None, None)
    progress = user.CreateWindowExW(0, 'msctls_progress32', '', 0x40000000, 0, 0, 500, 20, parent, None, None, None)
    status = user.CreateWindowExW(0, 'STATIC', '', 0x40000000, 0, 20, 500, 80, parent, None, None, None)
    assert parent and progress and status
    user.SendMessageW(progress, 0x406, 0, 100)  # PBM_SETRANGE32
    root = tmp_path / 'installed'
    manifest = package(tmp_path, {'rag-runtime/current-file': b'content' * 10000})
    text_states, positions = [], []
    try:
        with (tmp_path / 'helper.log').open('w', encoding='utf8') as log:
            process = subprocess.Popen([str(HELPER), '--action', 'install', '--root', str(root),
                '--manifest', str(manifest), '--sha256', identity(manifest.read_bytes())['sha256'],
                '--archives', str(manifest.parent), '--embedded', str(manifest.parent),
                '--ui-parent', str(parent), '--progress-hwnd', str(progress), '--status-hwnd', str(status)], stdout=log, stderr=log)
            deadline = time.monotonic() + 30
            while process.poll() is None and time.monotonic() < deadline:
                message = wintypes.MSG()
                while user.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                    user.TranslateMessage(ctypes.byref(message))
                    user.DispatchMessageW(ctypes.byref(message))
                buffer = ctypes.create_unicode_buffer(2048)
                user.GetWindowTextW(status, buffer, len(buffer))
                text_states.append(buffer.value)
                positions.append(user.SendMessageW(progress, 0x408, 0, 0))
                time.sleep(.005)
            assert process.wait(timeout=2) == 0, (tmp_path / 'helper.log').read_text(encoding='utf8')
        assert any(str(root) in text for text in text_states)
        assert any('rag-runtime/current-file' in text for text in text_states)
        assert max(positions) == 100
    finally:
        user.DestroyWindow(parent)


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


def windows_process_alive(pid, terminate=False):
    import ctypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.restype = ctypes.c_void_p
    handle = kernel.OpenProcess(0x00100001, False, pid)
    if not handle:
        return False
    try:
        alive = kernel.WaitForSingleObject(ctypes.c_void_p(handle), 0) == 258
        if alive and terminate:
            kernel.TerminateProcess(ctypes.c_void_p(handle), 1)
            kernel.WaitForSingleObject(ctypes.c_void_p(handle), 5000)
        return alive
    finally:
        kernel.CloseHandle(ctypes.c_void_p(handle))


def test_helper_death_terminates_postflight_tree_before_recovery(tmp_path):
    root = tmp_path / 'installed'
    old = package(tmp_path, {'agent-core.exe': b'old'}, 'old')
    ok(run(root, old))
    new = package(tmp_path, {'agent-core.exe': b'new'})
    script = tmp_path / 'postflight.ps1'
    script.write_text('param($InstallDirectory, $ManifestPath, $HealthDirectory)\n'
        '$child=Start-Process powershell.exe -ArgumentList "-NoProfile -Command Start-Sleep -Seconds 20" -WindowStyle Hidden -PassThru\n'
        '[IO.File]::WriteAllText((Join-Path $InstallDirectory "grandchild.pid"), [string]$child.Id)\n'
        '[IO.File]::WriteAllText((Join-Path $InstallDirectory "postflight.pid"), [string]$PID)\n'
        'Start-Sleep -Seconds 6\n'
        'New-Item -ItemType Directory -Path $HealthDirectory -Force | Out-Null\n'
        '[IO.File]::WriteAllText((Join-Path $InstallDirectory "orphan-write"), "late")\n')
    process = subprocess.Popen([str(HELPER), '--action', 'install', '--root', str(root),
        '--manifest', str(new), '--sha256', identity(new.read_bytes())['sha256'],
        '--archives', str(new.parent), '--embedded', str(new.parent), '--postflight', str(script)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    children = []
    try:
        deadline = time.monotonic() + 30
        while not (root / 'postflight.pid').exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.05)
        assert (root / 'postflight.pid').exists()
        children = [int((root / name).read_text()) for name in ('postflight.pid', 'grandchild.pid')]
        process.kill()
        process.wait(timeout=10)
        ok(run(root, action='recover'))
        assert not any(windows_process_alive(pid) for pid in children), 'postflight descendants survived recovery'
        assert not (root / 'orphan-write').exists()
        assert (root / 'agent-core.exe').read_bytes() == b'old'
        ok(run(root, new))
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)
        for pid in children:
            windows_process_alive(pid, terminate=True)


def test_running_worker_blocks_equivalent_installation_root_aliases(tmp_path):
    root = tmp_path / 'installed'
    worker_bytes = (Path(os.environ['SystemRoot']) / 'System32/ping.exe').read_bytes()
    old = package(tmp_path, {'agent-core.exe': b'old', 'rag-runtime/worker.exe': worker_bytes}, 'old')
    ok(run(root, old))
    new = package(tmp_path, {'agent-core.exe': b'new', 'rag-runtime/worker.exe': worker_bytes})
    alias = tmp_path / 'alias'
    alias.mkdir()
    worker = subprocess.Popen([str(root / 'rag-runtime/worker.exe'), '-t', '127.0.0.1'], stdout=subprocess.DEVNULL)
    try:
        normal = run(root, new)
        assert normal.returncode != 0
        result = run(alias / '..' / 'installed', new)
        assert result.returncode != 0, 'aliased root bypassed live-worker check'
        assert 'ambiguous installation root' in result.stderr
        assert (root / 'agent-core.exe').read_bytes() == b'old'
        assert run(Path('\\\\?\\' + str(root)), new).returncode != 0
        import ctypes
        short = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.kernel32.GetShortPathNameW(str(root), short, len(short)):
            assert run(Path(short.value), new).returncode != 0
    finally:
        worker.terminate()
        worker.wait(timeout=10)


@pytest.mark.parametrize('fault,transaction', [('after-publish:0', 'native-transaction'),
    ('before-prepare-journal', 'native-preparation'), ('after-cleanup-rename', 'native-cleanup')])
def test_recovery_preserves_unknown_native_transaction_content(tmp_path, fault, transaction):
    root = tmp_path / 'installed'
    old = package(tmp_path, {'agent-core.exe': b'old'}, 'old')
    ok(run(root, old))
    new = package(tmp_path, {'agent-core.exe': b'new'})
    assert run(root, new, extra=['--fault', fault]).returncode == 97
    personal = root / '.managed-runtime' / transaction / 'personal.txt'
    personal.write_bytes(b'personal data')
    result = run(root, action='recover')
    ok(result)
    preserved = list((root / '.managed-runtime').rglob('personal.txt'))
    assert len(preserved) == 1 and preserved[0].read_bytes() == b'personal data'
    assert 'PRESERVED_UNKNOWN' in result.stdout
    ok(run(root, new))


def test_late_uninstall_failure_keeps_retry_executable(tmp_path):
    root = tmp_path / 'installed'
    manifest = package(tmp_path, {'agent-core.exe': b'core', 'uninstall.exe': b'retry'})
    ok(run(root, manifest))
    version = root / '.managed-runtime/versions' / ('b' * 32)
    (version / 'previous').mkdir(parents=True)
    (version / 'owner.txt').write_text(APP + '.offline-runtime.v1')
    (version / 'previous-active.json').write_text('malformed')
    result = run(root, action='uninstall')
    assert result.returncode != 0
    assert (root / 'uninstall.exe').read_bytes() == b'retry'
    (version / 'previous-active.json').write_text(json.dumps({'schema_version': 1,
        'release_id': 'agent-0.16.0-preview.2', 'files': {}}))
    ok(run(root, action='uninstall'))
    assert (root / 'uninstall.exe').read_bytes() == b'retry', 'NSIS owns final uninstaller deletion'


def test_owner_sharing_failure_after_inventory_removal_is_retryable(tmp_path):
    import ctypes
    root = tmp_path / 'installed'
    manifest = package(tmp_path, {'agent-core.exe': b'core', 'uninstall.exe': b'retry'})
    ok(run(root, manifest))
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    handle = kernel.CreateFileW(str(root / '.managed-runtime/owner.txt'), 0x80000000, 3, None, 3, 0, None)
    assert handle != ctypes.c_void_p(-1).value
    try:
        result = run(root, action='uninstall')
        assert result.returncode != 0
        assert not (root / '.managed-runtime/native-active.json').exists()
        assert (root / 'uninstall.exe').read_bytes() == b'retry'
    finally:
        kernel.CloseHandle(ctypes.c_void_p(handle))
    ok(run(root, action='uninstall'))
    assert not (root / '.managed-runtime').exists()
    assert (root / 'uninstall.exe').read_bytes() == b'retry'


def test_successful_native_uninstall_can_retry_until_nsis_tail_finishes(tmp_path):
    root = tmp_path / 'installed'
    manifest = package(tmp_path, {'agent-core.exe': b'core', 'uninstall.exe': b'retry'})
    ok(run(root, manifest))
    ok(run(root, action='uninstall'))
    assert not (root / '.managed-runtime').exists()
    ok(run(root, action='uninstall'))
    assert (root / 'uninstall.exe').read_bytes() == b'retry'
    # A retry record must not authorize unrelated roots just because files are absent.
    unrelated = tmp_path / 'unrelated'
    unrelated.mkdir()
    assert run(unrelated, action='uninstall').returncode != 0
    (unrelated / '.airworthiness-uninstall.json').write_bytes((root / '.airworthiness-uninstall.json').read_bytes())
    (unrelated / 'agent-core.exe').write_bytes(b'personal')
    result = run(unrelated, action='uninstall')
    assert result.returncode != 0 and 'ownership differs' in result.stderr
    assert (unrelated / 'agent-core.exe').read_bytes() == b'personal'


@pytest.mark.parametrize('fault', ['uninstall-after-inventory', 'uninstall-after-owner', 'uninstall-before-nsis-tail'])
def test_uninstall_interruption_retains_durable_retry_inventory(tmp_path, fault):
    root = tmp_path / 'installed'
    manifest = package(tmp_path, {'agent-core.exe': b'core', 'uninstall.exe': b'retry'})
    ok(run(root, manifest))
    (root / 'personal.txt').write_bytes(b'personal')
    result = run(root, action='uninstall', extra=['--fault', fault])
    assert result.returncode == 97
    assert (root / 'uninstall.exe').read_bytes() == b'retry'
    ok(run(root, action='uninstall'))
    assert not (root / '.managed-runtime').exists()
    assert (root / 'personal.txt').read_bytes() == b'personal'


def test_reinstall_supersedes_interrupted_uninstall_inventory(tmp_path):
    root = tmp_path / 'installed'
    old = package(tmp_path, {'agent-core.exe': b'old', 'uninstall.exe': b'retry'}, 'old')
    ok(run(root, old))
    ok(run(root, action='uninstall'))
    new = package(tmp_path, {'agent-core.exe': b'new', 'uninstall.exe': b'retry'})
    ok(run(root, new))
    assert not (root / '.airworthiness-uninstall.json').exists()
    assert (root / 'agent-core.exe').read_bytes() == b'new'
