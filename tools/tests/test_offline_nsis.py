"""Opt-in real NSIS lifecycle regressions using tiny, synthetic local payloads."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

from tools.offline_delivery import installer_hook, inventory, offline_template

ROOT = Path(__file__).resolve().parents[2]
COMPILER = Path('C:/Users/Administrator/AppData/Local/tauri/NSIS/makensis.exe')


@unittest.skipUnless(os.environ.get('AGENT_TEST_REAL_NSIS') == '1', 'Opt-in native NSIS lifecycle tests')
class NsisLifecycleTests(unittest.TestCase):
    def setUp(self):
        parent = Path(os.environ.get('AGENT_TEST_ARTIFACT_DIR', str(ROOT / '.artifacts/local-runtime-work/task5-nsis-lifecycle-tests')))
        parent.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='owned-', dir=parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.install = self.root / 'installed'
        self.payload = self.root / 'payload'
        for name in ('llm-runtime/a.exe', 'rag-runtime/rag-worker.exe'):
            path = self.payload / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'previous runtime')
        self.write_manifest()
        command = ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                   str(ROOT / 'tools/installer/install-offline-runtime.ps1'), '-Action', 'Install',
                   '-PayloadDirectory', str(self.payload), '-InstallDirectory', str(self.install),
                   '-ManifestSha256', hashlib.sha256((self.payload / 'payload-manifest.json').read_bytes()).hexdigest()]
        result = subprocess.run(command, capture_output=True, text=True, timeout=40)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for name in ('agent-core.exe', 'agent-desktop.exe', 'uninstall.exe'):
            (self.install / name).write_bytes(('previous ' + name).encode())
        for path in (self.payload / 'llm-runtime/a.exe', self.payload / 'rag-runtime/rag-worker.exe'):
            path.write_bytes(b'new runtime')
        self.write_manifest()
        app_files = {}
        for name in ('agent-core.exe', 'agent-desktop.exe'):
            content = ('new ' + name).encode()
            (self.root / name).write_bytes(content)
            app_files[name] = {'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
        self.application = self.root / 'application-manifest.json'
        self.application.write_text(json.dumps({'schema_version': 1, 'files': app_files}))
        self.hook = self.root / 'hooks.nsh'
        installer_hook(self.payload / 'payload-manifest.json', self.hook,
                       application_manifest=self.application, run_prerequisites=False)

    def write_manifest(self):
        (self.payload / 'payload-manifest.json').write_text(json.dumps({
            'schema_version': 1, 'release_id': 'real-nsis-fixture', 'files': inventory(self.payload)}))

    def compile(self, name, mode='normal'):
        executable = self.root / (name + '.exe')
        pause = 'Sleep 30000' if mode == 'paused' else ''
        core_copy = f'File /oname=agent-core.exe "{self.root / "agent-core.exe"}"'
        if mode == 'skipped-core':
            core_copy = 'SetOverwrite off\n' + core_copy + '\nSetOverwrite on'
        if mode == 'copy-error':
            core_copy += '\nSetErrors'
        script = f'''Unicode true
!include LogicLib.nsh
!include FileFunc.nsh
!include "{self.hook}"
Name "agent tiny lifecycle regression"
OutFile "{executable}"
InstallDir "{self.install}"
RequestExecutionLevel user
Function .onInit
  !insertmacro NSIS_HOOK_ACQUIRE_LIFECYCLE
FunctionEnd
Function un.onInit
  !insertmacro NSIS_HOOK_ACQUIRE_LIFECYCLE
FunctionEnd
Function .onInstFailed
  !insertmacro NSIS_HOOK_INSTALLFAILED
FunctionEnd
Function .onUserAbort
  !insertmacro NSIS_HOOK_INSTALLFAILED
FunctionEnd
Section Install
  !insertmacro NSIS_HOOK_PREINSTALL
  {pause}
  ClearErrors
  File /oname=agent-desktop.exe "{self.root / 'agent-desktop.exe'}"
  !insertmacro NSIS_HOOK_ASSERT_APP_COPY
  ClearErrors
  {core_copy}
  !insertmacro NSIS_HOOK_ASSERT_APP_COPY
  ClearErrors
  WriteUninstaller "$INSTDIR\\uninstall.exe"
  !insertmacro NSIS_HOOK_ASSERT_APP_COPY
  !insertmacro NSIS_HOOK_POSTINSTALL
SectionEnd
Section Uninstall
  !insertmacro NSIS_HOOK_PREUNINSTALL
SectionEnd
'''
        source = self.root / (name + '.nsi')
        source.write_text(script, encoding='utf-8-sig')
        result = subprocess.run([str(COMPILER), '/V2', str(source)], capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode(errors='replace'))
        return executable

    def assert_previous_restored(self):
        self.assertEqual((self.install / 'agent-core.exe').read_bytes(), b'previous agent-core.exe')
        self.assertEqual((self.install / 'agent-desktop.exe').read_bytes(), b'previous agent-desktop.exe')
        self.assertEqual((self.install / 'llm-runtime/a.exe').read_bytes(), b'previous runtime')
        self.assertEqual((self.install / 'rag-runtime/rag-worker.exe').read_bytes(), b'previous runtime')
        self.assertFalse((self.install / '.managed-runtime/transaction.json').exists())

    def test_current_tauri_mui_template_compiles_with_recovery_abort_callback(self):
        import re
        import shutil
        target = Path(os.environ.get('CARGO_TARGET_DIR', str(ROOT / 'apps/agent-desktop/src-tauri/target')))
        rendered = target / 'release/nsis/x64/installer.nsi'
        self.assertTrue(rendered.is_file(), 'Render the current pinned Tauri installer before this regression')
        for name in ('utils.nsh', 'FileAssociation.nsh'):
            shutil.copyfile(rendered.parent / name, self.root / name)
        template = self.root / 'patched-template.nsi'
        offline_template(template)
        template_text = template.read_text(encoding='utf-8')
        self.assertIn('!define MUI_CUSTOMFUNCTION_ABORT OfflineInstallAbort', template_text)
        text = rendered.read_text(encoding='utf-8-sig')
        text = re.sub(r'(?m)^!define OUTFILE .*$', lambda _: f'!define OUTFILE "{self.root / "mui.exe"}"', text)
        text = re.sub(r'(?m)^!define MAINBINARYSRCPATH .*$', lambda _: f'!define MAINBINARYSRCPATH "{self.root / "agent-desktop.exe"}"', text)
        text = re.sub(r'(?m)^!include ".*offline-hooks.nsh"$', lambda _: f'!include "{self.hook}"', text)
        text = re.sub(r'(?m)^(\s*File /a "/oname=agent-core.exe") .*$',
                      lambda match: match.group(1) + f' "{self.root / "agent-core.exe"}"', text)
        text = text.replace('!define MUI_CUSTOMFUNCTION_ABORT OfflineInstallAbort\n', '')
        text = text.replace('Function OfflineInstallAbort\n', 'Function .onUserAbort\n')
        source = self.root / 'mui-current-template.nsi'
        source.write_text(text, encoding='utf-8-sig')
        failed = subprocess.run([str(COMPILER), '/V2', str(source)], capture_output=True, timeout=30)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn(b'onUserAbort', failed.stdout + failed.stderr)
        text = text.replace('!include MUI2.nsh', '!define MUI_CUSTOMFUNCTION_ABORT OfflineInstallAbort\n!include MUI2.nsh')
        text = text.replace('Function .onUserAbort\n', 'Function OfflineInstallAbort\n')
        source.write_text(text, encoding='utf-8-sig')
        passed = subprocess.run([str(COMPILER), '/V2', str(source)], capture_output=True, timeout=30)
        self.assertEqual(passed.returncode, 0, (passed.stdout + passed.stderr).decode(errors='replace'))
        self.assertTrue((self.root / 'mui.exe').is_file())

    def test_actual_skipped_core_extraction_fails_commit_and_recovers(self):
        executable = self.compile('skipped-core', 'skipped-core')
        result = subprocess.run([str(executable), '/S'], timeout=45)
        self.assertNotEqual(result.returncode, 0)
        self.assert_previous_restored()

    def test_actual_nsis_copy_error_flag_recovers_before_postinstall(self):
        executable = self.compile('copy-error', 'copy-error')
        result = subprocess.run([str(executable), '/S'], timeout=45)
        self.assertNotEqual(result.returncode, 0)
        self.assert_previous_restored()

    def test_real_overlap_refused_and_dead_owner_recovers_on_retry(self):
        paused = self.compile('paused', 'paused')
        normal = self.compile('normal')
        process = subprocess.Popen([str(paused), '/S'])
        journal = self.install / '.managed-runtime/transaction.json'
        try:
            for _ in range(200):
                if journal.exists():
                    try:
                        record = json.loads(journal.read_text(encoding='utf-8-sig'))
                        if record['phase'] == 'activated':
                            break
                    except (ValueError, OSError):
                        pass
                if process.poll() is not None:
                    self.fail(f'First installer exited before deferred pause, exit {process.returncode}')
                time.sleep(.05)
            self.assertEqual(record['phase'], 'activated')
            before = journal.read_bytes()
            second = subprocess.run([str(normal), '/S'], timeout=15)
            self.assertNotEqual(second.returncode, 0)
            self.assertEqual(journal.read_bytes(), before)
            self.assertEqual((self.install / 'llm-runtime/a.exe').read_bytes(), b'new runtime')
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)
        result = subprocess.run([str(normal), '/S'], timeout=45)
        self.assertEqual(result.returncode, 0)
        self.assertEqual((self.install / 'agent-core.exe').read_bytes(), b'new agent-core.exe')
        self.assertFalse(journal.exists())
