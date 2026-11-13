"""Build a manually updated OCR preview installer with local, verified ZIP assets."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import zipfile

from offline_delivery import installer_hook, sha256, validate_relative, write_json, check_file, LOCK

ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = ('llm-runtime', 'rag-runtime', 'ocr-runtime', 'ocr-model-package')
APPLICATION = ('agent-desktop.exe', 'agent-core.exe', 'agent_desktop_lib.dll')
ASSET_LIMIT = 1700 * 1024 ** 2
UNIFIED_APPLICATION = (*APPLICATION, 'agent-updater.exe', 'application.json')
UNIFIED_ASSET_LIMIT = 1500 * 1024 ** 2


def plan_volumes(records, limit=ASSET_LIMIT):
    groups, current, size = [], [], 0
    for record in records:
        if record['bytes'] > limit:
            raise ValueError(f"A single file exceeds the offline asset bound: {record['name']}")
        if current and size + record['bytes'] > limit:
            groups.append(current)
            current, size = [], 0
        current.append(record)
        size += record['bytes']
    if current:
        groups.append(current)
    return groups


def is_public_file(name, *, unified=False):
    relative = Path(name)
    application = UNIFIED_APPLICATION if unified else APPLICATION
    components = (*COMPONENTS, 'office-runtime') if unified else COMPONENTS
    return (name in application or relative.parts[0] in components) and '__pycache__' not in relative.parts and relative.suffix != '.pyc'


def verify_application(source, manifest, *, unified=False):
    expected = 'io.github.zcchou.agent.airworthiness' if unified else 'io.github.zcchou.agent.ocrpreview'
    if manifest.get('application_id') != expected:
        raise ValueError('Matching application identity required')
    application = {}
    for name in (UNIFIED_APPLICATION if unified else APPLICATION):
        entry = manifest['files'].get(name)
        if not isinstance(entry, dict):
            raise ValueError('Missing tested application identity')
        check_file(source / name, entry)
        application[name] = entry
    return application


def nsis_path(path):
    return str(Path(path).resolve()).replace('$', '$$').replace('"', '$\\"')


def native_manifest(directory, payload, archives, application):
    """Bind each runtime entry to one trusted ZIP without inflating the payload."""
    files = {name: {**record, 'volume': 'embedded'} for name, record in application.items()}
    seen = set()
    for archive in archives:
        validate_relative(archive['name'])
        with zipfile.ZipFile(Path(directory) / archive['name']) as package:
            for entry in package.infolist():
                name = entry.filename
                validate_relative(name)
                if name.casefold() in seen or name not in payload['files']:
                    raise ValueError('Duplicate or unexpected mapped payload file')
                record = payload['files'][name]
                if record['bytes'] != entry.file_size:
                    raise ValueError('Mapped payload size mismatch')
                seen.add(name.casefold())
                files[name] = {**record, 'volume': archive['name']}
    if seen != {name.casefold() for name in payload['files']}:
        raise ValueError('Mapped payload is missing files')
    return {'schema_version': 2, 'application_id': 'io.github.zcchou.agent.airworthiness',
            'release_id': payload['release_id'], 'files': files, 'archives': archives,
            'prerequisites': payload.get('prerequisites', {})}


def write_native_installer(source, output, payload_manifest, parts_manifest, compiler, version, helper=None):
    original = json.loads((source / 'delivery-manifest.json').read_text(encoding='utf-8'))
    application = verify_application(source, original, unified=True)
    helper = Path(helper) if helper else source / 'agent-installer.exe'
    if not helper.is_file():
        raise ValueError('Build agent-installer.exe and pass --installer-helper or place it beside the application source')
    payload = json.loads(payload_manifest.read_text(encoding='utf-8'))
    archives = json.loads(parts_manifest.read_text(encoding='utf-8'))['archives']
    manifest = native_manifest(output, payload, archives, application)
    # These small installer-owned files participate in the same native transaction.
    extras = {'LICENSE.txt': ROOT / 'LICENSE', '使用说明.md': output / '安装与使用说明.md'}
    for name, path in extras.items():
        manifest['files'][name] = {'bytes': path.stat().st_size, 'sha256': sha256(path), 'volume': 'embedded'}
    native = output / 'build/native-manifest.json'
    write_json(native, manifest)
    setup = output / f'agent-{version}-windows-x64-setup.exe'
    app_files = '\n'.join(f'  File /oname={name} "{nsis_path(source / name)}"' for name in application)
    app_files += '\n' + '\n'.join(f'  File /oname={name} "{nsis_path(path)}"' for name, path in extras.items())
    helper_file = f'  File /oname=agent-installer.exe "{nsis_path(helper)}"'
    script = f'''Unicode true
ManifestDPIAware true
!include MUI2.nsh
!include LogicLib.nsh
!include FileFunc.nsh
Name "agent Airworthiness"
OutFile "{nsis_path(setup)}"
InstallDir "$LOCALAPPDATA\\Programs\\agent Airworthiness"
InstallDirRegKey HKCU "Software\\agentAirworthiness" "InstallDir"
RequestExecutionLevel user
SetCompressor /SOLID lzma
ShowInstDetails show
ShowUninstDetails show
Icon "{nsis_path(ROOT / 'apps/agent-desktop/src-tauri/icons/icon.ico')}"
VIProductVersion "0.16.0.{version.rsplit('.', 1)[1]}"
VIAddVersionKey /LANG=1033 "ProductName" "agent Airworthiness"
VIAddVersionKey /LANG=1033 "FileVersion" "{version}"
VIAddVersionKey /LANG=1033 "FileDescription" "Differential full offline installer"
VIAddVersionKey /LANG=1033 "LegalCopyright" "agent contributors"
Var LifecycleMutex
Var NativeProgress
Var NativeStatus
Var NativeUiArgs
!define MUI_FINISHPAGE_RUN "$INSTDIR\\agent-desktop.exe"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!define MUI_PAGE_CUSTOMFUNCTION_SHOW SetupProgressUi
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!define MUI_PAGE_CUSTOMFUNCTION_SHOW un.SetupProgressUi
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"
!insertmacro MUI_LANGUAGE "English"
!macro AcquireLifecycle
  SetRegView 64
  SetShellVarContext current
  System::Call 'kernel32::CreateMutexW(p 0, i 1, w "Global\\io.github.zcchou.agent.airworthiness.offline-installer-v1") p.r0 ?e'
  Pop $1
  StrCpy $LifecycleMutex $0
  ${{If}} $0 == 0
  ${{OrIf}} $1 == 183
    SetErrorLevel 1
    Quit
  ${{EndIf}}
!macroend
Function .onInit
  !insertmacro AcquireLifecycle
FunctionEnd
Function un.onInit
  !insertmacro AcquireLifecycle
FunctionEnd
!macro NativeProgressUi PREFIX
Function ${{PREFIX}}SetupProgressUi
  GetDlgItem $NativeProgress $mui.InstFilesPage 1004
  SendMessage $NativeProgress 0x406 0 100
  GetDlgItem $0 $mui.InstFilesPage 1016
  System::Alloc 16
  Pop $1
  System::Call 'user32::GetWindowRect(p r0, p r1)'
  System::Call 'user32::MapWindowPoints(p 0, p $mui.InstFilesPage, p r1, i 2)'
  System::Call '*$1(i .r2, i .r3, i .r4, i .r5)'
  IntOp $4 $4 - $2
  IntOp $5 $5 - $3
  System::Call '*$1(i 0, i 0, i 0, i 32)'
  System::Call 'user32::MapDialogRect(p $mui.InstFilesPage, p r1)'
  System::Call '*$1(i, i, i, i .r6)'
  System::Free $1
  System::Call 'user32::CreateWindowExW(i 0, w "STATIC", w "", i 0x50000000, i r2, i r3, i r4, i r6, p $mui.InstFilesPage, p 0, p 0, p 0) p .r7'
  StrCpy $NativeStatus $7
  SendMessage $0 0x31 0 0 $8
  SendMessage $NativeStatus 0x30 $8 1
  IntOp $3 $3 + $6
  IntOp $5 $5 - $6
  System::Call 'user32::SetWindowPos(p r0, p 0, i r2, i r3, i r4, i r5, i 0x14)'
  StrCpy $NativeUiArgs '--ui-parent $HWNDPARENT --progress-hwnd $NativeProgress --status-hwnd $NativeStatus'
FunctionEnd
!macroend
!insertmacro NativeProgressUi ""
!insertmacro NativeProgressUi "un."
Section Install
  InitPluginsDir
  SetOutPath "$PLUGINSDIR"
{helper_file}
  File /oname=postflight.ps1 "{nsis_path(ROOT / 'tools/installer/postflight-airworthiness.ps1')}"
  File /oname=payload.json "{nsis_path(native)}"
  DetailPrint "Preflight / difference: verifying installed identities and required volumes..."
  SetOutPath "$PLUGINSDIR\\app"
{app_files}
  WriteUninstaller "$PLUGINSDIR\\app\\uninstall.exe"
  SetOutPath "$PLUGINSDIR"
  nsExec::ExecToLog '"$PLUGINSDIR\\agent-installer.exe" --action install --root "$INSTDIR" --manifest "$PLUGINSDIR\\payload.json" --sha256 {sha256(native)} --archives "$EXEDIR" --embedded "$PLUGINSDIR\\app" --uninstaller "$PLUGINSDIR\\app\\uninstall.exe" --postflight "$PLUGINSDIR\\postflight.ps1" $NativeUiArgs'
  Pop $0
  ${{If}} $0 != 0
    MessageBox MB_OK|MB_ICONSTOP "Installation could not finish. See details for missing volumes, busy processes or preserved recovery state. Retry this installer after resolving the reported condition." /SD IDOK
    SetErrorLevel 1
    Abort
  ${{EndIf}}
  WriteRegStr HKCU "Software\\agentAirworthiness" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\agentAirworthiness" "DisplayName" "agent Airworthiness"
  WriteRegStr HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\agentAirworthiness" "DisplayVersion" "{version}"
  WriteRegStr HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\agentAirworthiness" "UninstallString" '"$INSTDIR\\uninstall.exe"'
  WriteRegStr HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\agentAirworthiness" "DisplayIcon" "$INSTDIR\\agent-desktop.exe"
  CreateDirectory "$SMPROGRAMS\\agent Airworthiness"
  CreateShortcut "$SMPROGRAMS\\agent Airworthiness\\agent Airworthiness.lnk" "$INSTDIR\\agent-desktop.exe"
  CreateShortcut "$DESKTOP\\agent Airworthiness.lnk" "$INSTDIR\\agent-desktop.exe"
  DetailPrint "Completed: application and runtimes committed."
SectionEnd
Section Uninstall
  InitPluginsDir
  SetOutPath "$PLUGINSDIR"
{helper_file}
  nsExec::ExecToLog '"$PLUGINSDIR\\agent-installer.exe" --action uninstall --root "$INSTDIR" $NativeUiArgs'
  Pop $0
  ${{If}} $0 != 0
    MessageBox MB_OK|MB_ICONSTOP "Uninstall cleanup did not complete. Close application and workers, inspect the details, and retry." /SD IDOK
    SetErrorLevel 1
    Abort
  ${{EndIf}}
  Delete "$DESKTOP\\agent Airworthiness.lnk"
  Delete "$SMPROGRAMS\\agent Airworthiness\\agent Airworthiness.lnk"
  RMDir "$SMPROGRAMS\\agent Airworthiness"
  DeleteRegKey HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\agentAirworthiness"
  DeleteRegKey HKCU "Software\\agentAirworthiness"
  ClearErrors
  Delete "$INSTDIR\\uninstall.exe"
  ${{If}} ${{Errors}}
    MessageBox MB_OK|MB_ICONSTOP "The uninstall retry executable is still in use. Close it and retry; owned recovery state was retained." /SD IDOK
    SetErrorLevel 1
    Abort
  ${{EndIf}}
  Delete "$INSTDIR\\.airworthiness-uninstall.json"
  RMDir "$INSTDIR"
SectionEnd
'''
    nsi = output / 'build/setup.nsi'
    nsi.write_text(script, encoding='utf-8-sig')
    subprocess.run([str(compiler), '/V3', str(nsi)], check=True)
    verify_application(source, original, unified=True)
    return setup


def write_installer(source, output, payload_manifest, parts_manifest, compiler, version='0.15.0-ocr-preview.1', *, unified=False, helper=None):
    pattern = r'0\.16\.0-preview\.[1-9][0-9]*' if unified else r'0\.15\.0-ocr-preview\.[1-9][0-9]*'
    if not re.fullmatch(pattern, version):
        raise ValueError('Unsupported preview version')
    if unified:
        return write_native_installer(source, output, payload_manifest, parts_manifest, compiler, version, helper)
    build = output / 'build'
    original = json.loads((source / 'delivery-manifest.json').read_text(encoding='utf-8'))
    application = verify_application(source, original, unified=unified)
    application_manifest = build / 'application-manifest.json'
    write_json(application_manifest, {'schema_version': 1, 'files': application})
    hook = build / 'hooks.nsh'
    installer_hook(payload_manifest, hook, application_manifest=application_manifest, include_ocr=True, airworthiness=unified)
    hook.write_text(hook.read_text(encoding='utf-8-sig').replace('$EXEDIR\\payload', '$INSTDIR\\.ocr-preview-payload\\payload'), encoding='utf-8-sig')
    helper = ROOT / 'tools/installer/prepare-ocr-payload.ps1'
    files = '\n'.join(f'  ClearErrors\n  File /oname={name} "{nsis_path(source / name)}"\n  !insertmacro NSIS_HOOK_ASSERT_APP_COPY' for name in application)
    setup = output / f'agent-{version}-windows-x64-setup.exe'
    prepare = f'''  InitPluginsDir
  SetOutPath "$PLUGINSDIR"
  File /oname=prepare.ps1 "{nsis_path(helper)}"
  File /oname=parts.json "{nsis_path(parts_manifest)}"
  File /oname=payload.json "{nsis_path(payload_manifest)}"
  nsExec::ExecToLog '"$WINDIR\\Sysnative\\WindowsPowerShell\\v1.0\\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File ".\\prepare.ps1" -Action Prepare -ArchiveDirectory "$EXEDIR" -PartsManifestPath ".\\parts.json" -PayloadManifestPath ".\\payload.json" -InstallDirectory "$INSTDIR"'
  Pop $0
  ${{If}} $0 != 0
    MessageBox MB_OK|MB_ICONSTOP "Offline files missing, damaged, or disk space insufficient. Download all ZIP assets beside setup and see installation details." /SD IDOK
    SetErrorLevel 1
    Abort
  ${{EndIf}}'''
    cleanup = '''  InitPluginsDir
  SetOutPath "$PLUGINSDIR"
  File /oname=prepare.ps1 "''' + nsis_path(helper) + '''"
  nsExec::ExecToLog '"$WINDIR\\Sysnative\\WindowsPowerShell\\v1.0\\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File ".\\prepare.ps1" -Action Cleanup -InstallDirectory "$INSTDIR"'
  Pop $0
  ${If} $0 != 0
    DetailPrint "Owned extraction cleanup failed. The uninstaller can retry cleanup."
  ${EndIf}'''
    script = f'''Unicode true
ManifestDPIAware true
!include MUI2.nsh
!include LogicLib.nsh
!include FileFunc.nsh
!include "{nsis_path(hook)}"
Name "agent OCR Preview"
OutFile "{nsis_path(setup)}"
InstallDir "$LOCALAPPDATA\\Programs\\agent OCR Preview"
InstallDirRegKey HKCU "Software\\agentOCRPreview" "InstallDir"
RequestExecutionLevel user
SetCompressor /SOLID lzma
Icon "{nsis_path(ROOT / 'apps/agent-desktop/src-tauri/icons/icon.ico')}"
VIProductVersion "0.15.0.{version.rsplit('.', 1)[1]}"
VIAddVersionKey /LANG=1033 "ProductName" "agent OCR Preview"
VIAddVersionKey /LANG=1033 "FileVersion" "{version}"
VIAddVersionKey /LANG=1033 "FileDescription" "Offline OCR preview installer"
VIAddVersionKey /LANG=1033 "LegalCopyright" "agent contributors"
!define MUI_CUSTOMFUNCTION_ABORT InstallAbort
!define MUI_FINISHPAGE_RUN "$INSTDIR\\agent-desktop.exe"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"
!insertmacro MUI_LANGUAGE "English"
Function .onInit
  SetRegView 64
  SetShellVarContext current
  !insertmacro NSIS_HOOK_ACQUIRE_LIFECYCLE
FunctionEnd
Function un.onInit
  SetRegView 64
  SetShellVarContext current
  !insertmacro NSIS_HOOK_ACQUIRE_LIFECYCLE
FunctionEnd
Function .onInstFailed
  !insertmacro NSIS_HOOK_INSTALLFAILED
{cleanup}
FunctionEnd
Function InstallAbort
  !insertmacro NSIS_HOOK_INSTALLFAILED
{cleanup}
FunctionEnd
Section Install
{prepare}
  !insertmacro NSIS_HOOK_PREINSTALL
{files}
  File /oname=LICENSE.txt "{nsis_path(ROOT / 'LICENSE')}"
  File /oname=使用说明.md "{nsis_path(output / '安装与使用说明.md')}"
  ClearErrors
  WriteUninstaller "$INSTDIR\\uninstall.exe"
  !insertmacro NSIS_HOOK_ASSERT_APP_COPY
  !insertmacro NSIS_HOOK_POSTINSTALL
  WriteRegStr HKCU "Software\\agentOCRPreview" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\agentOCRPreview" "DisplayName" "agent OCR Preview"
  WriteRegStr HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\agentOCRPreview" "DisplayVersion" "{version}"
  WriteRegStr HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\agentOCRPreview" "UninstallString" '"$INSTDIR\\uninstall.exe"'
  WriteRegStr HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\agentOCRPreview" "DisplayIcon" "$INSTDIR\\agent-desktop.exe"
  CreateDirectory "$SMPROGRAMS\\agent OCR Preview"
  CreateShortcut "$SMPROGRAMS\\agent OCR Preview\\agent OCR Preview.lnk" "$INSTDIR\\agent-desktop.exe"
  CreateShortcut "$DESKTOP\\agent OCR Preview.lnk" "$INSTDIR\\agent-desktop.exe"
{cleanup}
SectionEnd
Section Uninstall
{cleanup}
  ${{If}} $0 != 0
    MessageBox MB_OK|MB_ICONSTOP "Extraction cleanup failed. Close processes using the installation directory and retry." /SD IDOK
    SetErrorLevel 1
    Abort
  ${{EndIf}}
  !insertmacro NSIS_HOOK_PREUNINSTALL
  Delete "$INSTDIR\\agent-desktop.exe"
  Delete "$INSTDIR\\agent-core.exe"
  Delete "$INSTDIR\\agent_desktop_lib.dll"
  Delete "$INSTDIR\\LICENSE.txt"
  Delete "$INSTDIR\\使用说明.md"
  Delete "$INSTDIR\\uninstall.exe"
  Delete "$DESKTOP\\agent OCR Preview.lnk"
  Delete "$SMPROGRAMS\\agent OCR Preview\\agent OCR Preview.lnk"
  RMDir "$SMPROGRAMS\\agent OCR Preview"
  DeleteRegKey HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\agentOCRPreview"
  DeleteRegKey HKCU "Software\\agentOCRPreview"
  RMDir "$INSTDIR"
SectionEnd
'''
    if unified:
        script = script.replace('agent OCR Preview', 'agent Airworthiness').replace('agentOCRPreview', 'agentAirworthiness')
        script = script.replace('.ocr-preview-payload', '.airworthiness-payload')
        script = script.replace('-Action Prepare', '-Airworthiness -Action Prepare').replace('-Action Cleanup', '-Airworthiness -Action Cleanup')
        script = script.replace('VIProductVersion "0.15.0.', 'VIProductVersion "0.16.0.')
        script = script.replace('  Delete "$INSTDIR\\agent_desktop_lib.dll"',
            '  Delete "$INSTDIR\\agent_desktop_lib.dll"\n  Delete "$INSTDIR\\agent-updater.exe"\n  Delete "$INSTDIR\\application.json"')
        hook.write_text(hook.read_text(encoding='utf-8-sig').replace('.ocr-preview-payload', '.airworthiness-payload'), encoding='utf-8-sig')
    nsi = build / 'setup.nsi'
    nsi.write_text(script, encoding='utf-8-sig')
    subprocess.run([str(compiler), '/V3', str(nsi)], check=True)
    verify_application(source, original, unified=unified)
    return setup


def reuse_archives(previous, output, manifest, version):
    previous, output = Path(previous), Path(output)
    old = json.loads((previous / 'build/payload-manifest.json').read_text(encoding='utf-8'))
    if old['files'] != manifest['files']:
        raise ValueError('Previous runtime payload differs from the requested release')
    parts = json.loads((previous / 'build/parts.json').read_text(encoding='utf-8'))['archives']
    archives = []
    for number, entry in enumerate(parts, 1):
        validate_relative(entry['name'])
        original = previous / entry['name']
        check_file(original, entry)
        name = f'agent-{version}-offline.part{number:02d}.zip'
        destination = output / name
        os.link(original, destination)
        archives.append({**entry, 'name': name})
    return archives


def reuse_runtime_volumes(previous, output, records, version):
    """Reuse immutable volumes independently when their complete inventory matches."""
    previous, output = Path(previous), Path(output)
    old = json.loads((previous / 'build/payload-manifest.json').read_text(encoding='utf-8'))['files']
    parts = json.loads((previous / 'build/parts.json').read_text(encoding='utf-8'))['archives']
    current = {record['name']: {key: record[key] for key in ('bytes', 'sha256')} for record in records}
    consumed, reused = set(), []
    for entry in parts:
        validate_relative(entry['name'])
        original = previous / entry['name']
        with zipfile.ZipFile(original) as archive:
            names = [item.filename for item in archive.infolist()]
            for name in names:
                validate_relative(name)
            if not names or len(names) != len(set(names)) or any(name in consumed for name in names):
                raise ValueError('Previous volume inventory is invalid')
            if any(name not in old or current.get(name) != old[name] for name in names):
                continue
            if entry['bytes'] >= 1536 * 1024 ** 2 or sum(old[name]['bytes'] for name in names) > UNIFIED_ASSET_LIMIT:
                continue
        check_file(original, entry)
        name = f'agent-{version}-offline.part{len(reused) + 1:02d}.zip'
        destination = output / name
        try:
            os.link(original, destination)
        except OSError:
            shutil.copyfile(original, destination)
            check_file(destination, entry)
        consumed.update(names)
        reused.append({**entry, 'name': name})
        print(f'REUSED {name} {entry["bytes"]}', flush=True)
    return reused, [record for record in records if record['name'] not in consumed]


def build(source, prerequisites, output, compiler, version='0.15.0-ocr-preview.1', reuse=None, *, unified=False, helper=None):
    source, prerequisites, output = map(Path, (source, prerequisites, output))
    if output.exists():
        raise ValueError('Choose a new release output directory')
    output.mkdir(parents=True)
    (output / 'build').mkdir()
    original = json.loads((source / 'delivery-manifest.json').read_text(encoding='utf-8'))
    verify_application(source, original, unified=unified)
    records = []
    for name, entry in original['files'].items():
        if not is_public_file(name, unified=unified) or name in (UNIFIED_APPLICATION if unified else APPLICATION):
            continue
        validate_relative(name)
        check_file(source / name, entry)
        records.append({'name': name, 'path': source / name, **entry})
    lock = json.loads(LOCK.read_text(encoding='utf-8'))
    for name, entry in lock['prerequisites'].items():
        check_file(prerequisites / name, entry)
        records.append({'name': 'prerequisites/' + name, 'path': prerequisites / name, 'bytes': entry['bytes'], 'sha256': entry['sha256']})
    manifest = {'schema_version': 1, 'release_id': 'agent-' + version,
                'prerequisites': lock['prerequisites'],
                'files': {r['name']: {'bytes': r['bytes'], 'sha256': r['sha256']} for r in records}}
    payload_manifest = output / 'build/payload-manifest.json'
    write_json(payload_manifest, manifest)
    archives = []
    if unified and reuse:
        archives, pending = reuse_runtime_volumes(reuse, output, records, version)
        groups = plan_volumes(pending, limit=UNIFIED_ASSET_LIMIT)
    else:
        groups = [] if reuse else plan_volumes(records, limit=UNIFIED_ASSET_LIMIT if unified else ASSET_LIMIT)
    for n, group in enumerate(groups, len(archives) + 1):
        archive = output / f'agent-{version}-offline.part{n:02d}.zip'
        with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as package:
            for record in group:
                package.write(record['path'], record['name'])
        if archive.stat().st_size >= (1536 * 1024 ** 2 if unified else 2 * 1024 ** 3):
            raise ValueError('A release asset exceeds the GitHub size limit')
        archives.append({'name': archive.name, 'bytes': archive.stat().st_size, 'sha256': sha256(archive)})
        print(f'PACKED {archive.name} {archive.stat().st_size}', flush=True)
    if reuse and not unified:
        archives = reuse_archives(reuse, output, manifest, version)
    parts_manifest = output / 'build/parts.json'
    write_json(parts_manifest, {'schema_version': 1, 'archives': archives})
    instructions = '''# agent OCR Preview 离线安装

版本为 0.15.0-ocr-preview.1，Windows 11 x64 单用户预览版。

1. 下载 setup.exe 和所有 offline.partNN.zip，保存到同一文件夹。
2. 运行 setup.exe，选择至少有 20 GiB 可用空间的安装盘。安装器会校验并解压同目录 ZIP，再安装运行组件。
3. 安装完成后运行 agent OCR Preview。主模型在“系统功能 → 模型”中选择本机 GGUF 文件导入和加载。
4. OCR 模型位于安装目录的 ocr-model-package，在“系统功能 → 模型 → OCR 模型”中导入该目录。嵌入和重排序模型按 README 准备，在“模型 → 检索模型”中本地导入。
5. 在“本地资料库”导入文件，处理完成后通过“项目设置 → 资料与 RAG”启用一个或多个资料库。

安装组件包含 llama.cpp CPU/CUDA/Vulkan、RAG CPU/CUDA、OCR CPU、OCR 模型、WebView2 和微软运行库。显卡驱动由电脑提供。

本次分发使用已验收的调试预览构建，安装器未做 Authenticode 签名，自动更新关闭。OCR CUDA、全量扫描页、复杂表格准确性和干净机验收继续保留，具体功能范围见仓库 docs/ocr-preview.md。

安装与数据目录使用独立的 OCR Preview 标识。卸载程序处理受管程序文件，用户导入的模型、文档、对话与数据库保存在应用数据目录。
'''
    instructions = instructions.replace('0.15.0-ocr-preview.1', version)
    if unified:
        instructions = f'''# agent 离线安装与更新

版本 {version}，Windows 11 x64 单用户预览版。

1. 下载 setup.exe 和全部 offline.partNN.zip，放在同一目录，运行安装器。完整包同时用于首次安装、升级和受管文件修复；升级保留已校验且未变化的文件，仅解压新增或变化的文件。相同内容的旧版分卷可直接复用，文件名不同也会按大小和 SHA256 识别。
2. 安装目录独立使用 agent Airworthiness。运行后在设置中迁入 Document Preview 和 OCR Preview 数据，并选择独立模型库。
3. 新用户需自行导入本地 GGUF 对话模型、嵌入和重排序模型。下载方式见仓库 README。OCR 模型已在离线包内提供。
4. 在资料库导入文档，处理完成后在项目设置中启用资料库。本地模型选定后，发送时由应用加载。
5. 程序更新采用设置中的手动检查。签名程序包仅更新主程序、Core、DLL 和更新辅助程序。运行组件变化需安装完整离线包。安装或卸载前关闭程序及后台工作进程；详情显示校验、解压、发布和清理的实际数量、字节数与耗时。

完整组件包含 llama.cpp CPU/CUDA/Vulkan、RAG CPU/CUDA、OCR、Office 解析、WebView2 和微软运行库。显卡驱动由电脑提供。

安装器尚无 Authenticode 签名。程序更新必须通过独立发布密钥的签名校验。
安装包包含机器识别组件，适航内容仍需专业人员复核。实际验收范围见发行说明。
'''
    (output / '安装与使用说明.md').write_text(instructions, encoding='utf-8')
    setup = write_installer(source, output, payload_manifest, parts_manifest, compiler, version=version, unified=unified, helper=helper)
    release = {'version': version, 'application_id': 'io.github.zcchou.agent.airworthiness' if unified else 'io.github.zcchou.agent.ocrpreview',
               'distribution': 'offline-preview-multipart-installer', 'application_build': 'release' if unified else 'debug-portable-tested',
               'authenticode_signed': False, 'updater_enabled': unified,
               'application_source_commit': original['source_commit'],
               'git_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
               'assets': archives + [{'name': setup.name, 'bytes': setup.stat().st_size, 'sha256': sha256(setup)}]}
    write_json(output / 'release-manifest.json', release)
    public = [p for p in output.iterdir() if p.is_file()]
    (output / 'SHA256SUMS.txt').write_text(''.join(f'{sha256(p)}  {p.name}\n' for p in sorted(public)), encoding='utf-8')
    print('BUILT ' + str(setup), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--prerequisites', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--nsis', required=True)
    parser.add_argument('--version', default='0.15.0-ocr-preview.1')
    parser.add_argument('--reuse-runtime-archives')
    parser.add_argument('--unified', action='store_true')
    parser.add_argument('--installer-helper', type=Path)
    args = parser.parse_args()
    build(args.source, args.prerequisites, args.output, args.nsis, args.version, args.reuse_runtime_archives, unified=args.unified, helper=args.installer_helper)
