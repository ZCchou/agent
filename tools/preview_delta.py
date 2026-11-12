"""Build application-only updates for an exact isolated preview baseline."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
import zipfile

from offline_delivery import check_file, sha256

APPLICATION = ('agent-desktop.exe', 'agent_desktop_lib.dll', 'agent-core.exe')
COMPONENTS = {'llm-runtime', 'rag-runtime', 'ocr-runtime', 'ocr-model-package'}


def build_delta(source: Path, baseline: dict, output: Path) -> Path:
    source, output = Path(source), Path(output)
    current = json.loads((source / 'delivery-manifest.json').read_text(encoding='utf-8'))
    if current['application_id'] != baseline['application_id']:
        raise ValueError('Incremental application identity differs from its baseline')
    previous_runtime = {name: entry for name, entry in baseline['files'].items() if name.split('/')[0] in COMPONENTS}
    runtime = {name: entry for name, entry in current['files'].items() if name.split('/')[0] in COMPONENTS}
    if not runtime or runtime != previous_runtime:
        raise ValueError('Changed runtime requires the complete offline installer')
    for name in APPLICATION:
        check_file(source / name, current['files'][name])
        if name not in baseline['files']:
            raise ValueError('Missing previous application identity')
    manifest = {'schema_version': 1, 'version': current['version'], 'base_version': baseline['version'],
                'application_id': current['application_id'], 'source_commit': current['source_commit'],
                'base_files': {name: baseline['files'][name] for name in APPLICATION},
                'files': {name: current['files'][name] for name in APPLICATION},
                'runtime_manifests': {name: entry for name, entry in runtime.items()
                    if name.endswith('/runtime.json') or name == 'ocr-model-package/ocr-model.json'}}
    if output.exists():
        raise ValueError('Use a new delta output path')
    script = Path(__file__).parent / 'installer/update-preview-application.ps1'
    instructions = f'''# agent 预览版增量更新

适用应用标识 `{current['application_id']}`，从 `{baseline['version']}` 更新到 `{current['version']}`。

1. 完整解压本 ZIP，关闭目标应用及其推理进程。
2. 双击 UPDATE.cmd，选择旧版 agent-desktop.exe 所在目录。
3. 更新程序校验旧版和组件身份，备份应用文件，再替换三个程序文件。
4. 提示 UPDATED 后启动 agent-desktop.exe，Core 自动备份并升级数据库。

出现版本不匹配时，使用完整离线安装包或对应应用标识的增量包。已完成更新的目录再次执行会提示 ALREADY_UPDATED。

安装版原应用备份位于 .managed-runtime/application-updates，随原卸载程序清理。便携版备份位于 .agent-app-update。中途断电后关闭应用并再次运行本包，可恢复后继续更新。启动新版后如需回退，应同时恢复旧程序文件和应用数据目录中配套的 agent.db.pre-schema29-*.bak 数据库。

本包采用未签名预览构建，自动更新关闭。用户模型、文档和数据库由应用数据目录保存。
'''
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as package:
        package.writestr('update.json', json.dumps(manifest, ensure_ascii=False, indent=2))
        package.writestr('apply-update.ps1', script.read_text(encoding='utf-8-sig').encode('utf-8-sig'))
        package.writestr('UPDATE.cmd', '@echo off\r\npowershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File "%~dp0apply-update.ps1"\r\npause\r\n')
        package.writestr('UPDATE.zh-CN.md', instructions)
        for name in APPLICATION:
            package.write(source / name, 'payload/' + name)
    print(f'DELTA {output.name} {output.stat().st_size} {sha256(output)}', flush=True)
    return output


def combine_deltas(archives: list[Path], output: Path) -> Path:
    """Keep profile-specific hosts and one byte-identical shared Core."""
    identities = {'io.github.zcchou.agent.ocrpreview': 'ocr',
                  'io.github.zcchou.agent.documentpreview': 'document'}
    profiles = {}
    shared_core = None
    for archive in archives:
        with zipfile.ZipFile(archive) as package:
            if len(package.namelist()) != len(set(package.namelist())):
                raise ValueError('Duplicate delta archive entry')
            if package.getinfo('update.json').file_size > 1024 ** 2:
                raise ValueError('Oversized delta manifest')
            manifest = json.loads(package.read('update.json'))
            kind = identities.get(manifest.get('application_id'))
            if not kind or kind in profiles or set(manifest.get('files', {})) != set(APPLICATION):
                raise ValueError('Unsupported or duplicate delta profile')
            payload = {}
            for name in APPLICATION:
                info = package.getinfo('payload/' + name)
                expected = manifest['files'][name]
                if info.file_size != expected['bytes'] or not 0 < info.file_size <= 256 * 1024 ** 2:
                    raise ValueError('Delta payload size mismatch')
                data = package.read(info)
                if hashlib.sha256(data).hexdigest() != expected['sha256']:
                    raise ValueError('Delta payload integrity mismatch')
                payload[name] = data
            core = payload.pop('agent-core.exe')
            if shared_core is not None and core != shared_core:
                raise ValueError('Profiles must use the same shared Core')
            shared_core = core
            profiles[kind] = (manifest, payload)
    if set(profiles) != {'ocr', 'document'}:
        raise ValueError('Both supported preview profiles are required')
    output = Path(output)
    if output.exists():
        raise ValueError('Use a new combined output path')
    installer = Path(__file__).parent / 'installer'
    instructions = '''# agent 通用增量更新

本包同时支持公开 OCR Preview .1 和此前 schema27、提交4da8068的 Document Preview .1，自动识别所选安装目录并更新至对应 .2。已更新的目录再次执行会提示 ALREADY_UPDATED。

1. 完整解压本 ZIP。
2. 退出目标应用及其推理进程。
3. 双击 UPDATE.cmd，选择 agent-desktop.exe 所在目录。
4. 提示 UPDATED 后启动原应用，Core 自动备份并升级数据库。

两类应用继续使用各自的数据目录。包内共用的 Core 只保存一份。原有版本校验、备份、原子替换和中断恢复由同一更新引擎执行。

程序文件与已知版本不符时会停止更新。安装版备份位于 .managed-runtime/application-updates，便携版备份位于 .agent-app-update。启动新版完成数据库迁移后如需回退，应配套恢复旧程序文件和同代数据库备份。本包采用未签名预览构建。
'''
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as package:
        package.writestr('apply-update.ps1', (installer / 'select-preview-update.ps1').read_text(encoding='utf-8-sig').encode('utf-8-sig'))
        package.writestr('engine.ps1', (installer / 'update-preview-application.ps1').read_text(encoding='utf-8-sig').encode('utf-8-sig'))
        package.writestr('UPDATE.cmd', '@echo off\r\npowershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File "%~dp0apply-update.ps1"\r\npause\r\n')
        package.writestr('UPDATE.zh-CN.md', instructions)
        package.writestr('shared/agent-core.exe', shared_core)
        for kind, (manifest, payload) in profiles.items():
            package.writestr(f'profiles/{kind}/update.json', json.dumps(manifest, ensure_ascii=False, indent=2))
            for name, data in payload.items():
                package.writestr(f'profiles/{kind}/payload/{name}', data)
    print(f'UNIVERSAL_DELTA {output.name} {output.stat().st_size} {sha256(output)}', flush=True)
    return output
