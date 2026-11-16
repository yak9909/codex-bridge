"""Dispatch the single executable to the host or lender's existing entry point."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import sys
import webbrowser

from .launcher import choose_role

_agent_handle: int | None = None


def attach_console() -> None:
    """Give the windowed executable a parent console, or a new console for a double click."""
    if sys.platform != "win32":
        return
    kernel = ctypes.windll.kernel32
    if not kernel.AttachConsole(-1):
        if not kernel.AllocConsole() and kernel.GetLastError() != 5:  # already attached
            raise ctypes.WinError()
    sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)
    sys.stderr = open("CONOUT$", "w", encoding="utf-8", buffering=1)
    sys.stdin = open("CONIN$", "r", encoding="utf-8")


def acquire_agent_instance() -> tuple[bool, int | None]:
    """Hold a per-login Windows marker throughout the lender's lifetime, including setup."""
    if sys.platform != "win32":
        return True, None
    kernel = ctypes.windll.kernel32
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateMutexW(None, False, r"Local\codex-bridge-agent")
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


def _run_agent() -> int:
    global _agent_handle
    acquired, handle = acquire_agent_instance()
    if not acquired:
        webbrowser.open("http://127.0.0.1:8766/")
        return 0
    _agent_handle = handle
    try:
        from .agent.main import main
        main()
    finally:
        release_agent_instance()
    return 0


def _run_host(argv: list[str], directory: Path | None = None) -> int:
    attach_console()
    from .host.main import main
    previous_argv = sys.argv
    previous_cwd = Path.cwd()
    try:
        if directory is not None:
            os.chdir(directory)
        # The existing development entry reads argparse's arguments from sys.argv.
        sys.argv = [previous_argv[0], *argv]
        main()
    finally:
        sys.argv = previous_argv
        if directory is not None:
            os.chdir(previous_cwd)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        role = choose_role()
        if role is None:
            return 0
        if role == "host":
            directory = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
                         else Path.cwd())
            command = "start" if (directory / "host.yaml").exists() else "setup"
            return _run_host([command, "--config", "host.yaml"], directory)
        return _run_agent()
    if argv[0] == "host":
        return _run_host(argv[1:])
    if argv == ["agent"]:
        return _run_agent()
    attach_console()
    print("使い方: codex-bridge.exe [host <cmd> --config host.yaml | agent]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
