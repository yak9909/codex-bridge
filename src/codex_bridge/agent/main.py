"""codex-bridge-agent: runs on the friend's PC (tray app)."""

from __future__ import annotations

import asyncio
import os
import shutil
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
from .logview import LogStore
from .runner import AgentRunner
from .sessions import SessionRegistry
from .settings import SettingsApplier, SettingsStore, hello_fields, resolve_workdir
from .setup import (MIN_CODEX_VERSION, choose_codex, data_root, download_codex, load_invite, make_guard, run_login,
                    save_invite, user_mcp_names)
from .ui import create_ui_app
from .worklog import WorkLog

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
            pystray.MenuItem("画面を開く", lambda: webbrowser.open(f"http://127.0.0.1:{LOGVIEW_PORT}/"),
                             default=True),
            pystray.MenuItem(lambda item: "再開する" if self.paused else "一時停止する", lambda: on_pause_toggle()),
            pystray.MenuItem("終了", lambda: on_quit()),
        ))

    def set_status(self, s: str) -> None:
        self.status = s
        self.icon.title = f"{TITLE}: {s}"
        self.icon.update_menu()

    def notify_pending(self, prompt: str) -> None:
        try:
            self.icon.notify(prompt[:200], "codex-bridge: 事前承認待ち")
        except Exception:
            pass

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


async def run_agent(invite: Invite, codex: Path, home: Path) -> bool:
    """Runs until the user quits. True when the data was moved to another place: the exe must start again."""
    cmd = codex_command(codex)
    settings = SettingsStore(home / "settings.json", legacy_limits=home / "limits.json")
    current = settings.get()
    # Our sessions use only the bridge tools: the friend's own MCP servers are switched off for them.
    app = AppServer(cmd, invite.url, invite.token, resolve_workdir(current, home), disabled_mcp=user_mcp_names(),
                    features=current["features"])
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
            return False

    store = LogStore(home / "logs")
    notified = None

    def on_log(message: dict) -> None:
        nonlocal notified
        if message.get("type") in {"settings", "error"}:
            store.add(message)  # task records belong only to WorkLog, without a second unfiltered copy
        if message.get("type") == "task_state" and message.get("state") == "awaiting_lender":
            pending = runner.pending_approval
            key = (pending["task_id"], pending["kind"], pending["created"]) if pending else None
            if key is not None and key != notified:
                notified = key
                tray.notify_pending(pending["prompt"])

    runner = AgentRunner(app, make_guard(mode), SessionRegistry(home / "sessions.json"), send=None,
                         on_log=on_log,
                         worklog=WorkLog(home / "logs" / "tasks", retention_days=current["log_retention_days"]))
    runner.apply_settings(current)
    runner.log_visibility = store.visibility
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    relaunch = False

    async def set_paused(paused: bool) -> None:
        client.paused = paused
        tray.paused = paused
        tray.icon.update_menu()
        if paused:
            await client.drop()

    def toggle_pause() -> None:
        asyncio.run_coroutine_threadsafe(set_paused(not client.paused), loop)

    tray = Tray(on_quit=lambda: loop.call_soon_threadsafe(stop.set), on_pause_toggle=toggle_pause)
    versions = {"agent_version": __version__, "codex_version": codex_version(cmd), "auth_mode": mode}
    client = ControlClient(invite, runner, hello=lambda: {**versions, **hello_fields(runner.settings)},
                           on_status=lambda s: tray.set_status(s))
    tray.run_detached()
    applier = SettingsApplier(settings, app, runner, home, refresh_hello=client.refresh_hello, on_log=store.add)

    async def move_data(new: Path) -> list[Path]:
        nonlocal relaunch
        left = await applier.move_data(new)
        await set_paused(True)  # no new host request may arrive while the exe is relaunching
        relaunch = True  # the stores in this process still point at the old place: start again on the new one
        loop.call_later(1.0, stop.set)
        return left

    logview = make_ui_server(runner, settings, client, versions=versions, data_root=home,
                             on_settings=applier.apply, on_move=move_data, on_pause=set_paused)

    async def watchdog() -> None:
        while True:
            await asyncio.sleep(5)
            if applier.moving:
                continue
            try:
                await applier.settle()  # a change that had to wait for the running task
            except Exception as e:
                store.add({"type": "error", "text": f"設定を反映できませんでした: {type(e).__name__}: {e}"})
            if not app.running and not applier.moving:
                tray.set_status("Codex を再起動しています")
                try:
                    await app.start()
                except Exception:
                    pass

    await runner.start()
    tasks = [asyncio.create_task(client.run_forever()), asyncio.create_task(logview.serve()),
             asyncio.create_task(watchdog())]
    await stop.wait()
    try:
        await asyncio.wait_for(runner.shutdown(), 5)  # a request waiting for the lender is refused while the host is still connected
    except Exception as e:
        store.add({"type": "error", "text": f"終了の知らせを送れませんでした: {type(e).__name__}: {e}"})
    logview.should_exit = True
    for t in tasks[:1] + tasks[2:]:
        t.cancel()
    await runner.stop()
    await app.stop()
    tray.icon.stop()
    return relaunch


def ensure_std_streams() -> None:
    """The windowed exe has no console (sys.stdout/stderr are None); give libraries somewhere to write."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")


def make_ui_server(runner, settings, client, **kwargs) -> uvicorn.Server:
    # log_config=None: a windowed exe has no sys.stdout for uvicorn's logging setup.
    return uvicorn.Server(uvicorn.Config(create_ui_app(runner, settings, client, **kwargs), host="127.0.0.1",
                                         port=LOGVIEW_PORT, log_level="warning", log_config=None))


def run_guarded(fn, home: Path):
    """Run fn and return its result; on an unexpected error write logs/crash.log and tell the user in Japanese."""
    try:
        return fn()
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
    if run_guarded(lambda: _main(home), home):
        start_again()


def start_again() -> None:
    """The data was moved (design 4.7): start the exe again so that it opens the new place."""
    if not getattr(sys, "frozen", False):
        show_info("保存場所を移しました。codex-bridge を起動し直してください。")
        return
    # a one-file exe passes its unpacked folder on through the environment: the new one must unpack its own
    subprocess.Popen([sys.executable], env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"}, close_fds=True,
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                     | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))


def _main(home: Path) -> bool:
    invite = load_invite(home)
    if invite is None:
        invite = ask_invite()
        if invite is None:
            return False
        save_invite(home, invite)
    on_path = shutil.which("codex")
    bundled = home / "bin" / "codex.exe"
    codex = choose_codex(Path(on_path) if on_path else None, bundled if bundled.exists() else None)
    if codex is None:
        need = ".".join(map(str, MIN_CODEX_VERSION))
        if not ask_yes_no(f"Codex（コマンド版、{need} 以降）が見つからないか、古い版です。公式サイト"
                          "（GitHub の openai/codex）から最新版をダウンロードしてよいですか？（約 200MB）\n"
                          "※ あなたが普段使っている Codex には影響しません。"):
            return False
        try:
            codex = download_codex(home / "bin")
        except Exception as e:
            show_info(f"ダウンロードできませんでした: {e}", error=True)
            return False
    return asyncio.run(run_agent(invite, codex, home))


if __name__ == "__main__":
    main()
