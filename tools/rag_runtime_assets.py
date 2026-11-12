"""Deterministic runtime-only pruning and third-party license inventory.

Python headers, static/import libraries and debug symbols cannot be loaded by the
frozen Python worker. Identical top-level copies of Torch DLLs are consolidated
into Torch's own lib directory. The supported provider selector excludes TensorRT.
"""
from __future__ import annotations

import argparse
import hashlib
from importlib import metadata
import json
from pathlib import Path
import shutil
import ssl
import sys

BUILD_ONLY_SUFFIXES = {'.lib', '.a', '.exp', '.pdb', '.h', '.hpp', '.c', '.cc', '.cpp', '.cuh', '.cmake'}
UNSUPPORTED_PROVIDERS = {'onnxruntime_providers_tensorrt.dll'}
SSL_SUPPORT_RESOURCE_NAME = 'codex-rejected-cas.pem'
SSL_SUPPORT_RESOURCE_SHA256 = 'c916ce1b14ed8ca69e9b5c9fab22799a900dd610d6d4ec9b1347f4fd48e53718'


def ssl_support_resources() -> list[Path]:
    """Admit the build interpreter's pinned TLS support data when applicable."""
    module = Path(ssl.__file__)
    referenced = SSL_SUPPORT_RESOURCE_NAME in module.read_text(encoding='utf-8')
    resource = module.with_name(SSL_SUPPORT_RESOURCE_NAME)
    if not resource.is_file():
        if referenced:
            raise ValueError('The required build-interpreter SSL support resource codex-rejected-cas.pem is missing')
        return []
    if resource.is_symlink() or (hasattr(resource, 'is_junction') and resource.is_junction()):
        raise ValueError('Linked SSL support resources are not admitted')
    with resource.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    if digest != SSL_SUPPORT_RESOURCE_SHA256:
        raise ValueError('The build interpreter SSL support resource SHA256 differs from the fixed security resource')
    return [resource]


def stage_ssl_support_resources(root: Path) -> list[dict]:
    destination = root / '_internal'
    if not destination.is_dir() or destination.is_symlink() \
            or (hasattr(destination, 'is_junction') and destination.is_junction()):
        raise ValueError('Expected the frozen worker internal resource directory')
    records = []
    for source in ssl_support_resources():
        target = destination / source.name
        if target.is_symlink() or (hasattr(target, 'is_junction') and target.is_junction()):
            raise ValueError('Linked SSL support destination refused')
        shutil.copyfile(source, target)
        records.append({'path': target.relative_to(root).as_posix(), 'bytes': target.stat().st_size,
                        'sha256': SSL_SUPPORT_RESOURCE_SHA256, 'source': 'build-interpreter-stdlib'})
    return records


def excluded(path: Path) -> bool:
    return path.suffix.lower() in BUILD_ONLY_SUFFIXES or path.name.lower() in UNSUPPORTED_PROVIDERS


def prune(root: Path) -> dict:
    if root.is_symlink() or (hasattr(root, 'is_junction') and root.is_junction()):
        raise ValueError('Linked build output refused')
    root = root.resolve()
    if root.name != 'rag-runtime' or not (root / 'rag-worker.exe').is_file() or root.is_symlink():
        raise ValueError('Expected a newly built, owned rag-runtime directory')
    removed = []
    for path in sorted(root.rglob('*')):
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
            raise ValueError('Linked build output refused')
        if path.is_file() and excluded(path):
            with path.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            removed.append({'path': path.relative_to(root).as_posix(), 'bytes': path.stat().st_size,
                            'sha256': digest})
            path.unlink()
    duplicate_bytes = 0
    for canonical in sorted((root / '_internal/torch/lib').glob('*.dll')):
        duplicate = root / '_internal' / canonical.name
        if not duplicate.is_file() or duplicate.stat().st_size != canonical.stat().st_size:
            continue
        with canonical.open('rb') as stream:
            canonical_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
        with duplicate.open('rb') as stream:
            duplicate_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
        if duplicate_hash != canonical_hash:
            continue
        size = duplicate.stat().st_size
        removed.append({'path': duplicate.relative_to(root).as_posix(), 'bytes': size,
                        'sha256': duplicate_hash, 'canonical_path': canonical.relative_to(root).as_posix()})
        duplicate.unlink()
        duplicate_bytes += size
    return {'policy': 'runtime-only-and-identical-torch-dlls-v2',
            'removed_files': removed, 'removed_bytes': sum(item['bytes'] for item in removed),
            'deduplicated_dll_bytes': duplicate_bytes}


def collect_licenses(root: Path, cuda_license: Path) -> dict:
    output = root / 'third-party-licenses'
    output.mkdir(exist_ok=True)
    inventory = []
    for distribution in sorted(metadata.distributions(), key=lambda item: item.metadata.get('Name', '').lower()):
        name = distribution.metadata.get('Name', 'unknown')
        files = []
        for relative in distribution.files or []:
            # Includes license/notice data under dist-info and bundled dependencies.
            if not any(token in relative.name.lower() for token in ('license', 'copying', 'notice', 'copyright')):
                continue
            source = Path(distribution.locate_file(relative))
            if not source.is_file() or source.is_symlink() or source.stat().st_size > 20 * 1024 * 1024:
                continue
            safe_name = name.replace('/', '_').replace('\\', '_')
            license_relative = str(relative).replace('..', '_parent_')
            if Path(license_relative).is_absolute() or ':' in license_relative:
                raise ValueError('Unsafe third-party metadata license path')
            target = output / safe_name / license_relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            files.append(target.relative_to(root).as_posix())
        inventory.append({'distribution': name, 'version': distribution.version,
                          'license_expression': distribution.metadata.get('License-Expression'),
                          'license_field': distribution.metadata.get('License'), 'files': files})
    if not cuda_license.is_file():
        raise ValueError('Pinned NVIDIA redistribution license is required')
    shutil.copyfile(cuda_license, output / 'LICENSE-NVIDIA-CUDA.html')
    python_license = Path(sys.base_prefix) / 'LICENSE.txt'
    if not python_license.is_file():
        raise ValueError('The build interpreter CPython license is required')
    shutil.copyfile(python_license, output / 'LICENSE-CPython.txt')
    (output / 'inventory.json').write_text(json.dumps(inventory, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return {'inventory': 'third-party-licenses/inventory.json', 'distributions': len(inventory),
            'nvidia_cuda_license': 'third-party-licenses/LICENSE-NVIDIA-CUDA.html'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--cuda-license', type=Path, required=True)
    args = parser.parse_args()
    previous_path = args.directory / 'build-assets.json'
    history = []
    if previous_path.is_file():
        previous = json.loads(previous_path.read_text(encoding='utf-8'))
        history = previous.pop('previous_asset_stages', []) + [previous]
    record = {'asset_builder_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'pruning': prune(args.directory),
              'third_party_licenses': collect_licenses(args.directory, args.cuda_license),
              'ssl_support_resources': stage_ssl_support_resources(args.directory)}
    if history:
        record['previous_asset_stages'] = history
    (args.directory / 'build-assets.json').write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'removed_bytes': record['pruning']['removed_bytes'],
                      'distributions': record['third_party_licenses']['distributions']}))


if __name__ == '__main__':
    main()
