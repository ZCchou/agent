"""Rebuild CUDA inference from a hash-verified existing dependency runtime."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


def file_hash(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verify_source(root):
    root = Path(root).absolute()
    if root.is_symlink() or root.is_junction():
        raise ValueError('Linked dependency runtime refused')
    manifest_path = root / 'runtime.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8-sig'))
    if (manifest.get('schema_version') != 1 or manifest.get('protocol_version') not in (1, 2)
            or manifest.get('device_bundle') != 'cuda' or manifest.get('executable') != 'rag-worker.exe'):
        raise ValueError('A versioned CUDA dependency runtime is required')
    expected = manifest.get('files')
    if not isinstance(expected, dict) or 'rag-worker.exe' not in expected:
        raise ValueError('Dependency runtime executable is unlisted')
    actual = set()
    for directory, directories, names in os.walk(root, followlinks=False):
        for name in directories + names:
            path = Path(directory) / name
            if path.is_symlink() or path.is_junction() or not path.resolve().is_relative_to(root.resolve()):
                raise ValueError('Linked dependency runtime entry refused')
        for name in names:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if relative == 'runtime.json':
                continue
            actual.add(relative)
            if relative not in expected:
                raise ValueError(f'Unlisted dependency runtime file: {relative}')
            if file_hash(path) != expected[relative]:
                raise ValueError(f'Dependency runtime hash mismatch: {relative}')
    if actual != set(expected):
        raise ValueError('Dependency runtime files are missing')
    return {'root': str(root), 'manifest_sha256': file_hash(manifest_path),
            'protocol_version': manifest['protocol_version'], 'file_count': len(actual)}


def build_environment(build_root, overlay, source):
    build_root, overlay, source = (Path(value).absolute() for value in (build_root, overlay, source))
    bootstrap = build_root / 'bootstrap'
    bootstrap.mkdir(parents=True, exist_ok=True)
    tools_path = Path(__file__).resolve().parent
    priorities = [str(overlay), str(source), str(tools_path)]
    (bootstrap / 'sitecustomize.py').write_text(
        'import sys\n'
        f'_build_priorities = {priorities!r}\n'
        'for _build_path in reversed(_build_priorities):\n'
        '    if _build_path in sys.path:\n'
        '        sys.path.remove(_build_path)\n'
        '    sys.path.insert(0, _build_path)\n', encoding='utf-8')
    temporary = build_root / 'temp'
    temporary.mkdir(exist_ok=True)
    environment = os.environ.copy()
    environment.pop('PYTHONHOME', None)
    environment.update(PYTHONPATH=os.pathsep.join([str(bootstrap), *priorities]),
        PYTHONNOUSERSITE='1', PYTHONSAFEPATH='1', PYTHONDONTWRITEBYTECODE='1',
        PYINSTALLER_CONFIG_DIR=str(build_root / 'pyinstaller-cache'), TEMP=str(temporary), TMP=str(temporary),
        HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_DATASETS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1',
        HF_HOME=str(build_root / 'hf-cache'), TOKENIZERS_PARALLELISM='false')
    return environment


def dependency_facts(lock, overlay):
    from importlib import metadata
    import struct
    import torch
    import onnxruntime
    if sys.version_info[:2] != (3, 12) or struct.calcsize('P') != 8:
        raise ValueError('CPython 3.12 x64 is required')
    versions = {}
    for raw in Path(lock).read_text(encoding='utf-8').splitlines():
        if '==' not in raw or raw.startswith('#'):
            continue
        name, expected = raw.strip().split('==', 1)
        actual = metadata.version(name)
        if actual != expected:
            raise ValueError(f'Fixed dependency mismatch: {name} expected {expected}, got {actual}')
        versions[name] = actual
    overlay = Path(overlay).resolve()
    if not all(Path(module.__file__).resolve().is_relative_to(overlay) for module in (torch, onnxruntime)):
        raise ValueError('CUDA dependencies were imported outside the verified runtime')
    providers = onnxruntime.get_available_providers()
    if torch.__version__ != '2.8.0+cu128' or torch.version.cuda != '12.8' \
            or not {'CUDAExecutionProvider', 'CPUExecutionProvider'} <= set(providers):
        raise ValueError('Imported inference dependencies lack the pinned CUDA implementation')
    return {'python': sys.version, 'python_executable': sys.executable, 'versions': versions,
            'torch_source': torch.__file__, 'torch_cuda': torch.version.cuda,
            'torch_cuda_available': torch.cuda.is_available(), 'onnxruntime_source': onnxruntime.__file__,
            'onnxruntime_providers': providers}


def isolated_dependency_facts(lock, overlay):
    from build_verified_rag_runtime import dependency_facts
    return dependency_facts(lock, overlay)


def run_logged(command, path, environment):
    with Path(path).open('w', encoding='utf-8') as stream:
        subprocess.run(command, env=environment, stdout=stream, stderr=subprocess.STDOUT, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--source-runtime', type=Path, required=True)
    parser.add_argument('--build-directory', type=Path, required=True)
    parser.add_argument('--output-directory', type=Path, required=True)
    parser.add_argument('--python', default=sys.executable)
    parser.add_argument('--preflight-only', action='store_true')
    args = parser.parse_args()
    repository = args.repository.resolve()
    build_root = args.build_directory.absolute()
    output = args.output_directory.absolute()
    if output.exists():
        raise ValueError(f'Runtime destination already exists: {output}')
    if output.name != 'rag-runtime' or output.resolve().is_relative_to(args.source_runtime.resolve()):
        raise ValueError('Select a separate owned rag-runtime output directory')
    build_root.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    facts = {'method': 'ordinary PyInstaller with verified CUDA dependency sources on sys.path',
             'source_runtime': verify_source(args.source_runtime)}
    (build_root / 'source-verification.json').write_text(json.dumps(facts, indent=2) + '\n', encoding='utf-8')
    overlay = args.source_runtime.resolve() / '_internal'
    source = repository / 'services/agent-core/src'
    lock = repository / 'services/agent-core/requirements-rag-cuda.lock'
    environment = build_environment(build_root, overlay, source)
    preflight = build_root / 'preflight.py'
    preflight.write_text('''import json,sys
from PyInstaller import isolated
from build_verified_rag_runtime import dependency_facts,isolated_dependency_facts
main=dependency_facts(sys.argv[1],sys.argv[2])
child=isolated.call(isolated_dependency_facts,sys.argv[1],sys.argv[2])
print(json.dumps({'main':main,'isolated_hook_child':child}))
''', encoding='utf-8')
    run_logged([args.python, str(preflight), str(lock), str(overlay)], build_root / 'preflight.log', environment)
    facts['preflight'] = json.loads((build_root / 'preflight.log').read_text(encoding='utf-8').splitlines()[-1])
    from rag_runtime_bundle import build_identity, verify, verify_build_identity
    facts['build_identity'] = build_identity(repository, 'cuda')
    (build_root / 'build-facts.json').write_text(json.dumps(facts, indent=2) + '\n', encoding='utf-8')
    if args.preflight_only:
        print(json.dumps({'status': 'preflight_verified', 'facts': str(build_root / 'build-facts.json')}))
        return
    arguments = ['--noconfirm', '--clean', '--onedir', '--console', '--name', 'rag-worker',
        '--paths', str(source), '--paths', str(overlay), '--distpath', str(build_root / 'dist'),
        '--workpath', str(build_root / 'work'), '--specpath', str(build_root)]
    for package in ('transformers', 'torch', 'onnxruntime', 'onnx', 'tokenizers', 'safetensors', 'sentencepiece'):
        arguments += ['--collect-all', package]
    for package in ('tensorflow', 'jax', 'torchvision', 'torchaudio', 'IPython', 'pytest', 'tensorboard', 'tensorrt'):
        arguments += ['--exclude-module', package]
    arguments.append(str(source / 'agent/rag/worker.py'))
    (build_root / 'pyinstaller-arguments.json').write_text(json.dumps(arguments, indent=2) + '\n', encoding='utf-8')
    run_logged([args.python, '-m', 'PyInstaller', *arguments], build_root / 'pyinstaller.log', environment)
    shutil.copytree(build_root / 'dist/rag-worker', output)
    license_path = args.source_runtime / 'third-party-licenses/LICENSE-NVIDIA-CUDA.html'
    run_logged([args.python, str(repository / 'tools/rag_runtime_assets.py'), str(output),
                '--cuda-license', str(license_path)], build_root / 'assets.log', environment)
    manifest = {'schema_version': 1, 'protocol_version': 2, 'executable': 'rag-worker.exe',
        'device_bundle': 'cuda', 'python': '3.12', 'requirements_sha256': file_hash(lock),
        'build_identity': facts['build_identity'],
        'files': {path.relative_to(output).as_posix(): file_hash(path)
                  for path in sorted(output.rglob('*')) if path.is_file()}}
    (output / 'runtime.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    verified = verify(output, required_device='cuda')
    verify_build_identity(verified, repository)
    facts.update(build_seconds=round(time.monotonic() - started, 2), output_files=len(manifest['files']),
        output_bytes=sum(path.stat().st_size for path in output.rglob('*') if path.is_file()),
        output_exe_sha256=file_hash(output / 'rag-worker.exe'), output_manifest_sha256=file_hash(output / 'runtime.json'),
        cuda_license_sha256=file_hash(output / 'third-party-licenses/LICENSE-NVIDIA-CUDA.html'))
    (build_root / 'build-facts.json').write_text(json.dumps(facts, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': 'built_and_verified', 'facts': str(build_root / 'build-facts.json')}))


if __name__ == '__main__':
    main()
