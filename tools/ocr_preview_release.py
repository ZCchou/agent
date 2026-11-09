"""Build a manually updated OCR preview installer with local, verified ZIP assets."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import zipfile

from offline_delivery import installer_hook, sha256, validate_relative, write_json, check_file, LOCK

ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = ('llm-runtime', 'rag-runtime', 'ocr-runtime', 'ocr-model-package')
APPLICATION = ('agent-desktop.exe', 'agent-core.exe', 'agent_desktop_lib.dll')
ASSET_LIMIT = 1700 * 1024 ** 2


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


def is_public_file(name):
    relative = Path(name)
    return (name in APPLICATION or relative.parts[0] in COMPONENTS) and '__pycache__' not in relative.parts and relative.suffix != '.pyc'


def verify_application(source, manifest):
    application = {}
    for name in APPLICATION:
        entry = manifest['files'].get(name)
        if not isinstance(entry, dict):
            raise ValueError('Missing tested application identity')
        check_file(source / name, entry)
        application[name] = entry
    return application


def nsis_path(path):
    return str(Path(path).resolve()).replace('$', '$$').replace('"', '$\\"')


def write_installer(source, output, payload_manifest, parts_manifest, compiler):
    build = output / 'build'
    original = json.loads((source / 'delivery-manifest.json').read_text(encoding='utf-8'))
    application = verify_application(source, original)
    application_manifest = build / 'application-manifest.json'
    write_json(application_manifest, {'schema_version': 1, 'files': application})
    hook = build / 'hooks.nsh'
    installer_hook(payload_manifest, hook, application_manifest=application_manifest, include_ocr=True)
    hook.write_text(hook.read_text(encoding='utf-8-sig').replace('$EXEDIR\\payload', '$INSTDIR\\.ocr-preview-payload\\payload'), encoding='utf-8-sig')
    helper = ROOT / 'tools/installer/prepare-ocr-payload.ps1'
    files = '\n'.join(f'  ClearErrors\n  File /oname={name} "{nsis_path(source / name)}"\n  !insertmacro NSIS_HOOK_ASSERT_APP_COPY' for name in APPLICATION)
    setup = output / 'agent-0.15.0-ocr-preview.1-windows-x64-setup.exe'
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
VIProductVersion "0.15.0.1"
VIAddVersionKey /LANG=1033 "ProductName" "agent OCR Preview"
VIAddVersionKey /LANG=1033 "FileVersion" "0.15.0-ocr-preview.1"
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
  WriteRegStr HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\agentOCRPreview" "DisplayVersion" "0.15.0-ocr-preview.1"
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
    nsi = build / 'setup.nsi'
    nsi.write_text(script, encoding='utf-8-sig')
    subprocess.run([str(compiler), '/V3', str(nsi)], check=True)
    verify_application(source, original)
    return setup


def build(source, prerequisites, output, compiler):
    source, prerequisites, output = map(Path, (source, prerequisites, output))
    if output.exists():
        raise ValueError('Choose a new release output directory')
    output.mkdir(parents=True)
    (output / 'build').mkdir()
    original = json.loads((source / 'delivery-manifest.json').read_text(encoding='utf-8'))
    verify_application(source, original)
    records = []
    for name, entry in original['files'].items():
        if not is_public_file(name) or name in APPLICATION:
            continue
        validate_relative(name)
        check_file(source / name, entry)
        records.append({'name': name, 'path': source / name, **entry})
    lock = json.loads(LOCK.read_text(encoding='utf-8'))
    for name, entry in lock['prerequisites'].items():
        check_file(prerequisites / name, entry)
        records.append({'name': 'prerequisites/' + name, 'path': prerequisites / name, 'bytes': entry['bytes'], 'sha256': entry['sha256']})
    manifest = {'schema_version': 1, 'release_id': 'agent-0.15.0-ocr-preview.1',
                'prerequisites': lock['prerequisites'],
                'files': {r['name']: {'bytes': r['bytes'], 'sha256': r['sha256']} for r in records}}
    payload_manifest = output / 'build/payload-manifest.json'
    write_json(payload_manifest, manifest)
    archives = []
    for n, group in enumerate(plan_volumes(records), 1):
        archive = output / f'agent-0.15.0-ocr-preview.1-offline.part{n:02d}.zip'
        with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=1) as package:
            for record in group:
                package.write(record['path'], record['name'])
        if archive.stat().st_size >= 2 * 1024 ** 3:
            raise ValueError('A release asset exceeds the GitHub size limit')
        archives.append({'name': archive.name, 'bytes': archive.stat().st_size, 'sha256': sha256(archive)})
        print(f'PACKED {archive.name} {archive.stat().st_size}', flush=True)
    parts_manifest = output / 'build/parts.json'
    write_json(parts_manifest, {'schema_version': 1, 'archives': archives})
    instructions = '''# agent OCR Preview 离线安装

版本为 0.15.0-ocr-preview.1，Windows 11 x64 单用户预览版。

1. 下载 setup.exe 和所有 offline.partNN.zip，保存到同一文件夹。
2. 运行 setup.exe，选择至少有 20 GiB 可用空间的安装盘。安装器会校验并解压同目录 ZIP，再安装运行组件。
3. 安装完成后运行 agent OCR Preview。主模型在“系统功能 → 模型”中选择本机 GGUF 文件导入和加载。
4. OCR 模型位于安装目录的 ocr-model-package，在资料库辅助模型页面导入该目录。嵌入和重排序模型按 README 的下载方法准备后本地导入。

安装组件包含 llama.cpp CPU/CUDA/Vulkan、RAG CPU/CUDA、OCR CPU、OCR 模型、WebView2 和微软运行库。显卡驱动由电脑提供。

本次分发使用已验收的调试预览构建，安装器未做 Authenticode 签名，自动更新关闭。OCR CUDA、全量扫描页、复杂表格准确性和干净机验收继续保留，具体功能范围见仓库 docs/ocr-preview.md。

安装与数据目录使用独立的 OCR Preview 标识。卸载程序处理受管程序文件，用户导入的模型、文档、对话与数据库保存在应用数据目录。
'''
    (output / '安装与使用说明.md').write_text(instructions, encoding='utf-8')
    setup = write_installer(source, output, payload_manifest, parts_manifest, compiler)
    release = {'version': '0.15.0-ocr-preview.1', 'application_id': 'io.github.zcchou.agent.ocrpreview',
               'distribution': 'offline-preview-multipart-installer', 'application_build': 'debug-portable-tested',
               'authenticode_signed': False, 'updater_enabled': False,
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
    args = parser.parse_args()
    build(args.source, args.prerequisites, args.output, args.nsis)
