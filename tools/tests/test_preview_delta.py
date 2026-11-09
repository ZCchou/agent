import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest

TOOLS = Path(__file__).resolve().parents[1]


def module():
    sys.path.insert(0, str(TOOLS))
    spec = importlib.util.spec_from_file_location('preview_delta', TOOLS / 'preview_delta.py')
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def record(path):
    data = path.read_bytes()
    return {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def fixture(tmp_path):
    release = module()
    old, new, target = [tmp_path / name for name in ('old', 'new', '中文 安装')]
    for path in (old, new, target):
        path.mkdir()
    before, after = {}, {}
    for name in release.APPLICATION:
        (old / name).write_bytes(b'old-' + name.encode())
        (new / name).write_bytes(b'new-' + name.encode())
        (target / name).write_bytes((old / name).read_bytes())
        before[name], after[name] = record(old / name), record(new / name)
    for root in (old, new):
        (root / 'rag-runtime').mkdir()
        (root / 'rag-runtime/runtime.json').write_text('unchanged')
    (target / 'rag-runtime').mkdir()
    (target / 'rag-runtime/runtime.json').write_text('unchanged')
    runtime = {'rag-runtime/runtime.json': record(old / 'rag-runtime/runtime.json')}
    baseline = {'version': '0.15.0-document-preview.1', 'application_id': 'io.github.zcchou.agent.documentpreview',
                'files': {**before, **runtime}}
    current = {'version': '0.15.0-document-preview.2', 'application_id': baseline['application_id'],
               'source_commit': 'a' * 40, 'files': {**after, **runtime}}
    (old / 'delivery-manifest.json').write_text(json.dumps(baseline))
    (new / 'delivery-manifest.json').write_text(json.dumps(current))
    archive = release.build_delta(new, baseline, tmp_path / 'update.zip')
    bundle = tmp_path / 'bundle'
    with zipfile.ZipFile(archive) as package:
        assert not any(name.endswith(('.gguf', '.pdf', '.db')) for name in package.namelist())
        package.extractall(bundle)
    return release, old, new, target, bundle


def apply(bundle, target):
    return subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
        '-File', str(bundle / 'apply-update.ps1'), '-InstallDirectory', str(target)], capture_output=True, timeout=40)


def test_delta_updates_application_preserves_data_and_is_idempotent(tmp_path):
    release, old, new, target, bundle = fixture(tmp_path)
    (target / 'agent.db').write_bytes(b'user data')
    result = apply(bundle, target)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    for name in release.APPLICATION:
        assert (target / name).read_bytes() == (new / name).read_bytes()
    assert (target / 'agent.db').read_bytes() == b'user data'
    assert apply(bundle, target).returncode == 0
    assert list((target / '.agent-app-update').glob('*/previous/agent-core.exe'))


@pytest.mark.parametrize('fault', ['source', 'baseline', 'runtime', 'traversal'])
def test_delta_rejects_invalid_input_before_replacing_files(tmp_path, fault):
    release, old, new, target, bundle = fixture(tmp_path)
    if fault == 'source':
        (bundle / 'payload/agent-core.exe').write_bytes(b'corrupt')
    elif fault == 'baseline':
        (target / 'agent-core.exe').write_bytes(b'unknown application')
    elif fault == 'runtime':
        (target / 'rag-runtime/runtime.json').write_text('different runtime')
    else:
        manifest = json.loads((bundle / 'update.json').read_text())
        manifest['files']['../escape.exe'] = manifest['files']['agent-core.exe']
        (bundle / 'update.json').write_text(json.dumps(manifest))
    before = {name: (target / name).read_bytes() for name in release.APPLICATION}
    assert apply(bundle, target).returncode != 0
    assert {name: (target / name).read_bytes() for name in release.APPLICATION} == before
    assert not (tmp_path / 'escape.exe').exists()


def test_partial_copy_failure_restores_previous_application(tmp_path):
    release, old, new, target, bundle = fixture(tmp_path)
    import os
    import stat
    protected = target / release.APPLICATION[1]
    os.chmod(protected, stat.S_IREAD)
    try:
        assert apply(bundle, target).returncode != 0
        for name in release.APPLICATION:
            assert (target / name).read_bytes() == (old / name).read_bytes()
    finally:
        os.chmod(protected, stat.S_IWRITE)


def test_interrupted_update_is_recovered_before_a_new_attempt(tmp_path):
    release, old, new, target, bundle = fixture(tmp_path)
    managed = target / '.agent-app-update'
    previous = managed / ('b' * 32) / 'previous'
    previous.mkdir(parents=True)
    for name in release.APPLICATION:
        (previous / name).write_bytes((old / name).read_bytes())
    manifest = json.loads((bundle / 'update.json').read_text())
    (managed / 'owner.txt').write_text('agent-preview-app-update-v1')
    (managed / 'pending.json').write_text(json.dumps({'id': 'b' * 32, 'version': manifest['version']}))
    (target / release.APPLICATION[0]).write_bytes((new / release.APPLICATION[0]).read_bytes())
    result = apply(bundle, target)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    for name in release.APPLICATION:
        assert (target / name).read_bytes() == (new / name).read_bytes()
    assert not (managed / 'pending.json').exists()


def test_update_atomically_consumes_verified_candidates(tmp_path):
    release, old, new, target, bundle = fixture(tmp_path)
    result = apply(bundle, target)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    assert not list((target / '.agent-app-update').glob('*/candidate/*.exe'))
    assert not list((target / '.agent-app-update').glob('*/candidate/*.dll'))


def test_recovery_atomically_consumes_restore_candidate():
    text = (TOOLS / 'installer/update-preview-application.ps1').read_text(encoding='utf-8')
    recovery = text.split('function Recover-Pending', 1)[1].split('\ntry {', 1)[0]
    assert '[IO.File]::Replace' in recovery


def test_installed_ocr_delta_uses_uninstaller_owned_runtime_tree(tmp_path):
    release, old, new, target, bundle = fixture(tmp_path)
    manifest_path = bundle / 'update.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['application_id'] = 'io.github.zcchou.agent.ocrpreview'
    manifest_path.write_text(json.dumps(manifest))
    managed = target / '.managed-runtime'
    managed.mkdir()
    (managed / 'owner.txt').write_text('io.github.zcchou.agent.ocrpreview.offline-runtime.v1')
    result = apply(bundle, target)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    assert list((managed / 'application-updates').glob('*/previous/agent-core.exe'))
    assert not (target / '.agent-app-update').exists()


def test_delta_builder_rejects_changed_runtime(tmp_path):
    release, old, new, target, bundle = fixture(tmp_path)
    baseline = json.loads((old / 'delivery-manifest.json').read_text())
    current = json.loads((new / 'delivery-manifest.json').read_text())
    current['files']['rag-runtime/runtime.json']['sha256'] = '0' * 64
    (new / 'delivery-manifest.json').write_text(json.dumps(current))
    with pytest.raises(ValueError, match='runtime'):
        release.build_delta(new, baseline, tmp_path / 'bad.zip')


def test_live_application_worker_blocks_delta_before_replacement(tmp_path):
    release, old, new, target, bundle = fixture(tmp_path)
    executable = target / 'busy.exe'
    shutil.copyfile(Path(os.environ['WINDIR']) / 'System32/WindowsPowerShell/v1.0/powershell.exe', executable)
    process = subprocess.Popen([str(executable), '-NoProfile', '-Command', '[Threading.Thread]::Sleep(30000)'])
    try:
        result = apply(bundle, target)
        assert result.returncode != 0
        assert b'Busy PID' in result.stderr
        for name in release.APPLICATION:
            assert (target / name).read_bytes() == (old / name).read_bytes()
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_concurrent_full_installer_lock_blocks_delta(tmp_path):
    release, old, new, target, bundle = fixture(tmp_path)
    digest = hashlib.sha256(str(target).lower().encode()).hexdigest().upper()
    command = "$m=New-Object Threading.Mutex($true,'Local\\agent-offline-install-" + digest + "'); [Console]::WriteLine('ready'); [Threading.Thread]::Sleep(30000)"
    process = subprocess.Popen(['powershell.exe', '-NoProfile', '-Command', command], stdout=subprocess.PIPE)
    try:
        assert process.stdout.readline().strip() == b'ready'
        result = apply(bundle, target)
        assert result.returncode != 0
        assert b'Another installer' in result.stderr
        assert not (target / '.agent-app-update').exists()
    finally:
        process.terminate()
        process.wait(timeout=10)
