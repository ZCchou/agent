"""Build and sign application-only updates for the independent agent channel."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import zipfile


ROOT = Path(__file__).resolve().parents[1]
APPLICATION_ID = 'io.github.zcchou.agent.airworthiness'
PROGRAM_FILES = ('agent-desktop.exe', 'agent_desktop_lib.dll', 'agent-core.exe', 'agent-updater.exe')
RUNTIME_MANIFESTS = ('llm-runtime/runtime.json', 'rag-runtime/runtime.json', 'ocr-runtime/runtime.json',
                     'ocr-model-package/ocr-model.json', 'office-runtime/runtime.json')


def identity(path):
    with Path(path).open('rb') as stream:
        return {'bytes': Path(path).stat().st_size, 'sha256': hashlib.file_digest(stream, 'sha256').hexdigest()}


def check_version(version, channel):
    pattern = r'\d+\.\d+\.\d+-preview\.[1-9]\d*' if channel == 'preview' else r'\d+\.\d+\.\d+'
    if channel not in {'preview', 'stable'} or not re.fullmatch(pattern, version):
        raise ValueError('Invalid independent release channel or version')


def build_archive(source, destination):
    source, destination = Path(source), Path(destination)
    if destination.exists():
        raise ValueError('Choose a new program archive path')
    manifest = json.loads((source / 'application.json').read_text(encoding='utf-8-sig'))
    if manifest.get('schema_version') != 1 or manifest.get('application_id') != APPLICATION_ID:
        raise ValueError('Application identity mismatch')
    check_version(manifest['version'], 'preview' if '-preview.' in manifest['version'] else 'stable')
    contract = manifest.get('core_contract', {})
    if not contract.get('schema_version') or not contract.get('protocol_version') or not contract.get('build_id'):
        raise ValueError('Missing tested Core identity')
    if set(manifest.get('files', {})) != set(PROGRAM_FILES):
        raise ValueError('Program identity must contain exactly four binaries')
    runtimes = manifest.get('runtime_manifests', {})
    if not runtimes or set(runtimes) - set(RUNTIME_MANIFESTS):
        raise ValueError('Invalid runtime identity')
    for name, expected in {**manifest['files'], **runtimes}.items():
        path = source / name
        if path.is_symlink() or identity(path) != expected:
            raise ValueError('File identity mismatch')
        if not 0 < expected['bytes'] <= 256 * 1024 ** 2:
            raise ValueError('File identity exceeds the program update limit')
    temporary = destination.with_name(destination.name + '.partial')
    try:
        with zipfile.ZipFile(temporary, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            archive.writestr('update.json', json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf8'))
            for name in PROGRAM_FILES:
                archive.write(source / name, 'payload/' + name)
        if temporary.stat().st_size > 512 * 1024 ** 2:
            raise ValueError('Program archive exceeds the update download limit')
        temporary.rename(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return manifest


def feed(version, signature, channel, notes):
    check_version(version, channel)
    if not signature.strip():
        raise ValueError('A signed program package is required')
    return {'version': version, 'notes': notes,
            'pub_date': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            'platforms': {'windows-x86_64': {
                'signature': signature.strip(),
                'url': f'https://github.com/ZCchou/agent/releases/download/v{version}/agent-{version}-windows-x86_64-program.zip'}}}


def sign(path, private_key):
    result = subprocess.run(['node', str(ROOT / 'apps/agent-desktop/node_modules/@tauri-apps/cli/tauri.js'),
                             'signer', 'sign', '--private-key-path', str(private_key), '--password', '', str(path)],
                            capture_output=True, stdin=subprocess.DEVNULL, timeout=120)
    if result.returncode:
        raise RuntimeError('Signing failed. Verify the private key and signer configuration locally.')
    signature = Path(str(path) + '.sig')
    if not signature.is_file():
        raise RuntimeError('Signer did not produce a detached signature')
    checked = subprocess.run(['node', str(ROOT / 'tools/verify-updater-signature.cjs'), str(path),
                              str(signature), str(ROOT / 'apps/agent-desktop/src-tauri/tauri.conf.json')],
                             capture_output=True, timeout=120)
    if checked.returncode:
        raise RuntimeError('Release signature does not match the application public key')
    return signature.read_text(encoding='utf8').strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--private-key', required=True, type=Path)
    parser.add_argument('--channel', default='preview', choices=('preview', 'stable'))
    parser.add_argument('--notes-file', required=True, type=Path)
    args = parser.parse_args()
    owner = json.loads((args.source / 'application.json').read_text(encoding='utf-8-sig'))
    version = owner['version']
    check_version(version, args.channel)
    args.output.mkdir(parents=True, exist_ok=True)
    archive = args.output / f'agent-{version}-windows-x86_64-program.zip'
    build_archive(args.source, archive)
    signature = sign(archive, args.private_key)
    record = feed(version, signature, args.channel, args.notes_file.read_text(encoding='utf8'))
    destination = args.output / f'airworthiness-{args.channel}.json'
    destination.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n', encoding='utf8', newline='\n')
    sign(destination, args.private_key)
    print(json.dumps({'program': archive.name, **identity(archive), 'feed': destination.name}))


if __name__ == '__main__':
    main()
