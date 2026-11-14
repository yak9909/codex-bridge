"""codex-bridge-agent: runs on the friend's PC (tray app)."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

import uvicorn

from .. import __version__
from ..common.invite import Invite, parse_invite
from .appserver import AppServer, codex_command
from .control_client import ControlClient
from .logview import LogStore, create_logview_app
from .runner import AgentRunner
from .sessions import SessionRegistry
from .setup import data_root, download_codex, find_codex, load_invite, make_guard, run_login, save_invite

LOGVIEW_PORT = 8766
TITLE = "codex-bridge"


# ---- small dialogs (tkinter) ---------------------------------------------
def ask_invite() -> Invite | None:
    import tkinter as tk
    from tkinter import messagebox
    result: dict = {}
    root = tk.Tk()
    root.title(f"{TITLE} の初期設定")
    root.resizable(False, False)
    tk.Label(root, text="依頼主から受け取った招待コードを貼り付けてください。", padx=16, pady=10).pack(anchor="w")
    entry = tk.Entry(root, width=70)
    entry.pack(padx=16)
    entry.focus_set()

    def ok() -> None:
        try:
            result["invite"] = parse_invite(entry.get())
            root.destroy()
        except ValueError as e:
            messagebox.showerror(TITLE, str(e), parent=root)

    tk.Button(root, text="OK", width=12, command=ok).pack(pady=12)
    root.bind("<Return>", lambda e: ok())
    root.mainloop()
    return result.get("invite")


def ask_yes_no(text: str) -> bool:
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    root.withdraw()
    try:
        return messagebox.askyesno(TITLE, text, parent=root)
    finally:
        root.destroy()


def show_info(text: str, error: bool = False) -> None:
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    root.withdraw()
    (messagebox.showerror if error else messagebox.showinfo)(TITLE, text, parent=root)
    root.destroy()


# ---- tray --------------------------------------------------------------
class Tray:
    def __init__(self, on_quit, on_pause_toggle) -> None:
        import pystray
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((4, 4, 60, 60), 14, fill=(47, 111, 222, 255))
        d.text((18, 18), "CB", fill=(255, 255, 255, 255))
        self.status = "起動中"
        self.paused = False
        self.icon = pystray.Icon("codex-bridge", img, TITLE, menu=pystray.Menu(
            pystray.MenuItem(lambda item: f"状態: {self.status}", None, enabled=False),
            pystray.MenuItem("ログ画面を開く", lambda: webbrowser.open(f"http://127.0.0.1:{LOGVIEW_PORT}/"),
                             default=True),
            pystray.MenuItem(lambda item: "再開する" if self.paused else "一時停止する", lambda: on_pause_toggle()),
            pystray.MenuItem("終了", lambda: on_quit()),
        ))

    def set_status(self, s: str) -> None:
        self.status = s
        self.icon.title = f"{TITLE}: {s}"
        self.icon.update_menu()

    def run_detached(self) -> None:
        threading.Thread(target=self.icon.run, daemon=True).start()


# ---- main --------------------------------------------------------------
def codex_version(cmd: list[str]) -> str:
    try:
        out = subprocess.run([*cmd, "--version"], capture_output=True, text=True, timeout=30,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return out.strip().split()[-1] if out.strip() else "?"
    except (OSError, subprocess.TimeoutExpired):
        return "?"


async def run_agent(invite: Invite, codex: Path, home: Path) -> None:
    cmd = codex_command(codex)
    workdir = home / "workdir"
    workdir.mkdir(exist_ok=True)
    app = AppServer(cmd, invite.url, invite.token, workdir)
    await app.start()
    mode = await app.auth_mode()
    if mode == "none":
        await app.stop()
        show_info("Codex にログインします。開いた画面の案内に従って ChatGPT でログインしてください。")
        await asyncio.to_thread(run_login, cmd)
        await app.start()
        mode = await app.auth_mode()
        if mode == "none":
            show_info("Codex にログインできませんでした。もう一度起動してください。", error=True)
            return

    store = LogStore(home / "logs")
    runner = AgentRunner(app, make_guard(mode), SessionRegistry(home / "sessions.json"), send=None,
                         on_log=store.add)
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def toggle_pause() -> None:
        client.paused = not client.paused
        tray.paused = client.paused
        if client.paused:
            asyncio.run_coroutine_threadsafe(client.drop(), loop)

    tray = Tray(on_quit=lambda: loop.call_soon_threadsafe(stop.set), on_pause_toggle=toggle_pause)
    client = ControlClient(invite, runner, hello={"agent_version": __version__, "codex_version": codex_version(cmd),
                                                  "auth_mode": mode},
                           on_status=lambda s: tray.set_status(s))
    tray.run_detached()
    logview = make_logview_server(store)

    async def watchdog() -> None:
        while True:
            await asyncio.sleep(5)
            if not app.running:
                tray.set_status("Codex を再起動しています")
                try:
                    await app.start()
                except Exception:
                    pass

    await runner.start()
    tasks = [asyncio.create_task(client.run_forever()), asyncio.create_task(logview.serve()),
             asyncio.create_task(watchdog())]
    await stop.wait()
    logview.should_exit = True
    for t in tasks[:1] + tasks[2:]:
        t.cancel()
    await runner.stop()
    await app.stop()
    tray.icon.stop()


def ensure_std_streams() -> None:
    """The windowed exe has no console (sys.stdout/stderr are None); give libraries somewhere to write."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")


def make_logview_server(store: LogStore) -> uvicorn.Server:
    # log_config=None: uvicorn's own logging setup inspects sys.stdout, which a windowed exe lacks.
    return uvicorn.Server(uvicorn.Config(create_logview_app(store), host="127.0.0.1", port=LOGVIEW_PORT,
                                         log_level="warning", log_config=None))


def run_guarded(fn, home: Path) -> None:
    """Run fn; on an unexpected error write logs/crash.log and tell the user in Japanese."""
    try:
        fn()
    except Exception:
        log = Path(home) / "logs" / "crash.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} (agent {__version__})\n{traceback.format_exc()}\n")
        show_info("codex-bridge が予期しないエラーで止まりました。\n"
                  f"お手数ですが、次のファイルを依頼主に送ってください:\n{log}", error=True)


def main() -> None:
    ensure_std_streams()
    home = data_root()
    run_guarded(lambda: _main(home), home)


def _main(home: Path) -> None:
    invite = load_invite(home)
    if invite is None:
        invite = ask_invite()
        if invite is None:
            return
        save_invite(home, invite)
    codex = find_codex(home / "bin")
    if codex is None:
        if not ask_yes_no("Codex（コマンド版）が見つかりません。公式サイト（GitHub の openai/codex）から"
                          "ダウンロードしてよいですか？（約 200MB）"):
            return
        try:
            codex = download_codex(home / "bin")
        except Exception as e:
            show_info(f"ダウンロードできませんでした: {e}", error=True)
            return
    asyncio.run(run_agent(invite, codex, home))


if __name__ == "__main__":
    main()
