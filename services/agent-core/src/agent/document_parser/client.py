"""Async parent for short-lived, Job-contained document parsing processes."""
from __future__ import annotations

import asyncio
from pathlib import Path
import subprocess
import sys
import uuid

from agent.document_parser.protocol import (MARKER, MEMORY_LIMIT, RESULT, RESULT_LIMIT, STATUS_LIMIT,
                                          TIMEOUT, clean_stage, decode_frame, encode_frame, invalid, managed_original,
                                          plain_path, read_result, stage_marker)
from agent.infrastructure.processes import launch_contained
from agent.storage import CoreDataError
from agent.execution.execution_wait import Diagnostic, current_activity, current_task_id
from agent.execution.activity import run_stage, watch_process


class ParserService:
    def __init__(self, data_dir, *, command=None, memory_limit=MEMORY_LIMIT,
                 timeout=TIMEOUT, concurrency=1, result_limit=RESULT_LIMIT):
        frozen = bool(getattr(sys, 'frozen', False))
        if frozen and (command is not None or (memory_limit, timeout, concurrency, result_limit)
                       != (MEMORY_LIMIT, TIMEOUT, 1, RESULT_LIMIT)):
            raise ValueError('Parser test overrides are unavailable in a frozen application')
        if (type(memory_limit) is not int or memory_limit < 1 or not isinstance(timeout, (int, float))
                or timeout <= 0 or type(concurrency) is not int or concurrency < 1
                or type(result_limit) is not int or not 0 < result_limit <= RESULT_LIMIT):
            raise ValueError('Invalid document parser limits')
        self.data_dir = Path(data_dir).absolute()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        plain_path(self.data_dir)
        self.data_dir = self.data_dir.resolve(strict=True)
        plain_path(self.data_dir)
        self.staging = self.data_dir / 'parser-staging'
        self.staging.mkdir(exist_ok=True)
        plain_path(self.staging)
        for stage in self.staging.iterdir():
            clean_stage(stage)
        self.command = list(command) if command is not None else (
            [sys.executable, '--document-worker'] if frozen else
            [sys.executable, '-m', 'agent.document_parser.worker'])
        self.memory_limit, self.timeout, self.result_limit = memory_limit, timeout, result_limit
        self._semaphore = asyncio.Semaphore(concurrency)
        self._tasks = set()
        self._closed = False

    async def parse(self, original_path):
        if current_task_id.get() is not None and current_activity.get() is None:
            return await run_stage('document_parsing', lambda activity: self._parse(original_path),
                safe_to_retry=True, diagnose=lambda activity: Diagnostic(process_exited=
                    getattr(activity, '_parser_process', None) is not None and activity._parser_process.poll() is not None),
                recoverable=lambda error: getattr(error, 'code', None) == 'rag_parser_failed')
        return await self._parse(original_path)

    async def _parse(self, original_path):
        if self._closed:
            raise CoreDataError('rag_closed', 'The document parser is closed.')
        original = managed_original(self.data_dir, original_path)
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            async with self._semaphore:
                if self._closed:
                    raise CoreDataError('rag_closed', 'The document parser is closed.')
                request_id = uuid.uuid4().hex
                stage = self.staging / ('parser-' + request_id)
                plain_path(self.staging)
                stage.mkdir()
                (stage / MARKER).write_text(stage_marker(stage), encoding='ascii')
                try:
                    if current_task_id.get() is not None:
                        return await self._execute(original, stage, request_id)
                    return await asyncio.wait_for(self._execute(original, stage, request_id), self.timeout)
                except TimeoutError as error:
                    raise CoreDataError('rag_document_timeout', 'Document import exceeded its time limit.') from error
                finally:
                    clean_stage(stage)
        finally:
            self._tasks.discard(task)

    async def _execute(self, original, stage, request_id):
        output = stage / RESULT
        request = {'request_id': request_id, 'data_dir': str(self.data_dir),
                   'input_path': str(original), 'output_path': str(output),
                   'adaptive_wait': current_task_id.get() is not None,
                   'limits': {'result_bytes': self.result_limit, 'memory_bytes': self.memory_limit,
                              'file_bytes': 100 * 1024 * 1024, 'expanded_bytes': 200 * 1024 * 1024,
                              'text_chars': 20_000_000, 'chunks': 100_000}}
        process = job = None
        readers = []
        exchange = None
        sampler = None
        try:
            process, job = launch_contained(self.command, memory_limit=self.memory_limit,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                cwd=None if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[2])
            activity = current_activity.get()
            if activity is not None:
                activity._parser_process = process
                sampler = asyncio.create_task(watch_process(process, activity, job=job))
            readers = [asyncio.create_task(asyncio.to_thread(self._exchange, process, encode_frame(request))),
                       asyncio.create_task(asyncio.to_thread(process.wait))]
            # Shield pipe readers so cancellation can terminate the Job and then await EOF.
            exchange = asyncio.gather(*readers)
            status_bytes, returncode = await asyncio.shield(exchange)
            try:
                status = decode_frame(status_bytes)
            except (ValueError, UnicodeError, RecursionError) as error:
                raise invalid() from error
            if not isinstance(status, dict) or status.get('request_id') != request_id:
                raise invalid()
            if status.get('status') == 'error':
                code = status.get('code', '')
                if not isinstance(code, str) or not code.startswith('rag_') or len(code) > 80:
                    raise invalid()
                raise CoreDataError(code, 'The document worker could not parse this document.')
            if returncode or status.get('status') != 'complete':
                raise CoreDataError('rag_parser_failed', 'The document worker stopped before completion.')
            # Stop all descendants before accepting or reading any result.
            job.close()
            validation = asyncio.create_task(asyncio.to_thread(read_result, output, request_id, self.result_limit))
            readers.append(validation)
            result = await asyncio.shield(validation)
            if activity is not None:
                activity.progress(completed=len(result['chunks']), total=len(result['chunks']), validated=True)
            return result
        except (OSError, BrokenPipeError) as error:
            raise CoreDataError('rag_parser_failed', 'The document worker stopped before completion.') from error
        finally:
            if sampler is not None:
                sampler.cancel()
                await asyncio.gather(sampler, return_exceptions=True)
            if job:
                job.close()
            if process:
                if process.poll() is None:
                    process.kill()
                cleanup = asyncio.create_task(self._reap(process, readers, exchange))
                cancelled = False
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        cancelled = True
                cleanup.result()
                if cancelled:
                    raise asyncio.CancelledError

    @staticmethod
    async def _reap(process, readers, exchange):
        await asyncio.to_thread(process.wait)
        if readers:
            await asyncio.gather(*readers, return_exceptions=True)
        if exchange is not None:
            await asyncio.gather(exchange, return_exceptions=True)
        for stream in (process.stdin, process.stdout):
            stream.close()

    @staticmethod
    def _exchange(process, request):
        process.stdin.write(request)
        process.stdin.close()
        payload = decode_frame(request)
        activity = current_activity.get()
        if payload.get('adaptive_wait') is True and activity is not None:
            while True:
                status = process.stdout.readline(STATUS_LIMIT + 1)
                if len(status) > STATUS_LIMIT or not status.endswith(b'\n'):
                    raise invalid('The document worker heartbeat exceeded its limit or framing.')
                frame = decode_frame(status)
                if not isinstance(frame, dict) or frame.get('request_id') != payload['request_id']:
                    raise invalid('The document worker heartbeat identity changed.')
                if frame.get('status') != 'heartbeat':
                    status += process.stdout.read(STATUS_LIMIT + 1)
                    break
                if set(frame) != {'request_id', 'status'}:
                    raise invalid('The document worker heartbeat fields are invalid.')
                activity.heartbeat(worker='document_parser')
        else:
            status = process.stdout.read(STATUS_LIMIT + 1)
        if len(status) > STATUS_LIMIT or not status.endswith(b'\n') or status.count(b'\n') != 1:
            raise invalid('The document worker status exceeded its limit or framing.')
        return status

    async def close(self):
        self._closed = True
        tasks = list(self._tasks - {asyncio.current_task()})
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
