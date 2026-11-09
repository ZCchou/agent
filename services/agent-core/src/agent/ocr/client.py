"""Locate the isolated OCR runtime and reuse contained JSONL transport."""
from __future__ import annotations

import os
from pathlib import Path
import sys

from agent.rag.worker_client import WorkerClient
from agent.storage import CoreDataError


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
            self.worker = WorkerClient(self.command or runtime_command(), timeout=360, memory_limit=4 * 1024 ** 3)
        return await self.worker.request(action, {'data_dir': str(self.data_dir), 'path': str(path), **params})

    async def close(self):
        if self.worker:
            await self.worker.close()
            self.worker = None
