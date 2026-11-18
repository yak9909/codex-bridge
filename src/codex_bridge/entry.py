"""Dispatch the single executable: the resident app (both roles), `host ...` commands, and the hook."""

from __future__ import annotations

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


def _run_resident() -> int:
    global _agent_handle
    acquired, handle = acquire_agent_instance()
    if not acquired:
        from .resident import ui_port
        webbrowser.open(f"http://127.0.0.1:{ui_port()}/")
        return 0
    _agent_handle = handle
    try:
        from . import resident
        resident.main()
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
    if argv[0] == "host":
        return _run_host(argv[1:])
    attach_console()
    print("使い方: codex-bridge.exe [host <cmd> --config host.yaml]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
