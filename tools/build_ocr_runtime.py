"""Assemble an offline Windows OCR runtime from verified local build inputs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import zipfile

PYTHON_ARCHIVE_SHA256 = '4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3'


def file_hash(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def source_hash(path):
    return hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n').rstrip(b'\n') + b'\n').hexdigest()


def verify_existing(root):
    root = Path(root).absolute()
    if root.is_symlink() or root.is_junction():
        raise ValueError('Linked OCR runtime refused')
    manifest = json.loads((root / 'runtime.json').read_text(encoding='utf-8-sig'))
    if (manifest.get('schema_version') != 1 or manifest.get('profile') != 'ocr-preview-cpu-v1'
            or manifest.get('python_archive_sha256') != PYTHON_ARCHIVE_SHA256):
        raise ValueError('Expected the verified embedded OCR runtime profile')
    expected = manifest.get('files')
    if not isinstance(expected, dict) or 'python/python.exe' not in expected:
        raise ValueError('OCR interpreter is missing from the manifest')
    actual = set()
    resolved = root.resolve()
    for directory, directories, names in os.walk(root, followlinks=False):
        for name in directories + names:
            path = Path(directory) / name
            if path.is_symlink() or path.is_junction() or not path.resolve().is_relative_to(resolved):
                raise ValueError('Linked OCR runtime entry refused')
        for name in names:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if relative == 'runtime.json':
                continue
            actual.add(relative)
            if relative not in expected:
                raise ValueError(f'Unlisted OCR runtime file: {relative}')
            if file_hash(path) != expected[relative]:
                raise ValueError(f'OCR runtime hash mismatch: {relative}')
    if actual != set(expected):
        raise ValueError('OCR runtime files are missing')
    return manifest


def stage_existing(source, destination, *, repository=None):
    source, destination = Path(source).absolute(), Path(destination).absolute()
    if destination.exists() or destination.is_relative_to(source):
        raise ValueError('Choose a separate new OCR runtime destination')
    manifest = verify_existing(source)
    repository = Path(repository) if repository is not None else Path(__file__).resolve().parents[1]
    agent_prefix = 'python/Lib/site-packages/agent/'
    destination.mkdir(parents=True)
    for relative in manifest['files']:
        if relative.startswith(agent_prefix) or relative == 'requirements.lock':
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        os.link(source / relative, target)
    return _refresh_source_files(destination, repository, manifest)


def refresh_sources(destination, *, repository=None):
    destination = Path(destination).absolute()
    manifest = verify_existing(destination)
    repository = Path(repository) if repository is not None else Path(__file__).resolve().parents[1]
    return _refresh_source_files(destination, repository, manifest)


def _refresh_source_files(destination, repository, manifest):
    agent_source = repository / 'services/agent-core/src/agent'
    agent_destination = destination / 'python/Lib/site-packages/agent'
    sources = {path.relative_to(agent_source).as_posix(): path for path in agent_source.rglob('*')
               if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc'}
    prefix = 'python/Lib/site-packages/agent/'
    for relative in manifest['files']:
        if relative.startswith(prefix) and relative[len(prefix):] not in sources:
            target = destination / relative
            if target.exists():
                target.unlink()
    for relative, path in sources.items():
        target = agent_destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.unlink()
        shutil.copy2(path, target)
    lock = repository / 'services/agent-core/requirements-ocr-preview.lock'
    if (destination / 'requirements.lock').exists():
        (destination / 'requirements.lock').unlink()
    shutil.copy2(lock, destination / 'requirements.lock')
    identity = {'services/agent-core/src/agent/' + path.relative_to(agent_destination).as_posix(): source_hash(path)
                for path in sorted(agent_destination.rglob('*')) if path.is_file()}
    identity['services/agent-core/requirements-ocr-preview.lock'] = source_hash(lock)
    if (destination / 'runtime.json').exists():
        (destination / 'runtime.json').unlink()
    updated = {key: value for key, value in manifest.items() if key not in {'files', 'build_identity'}}
    updated.update(protocol_version=2, build_identity=identity,
        files={path.relative_to(destination).as_posix(): file_hash(path)
               for path in sorted(destination.rglob('*')) if path.is_file()})
    (destination / 'runtime.json').write_text(json.dumps(updated, indent=2) + '\n', encoding='utf-8')
    verify_existing(destination)
    print(json.dumps({'destination': str(destination), 'files': len(updated['files']),
                      'source_files': len(identity) - 1, 'manifest_sha256': file_hash(destination / 'runtime.json')}))
    return updated


def build(archive, site_packages, destination):
    archive, site_packages, destination = map(Path, (archive, site_packages, destination))
    if file_hash(archive) != PYTHON_ARCHIVE_SHA256:
        raise ValueError('Expected official CPython 3.12.10 Windows x64 embedded archive')
    if destination.exists():
        raise ValueError('Choose a new runtime destination')
    destination.mkdir(parents=True)
    python = destination / 'python'
    with zipfile.ZipFile(archive) as package:
        package.extractall(python)
    packages = python / 'Lib' / 'site-packages'
    shutil.copytree(site_packages, packages, ignore=shutil.ignore_patterns(
        '__pycache__', '*.pyc', 'pytest*', '_pytest', 'pip', 'pip-*', 'Scripts', '*.pth'))
    root = Path(__file__).resolve().parents[1]
    shutil.copytree(root / 'services/agent-core/src/agent', packages / 'agent',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    (python / 'python312._pth').write_text('python312.zip\n.\nLib/site-packages\nimport site\n', encoding='utf-8')
    shutil.copy2(root / 'services/agent-core/requirements-ocr-preview.lock', destination / 'requirements.lock')
    manifest = {'schema_version': 1, 'profile': 'ocr-preview-cpu-v1', 'python': '3.12.10',
                'python_archive_sha256': PYTHON_ARCHIVE_SHA256,
                'source': 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip',
                'files': {path.relative_to(destination).as_posix(): file_hash(path)
                          for path in sorted(destination.rglob('*')) if path.is_file()}}
    (destination / 'runtime.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(json.dumps({'destination': str(destination), 'files': len(manifest['files'])}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python-archive')
    parser.add_argument('--site-packages')
    parser.add_argument('--source-runtime')
    parser.add_argument('--refresh-source', action='store_true')
    parser.add_argument('--destination', required=True)
    args = parser.parse_args()
    if args.refresh_source:
        refresh_sources(args.destination)
    elif args.source_runtime:
        stage_existing(args.source_runtime, args.destination)
    elif args.python_archive and args.site_packages:
        build(args.python_archive, args.site_packages, args.destination)
    else:
        parser.error('Provide --source-runtime or both --python-archive and --site-packages')
