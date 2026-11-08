"""Build-host smoke checks against the installed executable and resource paths."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import uuid

from offline_delivery import verify_llm
from rag_runtime_bundle import verify, verify_build_identity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('installation', type=Path)
    parser.add_argument('--data-directory', type=Path, required=True)
    args = parser.parse_args()
    root = args.installation.resolve()
    repository = Path(__file__).resolve().parents[1]
    verify_llm(root / 'llm-runtime')
    runtime = verify(root / 'rag-runtime', 'cuda')
    verify_build_identity(runtime, repository)
    data = args.data_directory.resolve()
    if data.exists():
        raise ValueError('Use a new smoke-test data directory')
    data.mkdir(parents=True)
    text = 'Offline installed parser smoke.\n' * 20
    digest = hashlib.sha256(text.encode()).hexdigest()
    original = data / 'originals' / digest[:2] / (digest + '.txt')
    original.parent.mkdir(parents=True)
    original.write_text(text, encoding='utf-8')
    request_id = uuid.uuid4().hex
    stage = data / 'parser-staging' / ('parser-' + request_id)
    stage.mkdir(parents=True)
    (stage / '.agent-parser-owned').write_text('agent-document-parser-v1\n' + stage.name + '\n', encoding='ascii')
    result = stage / 'result.ndjson'
    frame = {'request_id': request_id, 'data_dir': str(data), 'input_path': str(original),
             'output_path': str(result), 'limits': {'result_bytes': 256 * 1024 * 1024}}
    environment = dict(os.environ, AGENT_DATA_DIR=str(data), HF_HUB_OFFLINE='1',
                       TRANSFORMERS_OFFLINE='1')
    process = subprocess.run([str(root / 'agent-core.exe'), '--document-worker'],
                             input=(json.dumps(frame) + '\n').encode(), capture_output=True,
                             timeout=125, cwd=root, env=environment)
    status = json.loads(process.stdout)
    if process.returncode or status.get('status') != 'complete' or status.get('request_id') != request_id:
        raise ValueError('Installed frozen parser mode failed')
    lines = [json.loads(line) for line in result.read_text(encoding='utf-8').splitlines()]
    if lines[-1].get('type') != 'end' or not any(line.get('type') == 'chunk' for line in lines):
        raise ValueError('Installed parser produced an incomplete result')
    print(json.dumps({'runtime_hashes_verified': True, 'cpu_cuda_bundle': runtime['device_bundle'],
                      'parser_mode_passed': True, 'installation': str(root)}))


if __name__ == '__main__':
    main()
