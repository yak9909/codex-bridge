"""Shared folders: add, remove, choose the main one (admin page and agents)."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

from .policy_form import _abs, add_folder, remove_folder, set_main_folder

UNWRITABLE_WARNING = "このフォルダは、今のユーザーの権限では書き込めません。Codex が書こうとしても Windows に断られます。"


def folder_is_writable(folder: Path) -> bool:
    """Try one exclusive creation; never retry Windows ACL failures like tempfile does."""
    probe = folder / f".codex-bridge-probe-{uuid4().hex}"
    # Keep Windows cleanup tied to the handle even when later deletion by name
    # would be denied. Other platforms use the explicit unlink in finally.
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_TEMPORARY", 0)
    try:
        descriptor = os.open(probe, flags, 0o600)
        try:
            os.close(descriptor)
        finally:
            probe.unlink(missing_ok=True)
    except OSError:
        return False
    return True


class ShareBusy(Exception):
    pass


def _guard(host, path: str, *, busy_check: bool = True) -> Path:
    p = Path(_abs(path))
    if busy_check and host.mgr.current_task_id():
        raise ShareBusy("作業中のタスクがあるため、共有フォルダは変えられません。終わってから変えてください")
    return p


def _done(host, what: str, p: Path) -> None:
    host.audit.write("shared_folders_changed", action=what, path=str(p))
    host.events.notify()


def add_shared_folder(host, path: str, level: str = "read") -> Path:
    p = _guard(host, path)
    add_folder(host.policy, p.as_posix(), level)
    _done(host, f"add:{level}", p)
    return p


def remove_shared_folder(host, path: str) -> Path:
    p = _guard(host, path)
    remove_folder(host.policy, p.as_posix())
    _done(host, "remove", p)
    return p


def set_main_shared_folder(host, path: str) -> Path:
    p = _guard(host, path)
    set_main_folder(host.policy, p.as_posix())
    _done(host, "main", p)
    return p


def change_share_root(host, new_path: str) -> Path:
    """v0.1 name: share this folder (writable) and make it the main folder."""
    p = _guard(host, new_path)
    if not p.is_dir():
        raise ValueError(f"フォルダが見つかりません: {p}")
    if host.policy.current().level_for_abs(p) != "write":
        add_folder(host.policy, p.as_posix(), "write")
    set_main_folder(host.policy, p.as_posix())
    _done(host, "main", p)
    return p.resolve()


def pick_folder_dialog(initial: Path | None = None) -> str | None:
    """Open the Windows folder picker on this PC (the admin page runs on the same machine)."""
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        chosen = filedialog.askdirectory(title="Codex に触らせるフォルダを選んでください",
                                         initialdir=str(initial) if initial else None, mustexist=True)
    finally:
        root.destroy()
    return chosen or None
