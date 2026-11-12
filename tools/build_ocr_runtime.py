"""Assemble an offline Windows OCR runtime from verified local build inputs."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import zipfile

PYTHON_ARCHIVE_SHA256 = '4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3'


def file_hash(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


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
    parser.add_argument('--python-archive', required=True)
    parser.add_argument('--site-packages', required=True)
    parser.add_argument('--destination', required=True)
    args = parser.parse_args()
    build(args.python_archive, args.site_packages, args.destination)
