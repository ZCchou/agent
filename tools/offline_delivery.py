"""Pinned local runtime preparation and full offline Windows delivery.

Build-time Python only. The installer uses the embedded Windows PowerShell verifier.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
LOCK = Path(__file__).with_name('offline-runtime.lock.json')
COMPONENTS = ('llm-runtime', 'rag-runtime', 'prerequisites')
NSIS_LIMIT = 2 ** 31 - 64 * 1024 * 1024
TAURI_TEMPLATE_SHA256 = 'f56e20d1afca05594d8218b45152455493b51e26ec3422835d1b418e6a2acf2f'


def sha256(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def source_identity() -> dict:
    """Hash text sources with Git-compatible CRLF-to-LF normalization."""
    suffixes = {'.py', '.rs', '.ts', '.tsx', '.css', '.html', '.json', '.toml', '.lock', '.ps1', '.bat', '.nsh', '.cjs', '.mjs', '.spec'}
    sources = set()
    for directory in ('apps/agent-desktop/src', 'apps/agent-desktop/src-tauri/src',
                      'services/agent-core/src', 'tools'):
        sources.update(path for path in (ROOT / directory).rglob('*')
                       if path.is_file() and path.suffix in suffixes)
    for directory in ('apps/agent-desktop', 'apps/agent-desktop/src-tauri', 'services/agent-core'):
        sources.update(path for path in (ROOT / directory).iterdir()
                       if path.is_file() and path.suffix in suffixes)
    return {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest()
            for path in sorted(sources)}


def validate_relative(name: str) -> str:
    if not isinstance(name, str) or not name or '\\' in name or ':' in name:
        raise ValueError('Invalid Windows relative payload path')
    path = PurePosixPath(name)
    if path.is_absolute() or str(path) != name:
        raise ValueError('Noncanonical relative payload path')
    for part in path.parts:
        if part in {'.', '..'} or part.endswith(('.', ' ')) or re.search(r'[<>"|?*\x00-\x1f]', part) \
                or re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?', part, re.I):
            raise ValueError('Unsafe Windows payload path')
    return name


def assert_unlinked(path: Path) -> None:
    path = Path(path).absolute()
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink() or (hasattr(ancestor, 'is_junction') and ancestor.is_junction()):
            raise ValueError('Linked payload ancestor')


def safe_tree(root: Path):
    root = Path(root).absolute()
    assert_unlinked(root)
    for directory, directories, names in os.walk(root, followlinks=False):
        for name in directories + names:
            path = Path(directory) / name
            if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
                raise ValueError('Linked payload entry')
            if not path.resolve().is_relative_to(root.resolve()):
                raise ValueError('Payload escapes its owned root')
        for name in names:
            path = Path(directory) / name
            if path.stat().st_nlink != 1:
                raise ValueError('Hard-linked payload entry')
            yield validate_relative(path.relative_to(root).as_posix()), path


def inventory(root: Path) -> dict:
    return {name: {'bytes': path.stat().st_size, 'sha256': sha256(path)}
            for name, path in sorted(safe_tree(root)) if name != 'payload-manifest.json'}


def verify_inventory(root: Path, files: dict) -> None:
    if not isinstance(files, dict) or not files:
        raise ValueError('Missing payload inventory')
    for name, record in files.items():
        validate_relative(name)
        if not isinstance(record, dict) or not isinstance(record.get('bytes'), int) \
                or record['bytes'] < 0 or not re.fullmatch('[a-f0-9]{64}', record.get('sha256', '')):
            raise ValueError('Invalid payload inventory record')
    if len({name.casefold() for name in files}) != len(files):
        raise ValueError('Case-colliding payload inventory')
    if inventory(root) != files:
        raise ValueError('Payload inventory, size or SHA256 mismatch')


def nsis_capacity_reason(total_bytes: int, largest_file: int) -> str | None:
    if max(total_bytes, largest_file) >= NSIS_LIMIT:
        return ('NSIS documents a 2 GiB data-block/executable size limit. '
                'The conservative uncompressed input bound exceeds that capacity. '
                'Use the complete ZIP with external verified payload. '
                'https://nsis.sourceforge.io/Features')
    return None


def check_file(path: Path, record: dict) -> None:
    if path.is_symlink() or not path.is_file() or path.stat().st_size != record['bytes'] \
            or sha256(path) != record['sha256']:
        raise ValueError(f'Pinned source mismatch: {path.name}')


def prepare_llm(cache: Path, target: Path) -> dict:
    """Reconstruct official archives with no network or user-selected executable."""
    lock = json.loads(LOCK.read_text(encoding='utf-8'))
    assert_unlinked(cache)
    assert_unlinked(target)
    if target.exists():
        raise ValueError('Use a new llm-runtime output directory')
    for item in lock['archives']:
        check_file(cache / 'downloads' / item['name'], item)
    for name, record in lock['licenses'].items():
        check_file(cache / 'engines' / name, record)
    target.mkdir(parents=True)
    try:
        for item in lock['archives']:
            destination = target / item['backend']
            destination.mkdir(exist_ok=True)
            with zipfile.ZipFile(cache / 'downloads' / item['name']) as archive:
                for member in archive.infolist():
                    name = member.filename.rstrip('/')
                    validate_relative(name)
                    if member.external_attr >> 16 & 0o170000 == 0o120000:
                        raise ValueError('Linked archive member')
                    archive.extract(member, destination)
        for name in lock['licenses']:
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(cache / 'engines' / name, destination)
        manifest = {'schema_version': 1, 'protocol_version': 1, 'engines': {},
                    'files': {}, 'licenses': list(lock['licenses'])}
        for backend in ('cpu', 'cuda', 'vulkan'):
            files = {f'{backend}/{name}': sha256(path) for name, path in safe_tree(target / backend)}
            output = subprocess.run([str(target / backend / 'llama-server.exe'), '--version'],
                                    capture_output=True, text=True, timeout=30)
            version_output = output.stdout + output.stderr
            if output.returncode or lock['source_commit'][:7] not in version_output \
                    or lock['version'].lstrip('b') not in version_output:
                raise ValueError(f'Native source identity mismatch for {backend}')
            manifest['engines'][backend] = {
                'executable': f'{backend}/llama-server.exe', 'version': lock['version'],
                'source_commit': lock['source_commit'],
                'build': {'source': 'official ggml-org/llama.cpp release',
                          'stable_version': lock['stable_version'], 'backend': backend,
                          'native_version_output': version_output.strip(),
                          'archives': [item for item in lock['archives'] if item['backend'] == backend],
                          'build_flags': 'Not recorded by upstream release metadata'},
                'files': files}
        manifest['files'] = {name: sha256(target / name) for name in lock['licenses'] if '/' not in name}
        write_json(target / 'runtime.json', manifest)
        verify_llm(target)
        return manifest
    except BaseException:
        # Keep the failed output for diagnosis. Never delete a caller-selected directory.
        raise


def verify_llm(root: Path) -> dict:
    manifest = json.loads((root / 'runtime.json').read_text(encoding='utf-8'))
    lock = json.loads(LOCK.read_text(encoding='utf-8'))
    if manifest.get('schema_version') != 1 or manifest.get('protocol_version') != 1 \
            or set(manifest.get('engines', {})) != {'cpu', 'cuda', 'vulkan'}:
        raise ValueError('Incomplete managed engine manifest')
    files = dict(manifest.get('files', {}))
    for backend, record in manifest['engines'].items():
        if record.get('source_commit') != lock['source_commit'] or record.get('version') != lock['version'] \
                or record.get('executable') != f'{backend}/llama-server.exe' or not record.get('build'):
            raise ValueError('Unpinned managed engine identity')
        files.update(record['files'])
    observed = {name: sha256(path) for name, path in safe_tree(root) if name != 'runtime.json'}
    if observed != files or observed != lock.get('runtime_files') \
            or manifest.get('licenses') != list(lock['licenses']):
        raise ValueError('Managed engine inventory mismatch')
    return manifest


def make_payload(llm: Path, rag: Path, prereqs: Path, target: Path, release_id: str) -> Path:
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from rag_runtime_bundle import verify, verify_build_identity
    assert_unlinked(target)
    assert_unlinked(prereqs)
    # Reject a linked source root before the inference verifier normalizes it.
    for _ in safe_tree(rag):
        pass
    verify_llm(llm)
    runtime = verify(rag, 'cuda')
    verify_build_identity(runtime, ROOT)
    lock = json.loads(LOCK.read_text(encoding='utf-8'))
    for name, record in lock['prerequisites'].items():
        check_file(prereqs / name, record)
    if target.exists() or not re.fullmatch('[A-Za-z0-9._-]{1,100}', release_id):
        raise ValueError('Use a new payload output directory and safe release ID')
    target.mkdir(parents=True)
    shutil.copytree(llm, target / 'llm-runtime')
    shutil.copytree(rag, target / 'rag-runtime')
    (target / 'prerequisites').mkdir()
    for name in lock['prerequisites']:
        shutil.copyfile(prereqs / name, target / 'prerequisites' / name)
    manifest = {'schema_version': 1, 'release_id': release_id,
                'files': inventory(target), 'runtime_lock_sha256': sha256(LOCK),
                'rag_build_identity': runtime['build_identity'],
                'prerequisites': lock['prerequisites']}
    write_json(target / 'payload-manifest.json', manifest)
    return target / 'payload-manifest.json'


def installer_hook(manifest: Path, destination: Path, embedded_payload: bool = False, *,
                   application_manifest: Path, run_prerequisites: bool = True,
                   include_ocr: bool = False) -> Path:
    """Bake the external payload identity into the installer itself."""
    digest = sha256(manifest)
    application_digest = sha256(application_manifest)
    prerequisite_argument = '-RunPrerequisites' if run_prerequisites else ''
    verifier = Path(__file__).parent / 'installer/install-offline-runtime.ps1'
    # NSIS uses its own escape syntax, including $ expansion inside quoted paths.
    def nsis_path(path):
        return str(path.resolve()).replace('$', '$$').replace('"', '$\\"')
    payload = '$EXEDIR\\payload'
    include = ''
    if embedded_payload:
        payload = '$PLUGINSDIR\\payload'
        record = json.loads(manifest.read_text(encoding='utf-8-sig'))
        required_bytes = sum(item['bytes'] for item in record['files'].values()) + manifest.stat().st_size
        include = f'''  nsExec::ExecToLog '\"$WINDIR\\Sysnative\\WindowsPowerShell\\v1.0\\powershell.exe\" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File \".\\i.ps1\" -Action Preflight -PayloadDirectory \"{payload}\" -InstallDirectory \"$INSTDIR\" -RequiredBytes {required_bytes}'
  Pop $0
  ${{If}} $0 != 0
    MessageBox MB_OK|MB_ICONSTOP "Insufficient space or busy managed processes. See installer details." /SD IDOK
    SetErrorLevel 1
    Abort
  ${{EndIf}}
  SetOutPath "$PLUGINSDIR\\payload"
  File /r "{nsis_path(manifest.parent)}\\*"
'''
    content = f'''Var OfflineLifecycleMutex
Var OfflineInstallerPid
Var OfflineTransactionToken

!macro NSIS_HOOK_ACQUIRE_LIFECYCLE
  System::Call 'kernel32::CreateMutexW(p 0, i 1, w "Global\\io.github.zcchou.agent.offline-installer-v1") p.r0 ?e'
  Pop $1
  StrCpy $OfflineLifecycleMutex $0
  ${{If}} $OfflineLifecycleMutex == 0
  ${{OrIf}} $1 == 183
    System::Call 'kernel32::CloseHandle(p r0)'
    SetErrorLevel 1
    Quit
  ${{EndIf}}
  System::Call 'kernel32::GetCurrentProcessId() i.r0'
  StrCpy $OfflineInstallerPid $0
  System::Alloc 16
  Pop $0
  System::Call 'ole32::CoCreateGuid(p r0)'
  System::Call 'ole32::StringFromGUID2(p r0, w.r1, i 39)'
  System::Free $0
  StrCpy $OfflineTransactionToken $1
!macroend

!macro NSIS_HOOK_PREINSTALL
  InitPluginsDir
  SetOutPath "$PLUGINSDIR"
  File /oname=i.ps1 "{nsis_path(verifier)}"
  File /oname=a.json "{nsis_path(application_manifest)}"
{include}  SetOutPath "$PLUGINSDIR"
  nsExec::ExecToLog '\"$WINDIR\\Sysnative\\WindowsPowerShell\\v1.0\\powershell.exe\" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File \".\\i.ps1\" -Action Install -PayloadDirectory \"{payload}\" -InstallDirectory \"$INSTDIR\" -ManifestSha256 {digest} {prerequisite_argument} -DeferCommit -InstallerProcessId $OfflineInstallerPid -TransactionToken $OfflineTransactionToken -ApplicationManifestPath \".\\a.json\" -ApplicationManifestSha256 {application_digest}'
  Pop $0
  ${{If}} $0 == 3010
    SetRebootFlag true
  ${{ElseIf}} $0 != 0
    MessageBox MB_OK|MB_ICONSTOP "Offline runtime verification or installation failed. See installer details. Previous runtimes were preserved." /SD IDOK
    SetErrorLevel 1
    Abort
  ${{EndIf}}
  SetOutPath "$INSTDIR"
!macroend

!macro NSIS_HOOK_POSTINSTALL
  SetOutPath "$PLUGINSDIR"
  nsExec::ExecToLog '\"$WINDIR\\Sysnative\\WindowsPowerShell\\v1.0\\powershell.exe\" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File \".\\i.ps1\" -Action Commit -InstallDirectory \"$INSTDIR\" -ManifestSha256 {digest} -InstallerProcessId $OfflineInstallerPid -TransactionToken $OfflineTransactionToken -ApplicationManifestSha256 {application_digest}'
  Pop $0
  ${{If}} $0 != 0
    !insertmacro NSIS_HOOK_INSTALLFAILED
    MessageBox MB_OK|MB_ICONSTOP "Offline activation did not commit. Close managed processes and retry the same installer. Recovery state was preserved." /SD IDOK
    SetErrorLevel 1
    Abort
  ${{EndIf}}
  SetOutPath "$INSTDIR"
  ${{If}} ${{RebootFlag}}
    SetErrorLevel 3010
  ${{EndIf}}
!macroend

!macro NSIS_HOOK_INSTALLFAILED
  ${{If}} ${{FileExists}} "$INSTDIR\\.managed-runtime\\transaction.json"
    InitPluginsDir
    SetOutPath "$PLUGINSDIR"
    File /oname=i.ps1 "{nsis_path(verifier)}"
    nsExec::ExecToLog '\"$WINDIR\\Sysnative\\WindowsPowerShell\\v1.0\\powershell.exe\" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File \".\\i.ps1\" -Action Recover -InstallDirectory \"$INSTDIR\" -InstallerProcessId $OfflineInstallerPid -TransactionToken $OfflineTransactionToken'
    Pop $0
  ${{EndIf}}
!macroend

!macro NSIS_HOOK_ASSERT_APP_COPY
  ${{If}} ${{Errors}}
    !insertmacro NSIS_HOOK_INSTALLFAILED
    SetErrorLevel 1
    Abort
  ${{EndIf}}
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  ClearErrors
  ${{GetOptions}} $CMDLINE "/UPDATE" $R9
  ${{IfNot}} ${{Errors}}
    ; Tauri's upgrade uninstaller preserves the previous verified runtimes.
    ; The new installer performs journaled activation and rollback.
  ${{Else}}
  InitPluginsDir
  SetOutPath "$PLUGINSDIR"
  File /oname=i.ps1 "{nsis_path(verifier)}"
  nsExec::ExecToLog '\"$WINDIR\\Sysnative\\WindowsPowerShell\\v1.0\\powershell.exe\" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File \".\\i.ps1\" -Action Uninstall -InstallDirectory \"$INSTDIR\"'
  Pop $0
  ${{If}} $0 != 0
    MessageBox MB_OK|MB_ICONSTOP "Close managed processes and retry. Modified or unowned runtime resources require inspection." /SD IDOK
    SetErrorLevel 1
    Abort
  ${{EndIf}}
  ${{EndIf}}
!macroend
'''
    if include_ocr:
        content = content.replace('-Action ', '-IncludeOcr -Action ')
    destination.write_text(content, encoding='utf-8-sig')
    return destination


def offline_template(destination: Path) -> dict:
    """Patch the installed, lockfile-pinned upstream template without a download.

    Template extraction is hash-pinned and fails closed on a CLI update. Keeping
    this as a checked patch avoids maintaining a second 33 KB installer template.
    """
    package = ROOT / 'apps/agent-desktop/node_modules/@tauri-apps/cli-win32-x64-msvc'
    binary = package / 'cli.win32-x64-msvc.node'
    data = binary.read_bytes()
    match = re.search(rb'Unicode true\r*\nManifestDPIAware true', data)
    if match is None:
        raise ValueError('Pinned Tauri NSIS template not found')
    start = match.start()
    boundary = data.find(b'\0', start)
    end = data.rfind(b'FunctionEnd', start, boundary) + len(b'FunctionEnd')
    raw = data[start:end].replace(b'\r', b'')
    if hashlib.sha256(raw).hexdigest() != TAURI_TEMPLATE_SHA256:
        raise ValueError('Tauri NSIS template changed. Review and repin the offline lifecycle patch.')
    source = raw.decode('utf-8')
    version_compare = '  nsis_tauri_utils::SemverCompare "${VERSION}" $R0\n  Pop $R0'
    if source.count(version_compare) != 1:
        raise ValueError('Unexpected upstream version comparison')
    source = source.replace(version_compare, version_compare + '''
  ; Offline delivery always upgrades in place, after payload verification.
  ${If} $WixMode = 1
    MessageBox MB_OK|MB_ICONSTOP "This offline installer requires an NSIS installation. Inspect the existing MSI installation before migration." /SD IDOK
    SetErrorLevel 1
    Quit
  ${EndIf}
  ${If} $R0 = -1
    MessageBox MB_OK|MB_ICONSTOP "Offline installer downgrades are disabled." /SD IDOK
    SetErrorLevel 1
    Quit
  ${EndIf}
  Abort
''')
    source, count = re.subn(r'Function PageLeaveReinstall\n.*?FunctionEnd',
                            'Function PageLeaveReinstall\n  ; Runtime activation owns upgrade rollback.\n  Return\nFunctionEnd',
                            source, count=1, flags=re.S)
    if count != 1:
        raise ValueError('Unexpected upstream reinstall lifecycle')
    for name, body in [('un.ConfirmShow', '  Return'),
                       ('un.ConfirmLeave', '  StrCpy $DeleteAppDataCheckboxState 0')]:
        source, count = re.subn(r'Function ' + re.escape(name) + r'[^\n]*\n.*?FunctionEnd',
                                f'Function {name}\n{body}\nFunctionEnd', source, count=1, flags=re.S)
        if count != 1:
            raise ValueError('Unexpected upstream user-data lifecycle')
    for line in ('    RmDir /r "$APPDATA\\${BUNDLEID}"',
                 '    RmDir /r "$LOCALAPPDATA\\${BUNDLEID}"'):
        if source.count(line) != 1:
            raise ValueError('Unexpected upstream app-data deletion')
        source = source.replace(line, '    ; User data and model libraries are preserved.')
    if 'Function .onInstFailed' in source or 'Function .onUserAbort' in source:
        raise ValueError('Unexpected upstream failure callbacks')
    if source.count('!include MUI2.nsh') != 1 or 'MUI_CUSTOMFUNCTION_ABORT' in source:
        raise ValueError('Unexpected upstream MUI abort lifecycle')
    source = source.replace('!include MUI2.nsh',
                            '!define MUI_CUSTOMFUNCTION_ABORT OfflineInstallAbort\n!include MUI2.nsh')
    for function in ('.onInit', 'un.onInit'):
        source, count = re.subn(r'(Function ' + re.escape(function) + r'\n.*?)(\nFunctionEnd)',
                                r'\1\n  !insertmacro NSIS_HOOK_ACQUIRE_LIFECYCLE\2',
                                source, count=1, flags=re.S)
        if count != 1:
            raise ValueError('Unexpected upstream installer initialization')
    install_start = source.index('Section Install\n')
    install_end = source.index('SectionEnd', install_start)
    body = source[install_start:install_end]
    body, count = re.subn(r'(?m)^([ \t]*)(File\b[^\n]*|WriteUninstaller\b[^\n]*)$',
                          r'\1ClearErrors\n\1\2\n\1!insertmacro NSIS_HOOK_ASSERT_APP_COPY', body)
    if count < 3:
        raise ValueError('Unexpected upstream application copy boundaries')
    source = source[:install_start] + body + source[install_end:]
    source += '''
Function .onInstFailed
  !insertmacro NSIS_HOOK_INSTALLFAILED
FunctionEnd
Function OfflineInstallAbort
  !insertmacro NSIS_HOOK_INSTALLFAILED
FunctionEnd
'''
    destination.write_text(source + '\n', encoding='utf-8')
    return {'upstream': 'https://github.com/tauri-apps/tauri/blob/dev/crates/tauri-bundler/src/bundle/windows/nsis/installer.nsi',
            'cli_version': json.loads((package / 'package.json').read_text())['version'],
            'upstream_normalized_sha256': TAURI_TEMPLATE_SHA256, 'patched_sha256': sha256(destination),
            'policy': 'in-place-upgrade-preserve-user-data-v1'}


def archive_delivery(root: Path, create_zip: bool = True) -> Path:
    manifest_path = root / 'release-manifest.json'
    release = json.loads(manifest_path.read_text(encoding='utf-8-sig'))
    payload = root / 'payload'
    payload_manifest = payload / 'payload-manifest.json'
    if sha256(payload_manifest) != release['build']['payload_manifest_sha256']:
        raise ValueError('Payload manifest differs from the installer release identity')
    source_record = root / 'source-identity.json'
    if sha256(source_record) != release['build']['source_identity_sha256'] \
            or json.loads(source_record.read_text(encoding='utf-8')) != source_identity():
        raise ValueError('Source changed after the prepared release freeze')
    application_record = root / 'application-manifest.json'
    if sha256(application_record) != release['build']['application_manifest_sha256']:
        raise ValueError('Application identities differ from this installer release')
    payload_record = json.loads(payload_manifest.read_text(encoding='utf-8'))
    if sha256(root / 'runtime-provenance.lock.json') != payload_record['runtime_lock_sha256']:
        raise ValueError('Runtime provenance lock differs from this release payload')
    verify_inventory(payload, payload_record['files'])
    installer = root / validate_relative(release['installer']['name'])
    check_file(installer, release['installer'])
    included = [installer, manifest_path, source_record, application_record, root / 'runtime-provenance.lock.json',
                root / '离线安装与迁移说明.md']
    included += [path for _, path in safe_tree(payload)]
    checksums = root / 'SHA256SUMS.txt'
    checksums.write_text(''.join(f'{sha256(path)}  {path.relative_to(root).as_posix()}\n'
                                 for path in sorted(included)), encoding='utf-8')
    included.append(checksums)
    if not create_zip:
        print(json.dumps({'delivery': 'single-exe', 'installer': str(installer),
                          'bytes': installer.stat().st_size, 'sha256': sha256(installer)}))
        return installer
    archive_path = root.with_name(root.name + '-full-offline.zip')
    if archive_path.exists():
        raise ValueError('Immutable offline ZIP output already exists')
    with zipfile.ZipFile(archive_path, 'x', compression=zipfile.ZIP_DEFLATED,
                         compresslevel=6, allowZip64=True) as archive:
        for path in sorted(included):
            archive.write(path, path.relative_to(root).as_posix())
    # CRC-decompress every member with bounded memory before accepting the ZIP.
    with zipfile.ZipFile(archive_path) as archive:
        failed = archive.testzip()
        if failed is not None:
            raise ValueError(f'ZIP verification failed: {failed}')
    print(json.dumps({'zip': str(archive_path), 'bytes': archive_path.stat().st_size,
                      'sha256': sha256(archive_path), 'files': len(included)}))
    archive_path.with_suffix('.zip.sha256').write_text(f'{sha256(archive_path)}  {archive_path.name}\n', encoding='ascii')
    return archive_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='action', required=True)
    prepare = subs.add_parser('prepare-llm')
    prepare.add_argument('--cache', type=Path, required=True)
    prepare.add_argument('--destination', type=Path, required=True)
    payload = subs.add_parser('prepare-payload')
    for name in ('llm', 'rag', 'prereqs', 'destination'):
        payload.add_argument('--' + name, type=Path, required=True)
    payload.add_argument('--release-id', required=True)
    verify = subs.add_parser('verify-payload')
    verify.add_argument('directory', type=Path)
    hook = subs.add_parser('installer-hook')
    hook.add_argument('--manifest', type=Path, required=True)
    hook.add_argument('--destination', type=Path, required=True)
    hook.add_argument('--embedded', action='store_true')
    hook.add_argument('--application-manifest', type=Path, required=True)
    archive = subs.add_parser('archive')
    archive.add_argument('directory', type=Path)
    archive.add_argument('--checksums-only', action='store_true')
    template = subs.add_parser('installer-template')
    template.add_argument('--destination', type=Path, required=True)
    identity = subs.add_parser('source-identity')
    identity.add_argument('--destination', type=Path, required=True)
    identity.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    if args.action == 'prepare-llm':
        prepare_llm(args.cache, args.destination)
    elif args.action == 'prepare-payload':
        path = make_payload(args.llm, args.rag, args.prereqs, args.destination, args.release_id)
        files = json.loads(path.read_text(encoding='utf-8'))['files']
        print(json.dumps({'manifest_sha256': sha256(path), 'files': len(files),
                          'bytes': sum(item['bytes'] for item in files.values()),
                          'fallback_reason': nsis_capacity_reason(sum(item['bytes'] for item in files.values()),
                                                                 max(item['bytes'] for item in files.values()))}))
    elif args.action == 'installer-hook':
        installer_hook(args.manifest, args.destination, args.embedded, application_manifest=args.application_manifest)
    elif args.action == 'archive':
        archive_delivery(args.directory, not args.checksums_only)
    elif args.action == 'installer-template':
        print(json.dumps(offline_template(args.destination)))
    elif args.action == 'source-identity':
        observed = source_identity()
        if args.verify:
            if json.loads(args.destination.read_text(encoding='utf-8')) != observed:
                raise ValueError('Source changed after the prepared release freeze')
        else:
            write_json(args.destination, observed)
    else:
        manifest = json.loads((args.directory / 'payload-manifest.json').read_text(encoding='utf-8'))
        verify_inventory(args.directory, manifest['files'])
        print(json.dumps({'verified': True, 'release_id': manifest['release_id']}))


if __name__ == '__main__':
    main()
