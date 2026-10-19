"""Windows: tie helper processes to Museion's life with a Job Object.

A Job Object with kill-on-close is a Windows kernel feature: every process
assigned to the job is terminated when the last handle to the job closes. One
job serves the whole of Museion and its handle is deliberately never closed,
so it closes only when Museion's process ends -- however it ends.

Measured on the Rust version: force-killing the app left three helpers alive
holding 11.7 GB. The job has no such gap.

Declared directly against kernel32 through ctypes, so nothing is compiled.
On other systems assign() does nothing.
"""

from __future__ import annotations

import ctypes
import sys
import threading

_handle: int | None = None
_job_lock = threading.Lock()


def _job() -> int | None:
    global _handle
    with _job_lock:
        if _handle is not None:
            return _handle
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return None
        info = ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        extended_limit_information = 9
        if not kernel32.SetInformationJobObject(handle, extended_limit_information, ctypes.byref(info), ctypes.sizeof(info)):
            return None
        _handle = handle
        return handle


def assign(pid: int) -> None:
    """Put a process into Museion's job. Best effort: if the job cannot be
    created the helper still runs, and the graceful-quit path still stops
    it."""
    if sys.platform != "win32":
        return
    job = _job()
    if job is None:
        return
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    process_set_quota, process_terminate = 0x0100, 0x0001
    handle = kernel32.OpenProcess(process_set_quota | process_terminate, False, pid)
    if handle:
        kernel32.AssignProcessToJobObject(job, handle)
        kernel32.CloseHandle(handle)
