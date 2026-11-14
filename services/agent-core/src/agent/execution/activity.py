"""Associate business runners and bounded leaf operations with execution roots."""
from __future__ import annotations

import asyncio
import os
from contextlib import contextmanager

from agent.execution.execution_wait import current_activity, current_monitor, current_task_id
from agent.storage import CoreDataError

@contextmanager
def associate(monitor, task_id):
    with monitor.associate(task_id):
        yield


async def run_stage(stage, operation, **options):
    activity = current_activity.get()
    monitor, identity = current_monitor.get(), current_task_id.get()
    if activity is not None or monitor is None or identity is None:
        return await operation(activity)
    return await monitor.execute_stage(identity, stage, operation, **options)


def monitored_local(provider):
    profile = getattr(provider, 'profile', None)
    return (current_task_id.get() is not None and profile is not None
            and (profile.local_strict or profile.id.startswith('managed:') or profile.kind == 'ollama'))


def process_counters(process, job=None):
    """Return measured CPU/I/O counters when available, preserving unknowns."""
    if os.name != 'nt' or not getattr(process, '_handle', None):
        return None
    import ctypes
    from ctypes import wintypes

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in
                    ('reads', 'writes', 'other', 'read_bytes', 'write_bytes', 'other_bytes')]

    created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
    io = IoCounters()
    library = ctypes.WinDLL('kernel32', use_last_error=True)
    if job is not None and getattr(job, '_handle', None):
        class Accounting(ctypes.Structure):
            _fields_ = [(name, ctypes.c_int64) for name in
                ('user_time', 'kernel_time', 'period_user_time', 'period_kernel_time')]
            _fields_ += [(name, wintypes.DWORD) for name in
                ('page_faults', 'total_processes', 'active_processes', 'terminated_processes')]
            _fields_ += IoCounters._fields_

        accounting = Accounting()
        query = library.QueryInformationJobObject
        query.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
        query.restype = wintypes.BOOL
        if not query(job._handle, 8, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
            return None
        return {'cpu_ticks': accounting.user_time + accounting.kernel_time,
                'io_bytes': accounting.read_bytes + accounting.write_bytes + accounting.other_bytes}
    handle = wintypes.HANDLE(int(process._handle))
    if not library.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                  ctypes.byref(kernel), ctypes.byref(user)):
        return None
    cpu = sum((value.dwHighDateTime << 32) + value.dwLowDateTime for value in (kernel, user))
    if not library.GetProcessIoCounters(handle, ctypes.byref(io)):
        return {'cpu_ticks': cpu, 'io_bytes': None}
    return {'cpu_ticks': cpu, 'io_bytes': io.read_bytes + io.write_bytes + io.other_bytes}


async def watch_process(process, activity, *, heartbeat=None, job=None):
    previous = None
    while process is not None and process.poll() is None:
        counters = process_counters(process, job)
        active = None
        if previous is not None and counters is not None:
            cpu_active = counters['cpu_ticks'] - previous['cpu_ticks'] >= 200_000
            io_known = counters.get('io_bytes') is not None and previous.get('io_bytes') is not None
            io_active = io_known and counters['io_bytes'] - previous['io_bytes'] >= 65536
            active = True if cpu_active or io_active else None
        activity.resource(active=active, process_alive=True, **(counters or {}))
        if heartbeat is not None:
            await heartbeat(activity)
        previous = counters
        await asyncio.sleep(5)


async def monitored_events(factory, *, stage='model_output', diagnose=None, process=None, heartbeat=None, job=None):
    """Monitor stream ownership, drain cancellation and preserve output ordering."""
    if current_monitor.get() is None or current_task_id.get() is None or current_activity.get() is not None:
        stream = factory()
        try:
            async for event in stream:
                yield event
        finally:
            await stream.aclose()
        return
    queue = asyncio.Queue(maxsize=16)
    delivered = False

    async def consume(activity):
        if delivered:
            raise CoreDataError('execution_replay_unsafe', '模型已经输出内容，请保留当前草稿并通过运行重试恢复。')
        stream = factory()
        sampler = asyncio.create_task(watch_process(process, activity, heartbeat=heartbeat, job=job)) if process is not None else None
        try:
            async for event in stream:
                if event.kind not in {'usage', 'provider_diagnostic'}:
                    activity.progress(kind=event.kind, characters=len(event.text or ''),
                                      **({'output_delivered': True} if event.kind != 'finish' else {}))
                await queue.put(event)
        finally:
            await stream.aclose()
            if sampler is not None:
                sampler.cancel()
                await asyncio.gather(sampler, return_exceptions=True)

    operation = asyncio.create_task(run_stage(stage, consume, safe_to_retry=False, diagnose=diagnose))
    pending = None
    try:
        while True:
            pending = asyncio.create_task(queue.get())
            await asyncio.wait({pending, operation}, return_when=asyncio.FIRST_COMPLETED)
            if pending.done():
                event = pending.result()
                delivered = delivered or event.kind not in {'usage', 'provider_diagnostic', 'finish'}
                yield event
                pending = None
            elif operation.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
                pending = None
                operation.result()
                break
    finally:
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
