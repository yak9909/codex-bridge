"""Keep the Windows replacement process and its bootloader children in one job."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from contextlib import contextmanager
import subprocess
import time

CREATE_SUSPENDED = 0x00000004
BASIC_ACCOUNTING_INFORMATION = 1
TERMINATION_TIMEOUT = 10.0


class _JobAccounting(ctypes.Structure):
    _fields_ = [
        ("total_user_time", ctypes.c_longlong),
        ("total_kernel_time", ctypes.c_longlong),
        ("period_user_time", ctypes.c_longlong),
        ("period_kernel_time", ctypes.c_longlong),
        ("page_faults", wintypes.DWORD),
        ("total_processes", wintypes.DWORD),
        ("active_processes", wintypes.DWORD),
        ("terminated_processes", wintypes.DWORD),
    ]


class _UpdateProcess:
    def __init__(self, api, kernel, process, job):
        self.api, self.kernel = api, kernel
        self.process, self.job = process, job

    def running(self) -> bool:
        return self.api.WaitForSingleObject(self.process, 0) == self.api.WAIT_TIMEOUT

    def stop_tree(self) -> None:
        if not self.kernel.TerminateJobObject(self.job, 1):
            raise ctypes.WinError(ctypes.get_last_error())
        deadline = time.monotonic() + TERMINATION_TIMEOUT
        accounting = _JobAccounting()
        while True:
            if not self.kernel.QueryInformationJobObject(
                    self.job, BASIC_ACCOUNTING_INFORMATION, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if not accounting.active_processes:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError("新しい exe の終了を確認できませんでした")
            time.sleep(0.05)


@contextmanager
def start_update_process(argv: list[str], *, cwd, env):
    import _winapi

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel.ResumeThread.restype = wintypes.DWORD
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateJobObject.restype = wintypes.BOOL
    kernel.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
    ]
    kernel.QueryInformationJobObject.restype = wintypes.BOOL

    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        process, thread, _, _ = _winapi.CreateProcess(
            argv[0], subprocess.list2cmdline(argv), None, None, False,
            CREATE_SUSPENDED | subprocess.CREATE_NO_WINDOW, env, str(cwd), subprocess.STARTUPINFO(),
        )
        try:
            # Assign the suspended bootloader before it can create the inner Python process.
            try:
                if not kernel.AssignProcessToJobObject(job, process):
                    raise ctypes.WinError(ctypes.get_last_error())
                if kernel.ResumeThread(thread) == 0xFFFFFFFF:
                    raise ctypes.WinError(ctypes.get_last_error())
            except OSError:
                _winapi.TerminateProcess(process, 1)
                _winapi.WaitForSingleObject(process, int(TERMINATION_TIMEOUT * 1000))
                raise
            finally:
                _winapi.CloseHandle(thread)
            yield _UpdateProcess(_winapi, kernel, process, job)
        finally:
            _winapi.CloseHandle(process)
    finally:
        # The job has no kill-on-close flag. A replacement that reached readiness can continue.
        _winapi.CloseHandle(job)
