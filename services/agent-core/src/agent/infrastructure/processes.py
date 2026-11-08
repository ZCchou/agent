"""Windows process containment shared by managed inference workers."""
from __future__ import annotations

import os
import signal
import subprocess


class WindowsJob:
    """Own a process tree. Closing the handle terminates all descendants."""

    def __init__(self, process_id: int, memory_limit: int | None = None):
        self.process_id = process_id
        self._handle = None
        self._closed = False
        if memory_limit is not None and (type(memory_limit) is not int or memory_limit < 1):
            raise ValueError("memory_limit must be a positive byte count")
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes

        class BasicLimit(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ("ReadOperationCount", "WriteOperationCount",
                "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimit), ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self._kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self._kernel.SetInformationJobObject.restype = wintypes.BOOL
        self._kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self._kernel.OpenProcess.restype = wintypes.HANDLE
        self._kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self._kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        self._kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel.CloseHandle.restype = wintypes.BOOL
        self._handle = self._kernel.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimit()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if memory_limit is not None:
            limits.BasicLimitInformation.LimitFlags |= 0x100 | 0x200
            limits.ProcessMemoryLimit = memory_limit
            limits.JobMemoryLimit = memory_limit
        try:
            if not self._kernel.SetInformationJobObject(self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            process = self._kernel.OpenProcess(0x0100 | 0x0001, False, process_id)
            if not process:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                if not self._kernel.AssignProcessToJobObject(self._handle, process):
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                self._kernel.CloseHandle(process)
        except BaseException:
            self.close()
            raise

    def close(self):
        if self._closed:
            return
        self._closed = True
        if os.name == "nt":
            if self._handle:
                self._kernel.CloseHandle(self._handle)
                self._handle = None
        else:
            try:
                os.killpg(self.process_id, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def launch_contained(command, *, memory_limit=None, **kwargs):
    """Attach a suspended Windows child before it can parse model input."""
    if os.name == "nt":
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | 0x08000000 | 0x00000004
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(command, **kwargs)
    job = None
    try:
        job = WindowsJob(process.pid, memory_limit)
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            resume = ctypes.WinDLL("ntdll").NtResumeProcess
            resume.argtypes = [wintypes.HANDLE]
            resume.restype = ctypes.c_long
            if resume(int(process._handle)) != 0:
                raise OSError("Unable to resume the contained child process")
        return process, job
    except BaseException:
        if job:
            job.close()
        process.kill()
        process.wait()
        raise


def restrict_file(path):
    """Apply an owner-only protected DACL to ephemeral connection credentials."""
    if os.name != "nt":
        os.chmod(path, 0o600)
        return
    import ctypes
    from ctypes import wintypes
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    descriptor = ctypes.c_void_p()
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    advapi.SetFileSecurityW.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW("D:P(A;;FA;;;OW)", 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not advapi.SetFileSecurityW(str(path), 0x00000004 | 0x80000000, descriptor):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.LocalFree(descriptor)
