"""Change the shared folder while the host runs (admin page and agents)."""

from __future__ import annotations

import dataclasses
from pathlib import Path

from .setup_tools import inside, set_yaml_value


class ShareBusy(Exception):
    pass


def change_share_root(host, new_path: str) -> Path:
    """Validate and switch the share folder; persists it to host.yaml. Raises ValueError / ShareBusy."""
    raw = str(new_path or "").strip().strip('"')
    p = Path(raw)
    if not raw or not p.is_absolute():
        raise ValueError("フォルダはフルパスで指定してください（例: D:/data/analysis）")
    p = p.resolve()
    if not p.is_dir():
        raise ValueError(f"フォルダが見つかりません: {p}")
    if inside(host.config.data_dir, p):
        raise ValueError("codex-bridge のデータフォルダ（記録や合言葉の置き場）を含むフォルダは共有できません")
    if host.mgr.current_task_id():
        raise ShareBusy("作業中のタスクがあるため、共有フォルダは変えられません。終わってから変えてください")
    host.paths.set_root(p)
    host.config = dataclasses.replace(host.config, share_root=p)
    if host.config_path:
        set_yaml_value(host.config_path, "share_root", p.as_posix())
    host.audit.write("share_root_changed", share_root=str(p))
    host.events.notify()
    return p


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
