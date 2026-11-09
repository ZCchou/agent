"""Locate the isolated OCR runtime and reuse contained JSONL transport."""
from __future__ import annotations

import os
import hashlib
import json
from pathlib import Path
import sys

from agent.rag.worker_client import WorkerClient
from agent.storage import CoreDataError


def verify_runtime(root):
    root = Path(root).resolve()
    try:
        manifest = json.loads((root / 'runtime.json').read_text(encoding='utf-8'))
        if manifest.get('schema_version') != 1 or manifest.get('profile') != 'ocr-preview-cpu-v1':
            raise ValueError('Unsupported OCR runtime manifest')
        files = manifest.get('files')
        if not isinstance(files, dict) or 'python/python.exe' not in files:
            raise ValueError('OCR runtime file manifest is missing')
        for name, expected in files.items():
            path = (root / name).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise ValueError('OCR runtime file is missing or outside the package')
            with path.open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
                    raise ValueError('OCR runtime hash mismatch')
    except (OSError, ValueError, TypeError) as error:
        raise CoreDataError('ocr_runtime_invalid', str(error)) from error


def runtime_command():
    if not getattr(sys, 'frozen', False) and os.environ.get('AGENT_OCR_PYTHON'):
        executable = Path(os.environ['AGENT_OCR_PYTHON'])
    else:
        executable = Path(sys.executable).parent / 'ocr-runtime' / 'python' / 'python.exe'
    if not executable.is_file():
        raise CoreDataError('ocr_runtime_missing', 'OCR runtime is missing. Import the offline OCR runtime package.')
    return [str(executable), '-m', 'agent.ocr.worker']


class OcrClient:
    def __init__(self, data_dir, *, command=None):
        self.data_dir = Path(data_dir).absolute()
        self.command = command
        self.worker = None

    def available(self):
        try:
            return {'available': True, 'command': self.command or runtime_command()}
        except CoreDataError as error:
            return {'available': False, 'error': error.code}

    async def request(self, action, path, **params):
        if self.worker is None:
            command = self.command or runtime_command()
            if self.command is None and getattr(sys, 'frozen', False):
                import asyncio
                await asyncio.to_thread(verify_runtime, Path(command[0]).parent.parent)
            self.worker = WorkerClient(command, timeout=360, memory_limit=4 * 1024 ** 3)
        return await self.worker.request(action, {'data_dir': str(self.data_dir), 'path': str(path), **params})

    async def close(self):
        if self.worker:
            await self.worker.close()
            self.worker = None
