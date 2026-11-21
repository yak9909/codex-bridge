"""Dispatch the single executable: the resident app (both roles), `host ...` commands, and the hook."""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import sys
import webbrowser

_agent_handle: int | None = None


def attach_console() -> bool:
    """Show our hidden console, preserve an inherited console, and report whether we own it."""
    if sys.platform != "win32":
        return False
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetConsoleWindow.argtypes = []
    kernel.GetConsoleWindow.restype = wintypes.HWND
    kernel.GetConsoleProcessList.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
    kernel.GetConsoleProcessList.restype = wintypes.DWORD
    window = kernel.GetConsoleWindow()
    owned = False
    # console=True normally provides the correct console before Python starts,
    # including in onefile builds. Retain a fallback for launches without one.
    if not window:
        if not kernel.AttachConsole(-1):
            if kernel.AllocConsole():
                owned = True
            else:
                error = ctypes.get_last_error()
                if error != 5:  # ERROR_ACCESS_DENIED: already attached, possibly without a window
                    raise ctypes.WinError(error)
        window = kernel.GetConsoleWindow()
    if window and not owned:
        # A double-clicked onefile console contains only Python and its bootloader.
        # A shell (or any other process) sharing it means it belongs to the caller.
        pids = (wintypes.DWORD * 2)()
        count = kernel.GetConsoleProcessList(pids, len(pids))
        if not count:
            raise ctypes.WinError(ctypes.get_last_error())
        own_pids = {os.getpid()}
        if getattr(sys, "frozen", False):
            own_pids.add(os.getppid())
        owned = count <= len(pids) and set(pids[:count]) <= own_pids
    if owned and window:
        show_window = ctypes.windll.user32.ShowWindow
        show_window.argtypes = [wintypes.HWND, ctypes.c_int]
        show_window.restype = wintypes.BOOL
        show_window(window, 5)  # SW_SHOW; hide-early only hides an owned console
    # Do not replace redirected files/pipes, or usable console streams.
    for name, device, mode in (("stdout", "CONOUT$", "w"), ("stderr", "CONOUT$", "w"),
                               ("stdin", "CONIN$", "r")):
        stream = getattr(sys, name)
        if stream is None or stream.closed:
            setattr(sys, name, open(device, mode, encoding="utf-8", buffering=1))
    return owned and bool(window)  # a windowless console must never trigger an Enter wait


def acquire_agent_instance() -> tuple[bool, int | None]:
    """Hold a per-login Windows marker for as long as the resident app runs."""
    if sys.platform != "win32":
        return True, None
    from .resident import mutex_name
    kernel = ctypes.windll.kernel32
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateMutexW(None, False, mutex_name())
    if not handle:
        raise ctypes.WinError()
    if kernel.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        kernel.CloseHandle(handle)
        return False, None
    return True, handle


def release_agent_instance() -> None:
    """Also used before the agent relaunches itself after moving its data."""
    global _agent_handle
    if _agent_handle is not None:
        ctypes.windll.kernel32.CloseHandle(_agent_handle)
        _agent_handle = None


def claim_agent_instance() -> bool:
    global _agent_handle
    if _agent_handle is not None:
        return True
    acquired, handle = acquire_agent_instance()
    if acquired:
        _agent_handle = handle
    return acquired


@contextmanager
def _previous_process(pid: int):
    """Keep a handle to the old process so that waiting cannot follow a reused PID."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
    if not handle:
        error = ctypes.get_last_error()
        if error != 87:  # The process has already exited.
            raise ctypes.WinError(error)
        yield lambda: None
        return

    def wait():
        if kernel.WaitForSingleObject(handle, 0xFFFFFFFF) != 0:
            raise ctypes.WinError(ctypes.get_last_error())

    try:
        yield wait
    finally:
        kernel.CloseHandle(handle)


def _run_resident(*, after_update: tuple[str, int] | None = None) -> int:
    if not claim_agent_instance():
        if after_update is not None:
            return 1
        from .resident import ui_port
        webbrowser.open(f"http://127.0.0.1:{ui_port()}/")
        return 0
    try:
        from . import resident
        if after_update is None:
            resident.main()
        else:
            from .updater import UpdateError, acknowledge_restart
            stage_name, previous_pid = after_update
            with _previous_process(previous_pid) as wait:
                async def ready():
                    if not acknowledge_restart(Path(sys.executable).resolve(), stage_name):
                        raise UpdateError("新しい exe の起動を通知できませんでした")
                return resident.main(update_ready=ready, wait_for_previous=wait)
    finally:
        release_agent_instance()
    return 0


def _run_host(argv: list[str]) -> int:
    attach_console()
    previous_argv = sys.argv
    try:
        # The existing development entry reads argparse's arguments from sys.argv.
        sys.argv = [previous_argv[0], *argv]
        from .host.main import main
        main()
    finally:
        sys.argv = previous_argv
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "hook":
        from .hook import main as hook_main
        return hook_main(argv[1:])
    if not argv or argv == ["agent"]:
        return _run_resident()
    if len(argv) == 3 and argv[0] == "--after-update" and getattr(sys, "frozen", False):
        try:
            previous_pid = int(argv[2])
        except ValueError:
            return 1
        if not 0 < previous_pid <= 0xFFFFFFFF or previous_pid == os.getpid():
            return 1
        return _run_resident(after_update=(argv[1], previous_pid))
    if argv[0] == "host":
        return _run_host(argv[1:])
    attach_console()
    print("使い方: codex-bridge.exe [host <cmd> --config host.yaml]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
