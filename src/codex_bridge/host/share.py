"""Shared folders: add, remove, choose the main one (admin page and agents)."""

from __future__ import annotations

import json
import os
import re
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import yaml

from .policy_form import _abs, _form, add_folder, main_is_within_folder, remove_folder, set_main_folder

UNWRITABLE_WARNING = "このフォルダは、今のユーザーの権限では書き込めません。Codex が書こうとしても Windows に断られます。"


HOST_YAML_WARNING = ("設定は反映しましたが、host.yaml を書き換えられませんでした。"
                     "次の起動では、管理画面でもう一度フォルダを選んでください。")


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


def _guard(host, path: str, *, busy_check: bool = False, removing: bool = False) -> Path:
    p = Path(_abs(path))
    if busy_check and host.mgr.current_task_id():
        raise ShareBusy("作業中はメインのフォルダを切り替えられません。終わってから切り替えてください")
    if not removing:
        host.policy.guard_shared_folder(p)
        install = host.paths.install_dir
        if install and (install.is_relative_to(p.resolve()) or any(
                p.resolve().is_relative_to(install / name) for name in ("vendor", "src", ".venv", "scripts"))):
            raise ValueError("codex-bridge のインストール先を含むフォルダは共有できません。別のフォルダを選んでください。")
    return p


def _done(host, what: str, p: Path) -> None:
    host.audit.write("shared_folders_changed", action=what, path=str(p))
    host.events.notify()


def add_shared_folder(host, path: str, level: str = "read") -> Path:
    p = _guard(host, path)
    host.share_warning = None  # each operation starts clean; only its own host.yaml failure sets it
    add_folder(host.policy, p.as_posix(), level)
    _done(host, f"add:{level}", p)
    return p


def remove_shared_folder(host, path: str) -> Path:
    p = _guard(host, path, removing=True)
    before = host.paths.root
    if host.mgr.active_task_ids() and main_is_within_folder(_form(host.policy)["main"], p):
        raise ShareBusy("作業中はメインのフォルダを外せません。終わってから外してください")
    host.share_warning = None
    remove_folder(host.policy, p.as_posix())
    after = host.paths.root
    if after != before:  # the main folder moved on (or is gone): host.yaml must say the same
        _follow_main(host, after)
    _done(host, "remove", p)
    return p


def _save_share_root(path: Path, root: Path | None) -> None:
    """Update only the setting's line (null when unset), retaining UTF-8 bytes and original newlines."""
    text = path.read_bytes().decode("utf-8")
    value = "null" if root is None else json.dumps(root.as_posix(), ensure_ascii=False)
    match = re.search(
        r"(?m)^(?:\ufeff)?(?:share_root|\"share_root\"|'share_root')[ \t]*:[^\r\n]*", text)
    if match:
        line = match.group()
        tokens = iter(yaml.scan(line))
        colon = next(t for t in tokens if isinstance(t, yaml.tokens.ValueToken))
        content = [t for t in tokens
                   if not isinstance(t, (yaml.tokens.BlockEndToken, yaml.tokens.StreamEndToken))]
        start = content[0].start_mark.index if content else colon.end_mark.index
        end = content[-1].end_mark.index if content else start
        if start == colon.end_mark.index:
            value = " " + value
        line = line[:start] + value + line[end:]
        text = text[:match.start()] + line + text[match.end():]
    else:
        ending = re.search(r"\r\n|\n|\r", text)
        newline = ending.group() if ending else "\n"
        separator = "" if not text or text.endswith(("\n", "\r")) else newline
        text += separator + f"share_root: {value}" + newline
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(text.encode("utf-8"))
    os.replace(tmp, path)


def _follow_main(host, root: Path | None) -> None:
    """Keep host.yaml (a copy for the next start) and the running host in step with the policy's main.

    The policy is already saved, so a host.yaml that cannot be written is a warning, not a failure."""
    if host.config_path is not None:
        try:
            _save_share_root(host.config_path, root)
        except (OSError, ValueError, yaml.YAMLError, StopIteration) as e:
            host.audit.write("host_yaml_not_updated", error=f"{type(e).__name__}: {e}")
            host.share_warning = HOST_YAML_WARNING
    host.config = replace(host.config, share_root=root)
    host.paths.set_root(root)
    host.policy.share_root = root


def set_main_shared_folder(host, path: str) -> Path:
    p = _guard(host, path, busy_check=True)
    host.share_warning = None
    was_unset = host.paths.root is None  # a real main that already exists keeps the user's list as is
    set_main_folder(host.policy, p.as_posix())
    _follow_main(host, p.resolve())
    _done(host, "main", p)
    if was_unset:
        for dropped in drop_sample_folders(host.policy):
            _done(host, "remove", dropped)
    return p


SAMPLE_FOLDER = "c:/path/to/your/folder"


def drop_sample_folders(store) -> list[Path]:
    """Remove the shipped sample row (it never exists on this PC). Returns the rows removed."""
    dropped = []
    for folder in store.current().folders:
        if folder.path.as_posix().rstrip("/").lower() == SAMPLE_FOLDER:
            remove_folder(store, folder.path.as_posix())
            dropped.append(folder.path)
    return dropped


def change_share_root(host, new_path: str) -> Path:
    """v0.1 name: share this folder (writable) and make it the main folder."""
    p = _guard(host, new_path, busy_check=True)
    if not p.is_dir():
        raise ValueError(f"フォルダが見つかりません: {p}")
    if host.policy.current().level_for_abs(p) != "write":
        add_folder(host.policy, p.as_posix(), "write")
    set_main_shared_folder(host, p.as_posix())  # also drops the sample row when the root was unset
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
