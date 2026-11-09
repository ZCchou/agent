"""Focused offline package inventory and transactional installer regressions."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parents[1]


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('offline_delivery', TOOLS / 'offline_delivery.py')
        self.delivery = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.delivery)
        self.temp = tempfile.TemporaryDirectory(prefix='agent-offline-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ('llm-runtime/a.exe', 'rag-runtime/rag-worker.exe', 'prerequisites/vc.exe'):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(name.encode())
        self.app_manifest = self.root / 'application-manifest.json'
        self.app_manifest.write_text(json.dumps({'schema_version': 1, 'files': {
            name: {'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
            for name, content in {'agent-desktop.exe': b'complete new host',
                                  'agent-core.exe': b'complete new core'}.items()}}))

    def test_inventory_verifies_and_detects_modified_missing_and_unlisted_files(self):
        manifest = self.delivery.inventory(self.root)
        self.delivery.verify_inventory(self.root, manifest)
        extra = self.root / 'llm-runtime/unlisted.dll'
        extra.write_bytes(b'bad')
        with self.assertRaises(ValueError):
            self.delivery.verify_inventory(self.root, manifest)
        extra.unlink()
        (self.root / 'llm-runtime/a.exe').write_bytes(b'bad')
        with self.assertRaises(ValueError):
            self.delivery.verify_inventory(self.root, manifest)
        (self.root / 'llm-runtime/a.exe').unlink()
        with self.assertRaises(ValueError):
            self.delivery.verify_inventory(self.root, manifest)

    def test_complete_zip_contains_installer_all_payloads_and_provenance(self):
        import shutil
        import zipfile
        release = self.root / 'release'
        payload = release / 'payload'
        payload.mkdir(parents=True)
        for name in ('llm-runtime', 'rag-runtime', 'prerequisites'):
            shutil.copytree(self.root / name, payload / name)
        lock = release / 'runtime-provenance.lock.json'
        lock.write_text('{}')
        manifest = {'schema_version': 1, 'release_id': 'archive-test',
                    'runtime_lock_sha256': self.delivery.sha256(lock),
                    'files': self.delivery.inventory(payload)}
        payload_manifest = payload / 'payload-manifest.json'
        payload_manifest.write_text(json.dumps(manifest))
        source = release / 'source-identity.json'
        source.write_text(json.dumps(self.delivery.source_identity()))
        application = release / 'application-manifest.json'
        shutil.copyfile(self.app_manifest, application)
        installer = release / 'setup.exe'
        installer.write_bytes(b'synthetic test installer')
        (release / '离线安装与迁移说明.md').write_text('Offline test instructions', encoding='utf-8')
        record = {'installer': {'name': installer.name, 'bytes': installer.stat().st_size,
                                'sha256': self.delivery.sha256(installer)},
                  'build': {'payload_manifest_sha256': self.delivery.sha256(payload_manifest),
                            'application_manifest_sha256': self.delivery.sha256(application),
                            'source_identity_sha256': self.delivery.sha256(source)}}
        (release / 'release-manifest.json').write_text(json.dumps(record))
        archive = self.delivery.archive_delivery(release)
        with zipfile.ZipFile(archive) as result:
            self.assertIn('setup.exe', result.namelist())
            self.assertIn('payload/rag-runtime/rag-worker.exe', result.namelist())
            self.assertIn('payload/prerequisites/vc.exe', result.namelist())
            self.assertIn('SHA256SUMS.txt', result.namelist())
            self.assertIn('source-identity.json', result.namelist())
            self.assertIsNone(result.testzip())
        (payload / 'rag-runtime/rag-worker.exe').write_bytes(b'tampered')
        with self.assertRaises(ValueError):
            self.delivery.archive_delivery(release)

    def test_manifest_rejects_windows_path_escapes_case_collisions_and_ads(self):
        for name in ('../bad', 'C:/bad', 'llm-runtime/a:stream', 'llm-runtime/../bad',
                     'llm-runtime/CON', 'llm-runtime/a.', 'llm-runtime/a\\b'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.delivery.validate_relative(name)
        manifest = self.delivery.inventory(self.root)
        manifest['LLM-RUNTIME/A.EXE'] = manifest['llm-runtime/a.exe']
        with self.assertRaises(ValueError):
            self.delivery.verify_inventory(self.root, manifest)

    def test_nsis_capacity_fallback_uses_conservative_uncompressed_bound(self):
        self.assertIsNone(self.delivery.nsis_capacity_reason(1024, 1024))
        self.assertIn('2 GiB', self.delivery.nsis_capacity_reason(2 ** 31, 1024))
        self.assertIn('2 GiB', self.delivery.nsis_capacity_reason(1024, 2 ** 31))

    def test_pinned_upstream_template_preserves_old_runtime_and_user_data(self):
        path = self.root / 'offline.nsi'
        record = self.delivery.offline_template(path)
        text = path.read_text(encoding='utf-8')
        function = text.split('Function PageLeaveReinstall\n', 1)[1].split('FunctionEnd', 1)[0]
        self.assertNotIn('ExecWait', function)
        self.assertNotIn('RmDir /r "$APPDATA', text)
        self.assertNotIn('RmDir /r "$LOCALAPPDATA', text)
        self.assertEqual(text.count('!insertmacro NSIS_HOOK_ACQUIRE_LIFECYCLE'), 2)
        install_body = text.split('Section Install\n', 1)[1].split('SectionEnd', 1)[0]
        self.assertGreaterEqual(install_body.count('!insertmacro NSIS_HOOK_ASSERT_APP_COPY'), 3)
        self.assertIn('ClearErrors\n  WriteUninstaller', install_body)
        self.assertIn('!define MUI_CUSTOMFUNCTION_ABORT OfflineInstallAbort', text)
        self.assertIn('Function OfflineInstallAbort\n  !insertmacro NSIS_HOOK_INSTALLFAILED', text)
        self.assertNotIn('Function .onUserAbort', text)
        self.assertIn('in-place-upgrade', record['policy'])
        self.assertEqual(self.delivery.sha256(path), record['patched_sha256'])

    def test_installer_hook_anchors_manifest_and_uses_only_local_prerequisites(self):
        manifest = self.root / 'payload-manifest.json'
        manifest.write_text('{}')
        hook = self.root / 'hooks.nsh'
        self.delivery.installer_hook(manifest, hook, application_manifest=self.app_manifest)
        text = hook.read_text(encoding='utf-8-sig')
        self.assertIn(hashlib.sha256(manifest.read_bytes()).hexdigest(), text)
        self.assertIn('$EXEDIR\\payload', text)
        self.assertIn('-RunPrerequisites', text)
        self.assertIn(hashlib.sha256(self.app_manifest.read_bytes()).hexdigest(), text)
        self.assertIn('-InstallerProcessId $OfflineInstallerPid -TransactionToken $OfflineTransactionToken', text)
        self.assertIn('$WINDIR\\Sysnative\\WindowsPowerShell', text)
        self.assertNotIn('http', text.lower())

    def test_application_manifest_refresh_tracks_host_mutation_and_rejects_core_mutation(self):
        import re
        script = (TOOLS / 'build-offline-windows.ps1').read_text(encoding='utf-8')
        match = re.search(r'function Update-OfflineApplicationManifest\([^\n]*\) \{\n.*?\n\}', script, re.S)
        self.assertIsNotNone(match)
        release = self.root / 'target/release'
        release.mkdir(parents=True)
        host = release / 'agent-desktop.exe'
        core = release / 'agent-core.exe'
        host.write_bytes(b'before bundle patch')
        core.write_bytes(b'frozen core')
        expected_core = hashlib.sha256(core.read_bytes()).hexdigest()
        helper = self.root / 'refresh.ps1'
        helper.write_text("Import-Module (Join-Path $PSHOME 'Modules/Microsoft.PowerShell.Utility')\n" + match.group(0) + '''
Update-OfflineApplicationManifest $args[0] $args[1] $args[2]
''', encoding='utf-8-sig')
        command = ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                   str(helper), str(self.root), expected_core, str(self.app_manifest)]
        result = subprocess.run(command, capture_output=True)
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode(errors='replace'))
        before = self.app_manifest.read_bytes()
        host.write_bytes(b'after bundle patch')
        result = subprocess.run(command, capture_output=True)
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode(errors='replace'))
        after = json.loads(self.app_manifest.read_text(encoding='utf-8-sig'))
        self.assertNotEqual(before, self.app_manifest.read_bytes())
        self.assertEqual(after['files']['agent-desktop.exe']['sha256'], hashlib.sha256(host.read_bytes()).hexdigest())
        self.assertEqual(after['files']['agent-desktop.exe']['bytes'], host.stat().st_size)
        core.write_bytes(b'unexpected core')
        self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)

    def test_rag_pruning_keeps_dynamic_runtime_data_and_licenses(self):
        spec = importlib.util.spec_from_file_location('rag_runtime_assets', TOOLS / 'rag_runtime_assets.py')
        assets = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(assets)
        for name in ('dnnl.lib', 'torch.pdb', 'torch.h', 'onnxruntime_providers_tensorrt.dll'):
            self.assertTrue(assets.excluded(Path(name)))
        for name in ('torch_cuda.dll', 'cudnn64_9.dll', 'onnxruntime_providers_cuda.dll',
                     'tokenizer.json', 'LICENSE', 'NOTICE', 'model.onnx', 'weights.bin'):
            self.assertFalse(assets.excluded(Path(name)))

    def test_release_sidecar_contract_is_ipc19_schema26(self):
        import re
        root = TOOLS.parent
        protocol = (root / 'services/agent-core/src/agent/protocol.py').read_text(encoding='utf-8')
        storage = (root / 'services/agent-core/src/agent/storage.py').read_text(encoding='utf-8')
        bridge = (root / 'apps/agent-desktop/src-tauri/src/core_bridge.rs').read_text(encoding='utf-8')
        self.assertRegex(protocol, r'PROTOCOL_VERSION\s*=\s*19\b')
        self.assertRegex(storage, r'SCHEMA_VERSION\s*=\s*26\b')
        self.assertRegex(bridge, r'PROTOCOL_VERSION:\s*u64\s*=\s*19\b')
        self.assertRegex(bridge, r'EXPECTED_SCHEMA_VERSION:\s*u64\s*=\s*26\b')
        verifier = (TOOLS / 'verify-python-sidecar.ps1').read_text(encoding='utf-8')
        self.assertIn('$actual.schema_version -ne $expectedSchema', verifier)
        self.assertIn('$actual.protocol_version -ne $expectedProtocol', verifier)

    def test_rag_build_identity_covers_owned_weight_validation_source(self):
        spec = importlib.util.spec_from_file_location('rag_runtime_bundle', TOOLS / 'rag_runtime_bundle.py')
        runtime = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runtime)
        identity = runtime.build_identity(TOOLS.parent, 'cuda')
        self.assertEqual(set(identity), {'services/agent-core/src/agent/rag/worker.py',
                                        'services/agent-core/src/agent/rag/model_profiles.py',
                                        'services/agent-core/src/agent/rag/models.py',
                                        'services/agent-core/requirements-rag-cuda.lock'})
        changed = {'device_bundle': 'cuda', 'build_identity': dict(identity)}
        changed['build_identity']['services/agent-core/src/agent/rag/models.py'] = '0' * 64
        with self.assertRaises(ValueError):
            runtime.verify_build_identity(changed, TOOLS.parent)

    def test_native_verifier_rejects_manifest_substitution_and_payload_tampering(self):
        manifest = {'schema_version': 1, 'release_id': 'test-123',
                    'files': self.delivery.inventory(self.root)}
        path = self.root / 'payload-manifest.json'
        path.write_text(json.dumps(manifest), encoding='utf-8')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        script = TOOLS / 'installer/install-offline-runtime.ps1'
        args = ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(script),
                '-Action', 'Verify', '-PayloadDirectory', str(self.root), '-ManifestSha256', digest]
        result = subprocess.run(args, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        (self.root / 'rag-runtime/rag-worker.exe').write_bytes(b'tampered')
        self.assertNotEqual(subprocess.run(args, capture_output=True).returncode, 0)
        args[-1] = '0' * 64
        self.assertNotEqual(subprocess.run(args, capture_output=True).returncode, 0)

    def invoke_installer(self, install, action='Install', fault='', defer=False, token=None):
        application = self.root / 'application-manifest.json'
        app_files = {name: {'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
                     for name, content in {'agent-desktop.exe': b'complete new host',
                                           'agent-core.exe': b'complete new core'}.items()}
        application.write_text(json.dumps({'schema_version': 1, 'files': app_files}))
        manifest = {'schema_version': 1, 'release_id': 'test-123',
                    'files': self.delivery.inventory(self.root)}
        path = self.root / 'payload-manifest.json'
        path.write_text(json.dumps(manifest), encoding='utf-8')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        args = ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                str(TOOLS / 'installer/install-offline-runtime.ps1'), '-Action', action,
                '-PayloadDirectory', str(self.root), '-InstallDirectory', str(install),
                '-ManifestSha256', digest]
        args += ['-ApplicationManifestPath', str(application), '-ApplicationManifestSha256',
                 hashlib.sha256(application.read_bytes()).hexdigest()]
        if fault:
            args += ['-Fault', fault]
        if defer:
            args += ['-DeferCommit']
            import uuid
            if token is None:
                token = uuid.uuid4().hex
                self.deferred_token = token
        if action in {'Commit', 'Recover'} and token is None:
            token = getattr(self, 'deferred_token', None)
        if token:
            import os
            args += ['-TransactionToken', token, '-InstallerProcessId', str(os.getpid())]
        return subprocess.run(args, capture_output=True, text=True)

    def test_second_deferred_installer_cannot_recover_or_commit_active_transaction(self):
        with tempfile.TemporaryDirectory(prefix='agent-owned-install-') as name:
            install = Path(name)
            result = self.invoke_installer(install, defer=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            journal = install / '.managed-runtime/transaction.json'
            original = journal.read_bytes()
            result = self.invoke_installer(install, defer=True, token='b' * 32)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(journal.read_bytes(), original)
            result = self.invoke_installer(install, action='Commit', token='b' * 32)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(journal.read_bytes(), original)
            result = self.invoke_installer(install, action='Recover')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_deferred_install_restores_app_binaries_when_nsis_copy_fails(self):
        with tempfile.TemporaryDirectory(prefix='agent-owned-install-') as name:
            install = Path(name)
            result = self.invoke_installer(install)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            core = install / 'agent-core.exe'
            core.write_bytes(b'previous core')
            old = (install / 'llm-runtime/a.exe').read_bytes()
            (self.root / 'llm-runtime/a.exe').write_bytes(b'new runtime')
            result = self.invoke_installer(install, defer=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((install / '.managed-runtime/transaction.json').exists())
            core.write_bytes(b'partially replaced core')
            (install / 'agent-desktop.exe').write_bytes(b'new desktop')
            result = self.invoke_installer(install, action='Recover')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(core.read_bytes(), b'previous core')
            self.assertFalse((install / 'agent-desktop.exe').exists())
            self.assertEqual((install / 'llm-runtime/a.exe').read_bytes(), old)
            result = self.invoke_installer(install, defer=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            core.write_bytes(b'complete new core')
            (install / 'agent-desktop.exe').write_bytes(b'complete new host')
            (install / 'uninstall.exe').write_bytes(b'new uninstaller')
            result = self.invoke_installer(install, action='Commit')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(core.read_bytes(), b'complete new core')
            self.assertFalse((install / '.managed-runtime/transaction.json').exists())

    def test_commit_rejects_skipped_core_copy_and_preserves_recovery_journal(self):
        with tempfile.TemporaryDirectory(prefix='agent-owned-install-') as name:
            install = Path(name)
            result = self.invoke_installer(install)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            (install / 'agent-core.exe').write_bytes(b'previous core')
            result = self.invoke_installer(install, defer=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            (install / 'agent-desktop.exe').write_bytes(b'complete new host')
            (install / 'uninstall.exe').write_bytes(b'new uninstaller')
            result = self.invoke_installer(install, action='Commit')
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue((install / '.managed-runtime/transaction.json').exists())
            result = self.invoke_installer(install, action='Recover')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((install / 'agent-core.exe').read_bytes(), b'previous core')

    def test_upgrade_copy_and_activation_failures_restore_previous_runtime(self):
        with tempfile.TemporaryDirectory(prefix='agent-owned-install-') as name:
            install = Path(name)
            result = self.invoke_installer(install)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            old = (install / 'llm-runtime/a.exe').read_bytes()
            (self.root / 'llm-runtime/a.exe').write_bytes(b'new release')
            for fault in ('DuringCopy', 'AfterCopy', 'AfterFirstActivation'):
                result = self.invoke_installer(install, fault=fault)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual((install / 'llm-runtime/a.exe').read_bytes(), old)
                self.assertFalse((install / '.managed-runtime/transaction.json').exists())
            result = self.invoke_installer(install)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((install / 'llm-runtime/a.exe').read_bytes(), b'new release')
            result = self.invoke_installer(install, action='Uninstall')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse((install / 'llm-runtime').exists())
            self.assertFalse((install / '.managed-runtime').exists())

    def test_space_preflight_leaves_no_new_managed_resources(self):
        with tempfile.TemporaryDirectory(prefix='agent-owned-install-') as name:
            install = Path(name)
            result = self.invoke_installer(install, fault='InsufficientSpace')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Insufficient disk space', result.stderr)
            self.assertFalse((install / '.managed-runtime').exists())

    def test_upgrade_refuses_unlisted_previous_files_without_moving_them(self):
        with tempfile.TemporaryDirectory(prefix='agent-owned-install-') as name:
            install = Path(name)
            result = self.invoke_installer(install)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            extra = install / 'llm-runtime/unlisted-user-file.txt'
            extra.write_bytes(b'preserve this unknown file')
            result = self.invoke_installer(install)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Unlisted payload file', result.stderr)
            self.assertEqual(extra.read_bytes(), b'preserve this unknown file')

    def test_concurrent_installer_is_refused_before_mutation(self):
        import time
        with tempfile.TemporaryDirectory(prefix='agent-owned-install-') as name:
            install = Path(name)
            mutex = 'Local\\agent-offline-install-' + hashlib.sha256(str(install).lower().encode()).hexdigest().upper()
            ready = self.root / 'mutex-ready.tmp'
            helper = self.root / 'mutex.ps1'
            helper.write_text('param($name,$ready)\n$m=New-Object Threading.Mutex($true,$name); [IO.File]::WriteAllText($ready,"ready"); Start-Sleep 30')
            process = subprocess.Popen(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                                        '-File', str(helper), mutex, str(ready)])
            try:
                for _ in range(100):
                    if ready.exists():
                        break
                    time.sleep(.05)
                self.assertTrue(ready.exists())
                result = self.invoke_installer(install)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Another installer', result.stderr)
                self.assertFalse((install / '.managed-runtime').exists())
            finally:
                process.terminate()
                process.wait(timeout=10)

    def test_interrupted_activation_is_recovered_on_next_invocation(self):
        with tempfile.TemporaryDirectory(prefix='agent-owned-install-') as name:
            install = Path(name)
            result = self.invoke_installer(install)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            old = (install / 'llm-runtime/a.exe').read_bytes()
            (self.root / 'llm-runtime/a.exe').write_bytes(b'new interrupted release')
            result = self.invoke_installer(install, fault='InterruptAfterFirstActivation')
            self.assertEqual(result.returncode, 97, result.stdout + result.stderr)
            self.assertTrue((install / '.managed-runtime/transaction.json').exists())
            result = self.invoke_installer(install, action='Recover')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((install / 'llm-runtime/a.exe').read_bytes(), old)

    def test_live_managed_executable_refuses_upgrade(self):
        with tempfile.TemporaryDirectory(prefix='agent-owned-install-') as name:
            install = Path(name)
            executable = install / 'busy.exe'
            import shutil
            import os
            shutil.copyfile(Path(os.environ['WINDIR']) / 'System32/timeout.exe', executable)
            # timeout exits when stdin is redirected. A copied PowerShell holds a real managed process.
            shutil.copyfile(Path(os.environ['WINDIR']) / 'System32/WindowsPowerShell/v1.0/powershell.exe', executable)
            process = subprocess.Popen([str(executable), '-NoProfile', '-Command', 'Start-Sleep 30'])
            self.addCleanup(process.terminate)
            try:
                result = self.invoke_installer(install)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Busy PID', result.stderr)
            finally:
                process.terminate()
                process.wait(timeout=10)

    def test_active_file_lock_rolls_back_without_destroying_previous_files(self):
        # FileShare.None exercises the same rename failure as a loaded managed DLL.
        with tempfile.TemporaryDirectory(prefix='agent-owned-install-') as name:
            install = Path(name)
            result = self.invoke_installer(install)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            path = install / 'rag-runtime/rag-worker.exe'
            ready = self.root / 'ready.tmp'
            # Use a script because PowerShell -Command argument parsing differs across versions.
            helper = self.root / 'hold.ps1'
            helper.write_text('param($file,$ready)\n$f=[IO.File]::Open($file,\'Open\',\'Read\',\'None\'); [IO.File]::WriteAllText($ready,\'ready\'); Start-Sleep 30')
            process = subprocess.Popen(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(helper), str(path), str(ready)])
            import time
            try:
                for _ in range(100):
                    if ready.exists():
                        break
                    time.sleep(.05)
                result = self.invoke_installer(install)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual((install / 'llm-runtime/a.exe').read_bytes(), b'llm-runtime/a.exe')
            finally:
                process.terminate()
                process.wait(timeout=10)


if __name__ == '__main__':
    unittest.main()
