"""The lender's role: runs on the friend's PC (tray app)."""

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
from urllib.parse import urlsplit

import uvicorn

from .. import __version__
from ..common.invite import Invite, make_invite, parse_invite
from .appserver import AppServer, codex_command
from .control_client import ControlClient
from .connections import ConnectionStore
from .logview import LogStore
from .runner import AgentRunner, TaskSlot
from .sessions import SessionRegistry
from .settings import SettingsApplier, SettingsStore, hello_fields, resolve_workdir, move_data_root
from .setup import (MIN_CODEX_VERSION, choose_codex, data_root, download_codex, make_guard, run_login,
                    user_mcp_names)
from .ui import create_ui_app
from .worklog import WorkLog

LOGVIEW_PORT = 8766
TITLE = "codex-bridge"
SHUTDOWN_TIMEOUT = 5.0


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


class AgentConnections:
    """Live objects for each stored host; all task admission goes through the runner's shared slot."""

    def __init__(self, home: Path, *, app_factory, versions: dict, initial_backoff=1.0, on_log=None):
        self.home = Path(home)
        self.on_log = on_log or (lambda m: None)
        self.store = ConnectionStore(home, on_log=self.on_log)  # migrate before projecting the shared store
        self.settings = SettingsStore(home / "settings.json", legacy_limits=home / "limits.json", shared=True)
        self.settings.update({})
        self.app_factory, self.versions = app_factory, versions
        self.initial_backoff = initial_backoff
        self.slot = TaskSlot()
        self.runners, self.clients, self.links = {}, {}, {}
        self.started, self.paused, self.moving = False, False, False
        self._usage_probe = None
        self.appliers = {}
        for connection in self.store.list():
            self._install(connection)

    def _install(self, connection):
        current = {**self.settings.get(), **connection.settings}
        app = self.app_factory(connection, current)
        root = self.home / "connections" / connection.id

        async def prepare():
            if self.moving:
                raise RuntimeError("保存場所を移動中です")
            # Launch only when a task/session operation arrives. A change to launch arguments waits for idle.
            desired = runner.settings["features"]
            wd = resolve_workdir(runner.settings, self.home)
            if (app.running and (app.features != desired or Path(app.workdir) != wd)
                    and runner.cur and runner.cur.thread_id is None):
                await app.restart(features=desired, workdir=wd)
            elif not app.running:
                app.features = dict(desired)
                app.workdir = wd
            await app.start()
            mode = await app.auth_mode()
            if mode != self.versions.get("auth_mode"):
                self.versions["auth_mode"] = mode
                for other in self.runners.values():
                    other.guard.enabled = mode == "chatgpt"
                await asyncio.gather(*(client.refresh_hello() for client in self.clients.values()),
                                     return_exceptions=True)
            await runner.refresh_usage()

        runner = AgentRunner(app, make_guard(self.versions.get("auth_mode", "chatgpt")),
                             SessionRegistry(root / "sessions.json"), send=None, slot=self.slot,
                             connection_id=connection.id, host_name=urlsplit(connection.invite.url).netloc,
                             prepare=prepare, on_log=lambda m: self.on_log({**m, **runner.connection_fields()}),
                             worklog=WorkLog(root / "logs" / "tasks", current["log_retention_days"]))
        runner.apply_settings(current)
        runner.last_usage = self.slot.last_usage
        runner.log_visibility = current["log_visibility"]
        client = ControlClient(connection.invite, runner,
                               hello=lambda: {**self.versions, **hello_fields(runner.settings)},
                               initial_backoff=self.initial_backoff)
        client.paused = self.paused
        self.runners[connection.id], self.clients[connection.id] = runner, client
        # The existing applier handles a deferred restart, using the combined settings of this connection.
        class Combined:
            def get(inner):
                return runner.settings
        self.appliers[connection.id] = SettingsApplier(Combined(), app, runner, self.home,
                                                      refresh_hello=client.refresh_hello, on_log=self.on_log)

    async def _activate(self, id):
        await self.runners[id].start()
        self.links[id] = asyncio.create_task(self.clients[id].run_forever())

    async def start(self):
        self.started = True
        for id in self.runners:
            await self._activate(id)
        self._ensure_usage_probe()

    def _error(self, text, error):
        try:
            self.on_log({"type": "error", "text": f"{text}: {type(error).__name__}: {error}"})
        except Exception:
            pass

    def _ensure_usage_probe(self):
        if (not self.started or self.moving or not self.runners
                or any(r.app.running for r in self.runners.values())
                or self._usage_probe is not None and not self._usage_probe.done()):
            return
        self._usage_probe = asyncio.create_task(self._probe_usage())

    async def _probe_usage(self):
        try:
            async with self.slot.lock:
                if not self.started or self.moving:
                    return
                # Warm only one connection. Its periodic usage is broadcast to every requester by TaskSlot.
                runner = next(iter(self.runners.values()), None)
                if runner is not None:
                    await runner.prepare()
        except Exception as e:
            self._error("起動時の使用量を確認できませんでした", e)

    async def _stop_connection(self, id):
        link = self.links.pop(id, None)
        if link:
            link.cancel()
            await asyncio.gather(link, return_exceptions=True)
        runner = self.runners[id]
        try:
            await runner.stop()
        finally:
            await runner.app.stop()

    async def stop(self):
        self.started = False
        if self._usage_probe is not None:
            self._usage_probe.cancel()
            await asyncio.gather(self._usage_probe, return_exceptions=True)
        for runner in self.runners.values():
            try:
                await asyncio.wait_for(runner.shutdown(), SHUTDOWN_TIMEOUT)
            except Exception as e:
                self._error("終了の知らせを送れませんでした", e)
        for id in list(self.runners):
            try:
                await self._stop_connection(id)
            except Exception as e:
                self._error("接続先の終了処理でエラーがありました", e)

    async def add(self, code):
        connection = self.store.add(code)
        self._install(connection)
        if self.started:
            await self._activate(connection.id)
            self._ensure_usage_probe()
        return connection

    def _require_idle(self, id):
        if self.runners[id].active_task_id() is not None:
            raise RuntimeError("作業中・承認待ちの接続先は変更できません")

    async def replace_invite(self, id, code):
        async with self.slot.lock:
            self._require_idle(id)
            connection = self.store.replace_invite(id, code)
            await self._stop_connection(id)
            runner, client = self.runners[id], self.clients[id]
            same_host = (client.invite.url, client.invite.name) == (connection.invite.url, connection.invite.name)
            if not same_host:
                # Never send another requester's events or sessions to the replacement host.
                runner._unacked.clear()
                runner.sessions = SessionRegistry(self.home / "connections" / id / "sessions.json")
                runner.guard = make_guard(self.versions.get("auth_mode", "chatgpt"))
                runner.apply_settings({**self.settings.get(), **connection.settings})
            runner.host_name = urlsplit(connection.invite.url).netloc
            runner.app.host_url, runner.app.token = connection.invite.url, connection.invite.token
            client.invite, client.invite_problem = connection.invite, None
            if self.started:
                await self._activate(id)
                self._ensure_usage_probe()
            return connection

    async def remove(self, id):
        async with self.slot.lock:
            self._require_idle(id)
            await self._stop_connection(id)
            self.store.remove(id)
            self.slot.runners.remove(self.runners.pop(id))
            self.clients.pop(id)
            self.appliers.pop(id)
            self._ensure_usage_probe()

    async def update_settings(self, id, patch):
        saved = self.store.update_settings(id, patch)
        runner = self.runners[id]
        runner.apply_settings({**self.settings.get(), **saved})
        await self.clients[id].refresh_hello()
        if runner.app.running:
            await self.appliers[id].settle()
        runner.log_visibility = saved["log_visibility"]
        return saved

    async def apply_shared(self, saved):
        for id, runner in self.runners.items():
            runner.apply_settings({**runner.settings, **saved})
            if runner.app.running:
                await self.appliers[id].settle()
            await self.clients[id].refresh_hello()  # limits also appear in hello even when Codex is still lazy

    async def settle(self):
        if self.moving:
            return
        self._ensure_usage_probe()
        for id, runner in self.runners.items():
            # Do not turn an idle, never-used app-server on just to settle settings.
            if runner.app.running:
                await self.appliers[id].settle()

    async def set_paused(self, paused):
        self.paused = paused
        for client in self.clients.values():
            client.paused = paused
            if paused:
                await client.drop()

    async def move_data(self, new):
        async with self.slot.lock:
            if self.slot.owner is not None:
                raise ValueError("作業中・承認待ちは保存場所を変えられません")
            was_paused = self.paused
            await self.set_paused(True)
            self.moving = True
            try:
                for id in self.runners:
                    await self._stop_connection(id)
                return await asyncio.to_thread(move_data_root, new, current=self.home)
            except BaseException:
                self.moving = False
                if self.started:
                    for id in self.runners:
                        await self._activate(id)
                await self.set_paused(was_paused)
                raise

    def rows(self):
        return [{"id": id, "host_name": runner.host_name, "connected": self.clients[id].ws is not None,
                 "invite_problem": self.clients[id].invite_problem, "busy": runner.active_task_id() is not None}
                for id, runner in self.runners.items()]


async def run_agent(invite: Invite | None, codex: Path, home: Path) -> bool:
    """Runs all saved hosts until quit; True asks the exe to relaunch after moving its data."""
    cmd = codex_command(codex)
    store = LogStore(home / "logs")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    relaunch = False
    notified = None

    def factory(connection, current):
        app = AppServer(cmd, connection.invite.url, connection.invite.token, resolve_workdir(current, home),
                        disabled_mcp=user_mcp_names(), features=current["features"])
        original_start = app.start

        async def authenticated_start():
            await original_start()
            mode = await app.auth_mode()
            if mode == "none":
                await app.stop()
                await asyncio.to_thread(show_info, "Codex にログインします。開いた画面の案内に従って ChatGPT でログインしてください。")
                await asyncio.to_thread(run_login, cmd)
                await original_start()
                mode = await app.auth_mode()
                if mode == "none":
                    raise RuntimeError("Codex にログインできませんでした。もう一度起動してください。")
        app.start = authenticated_start
        return app

    def on_log(message):
        nonlocal notified
        if message.get("type") in {"settings", "error"}:
            store.add(message)
        if message.get("type") == "task_state" and message.get("state") == "awaiting_lender":
            runner = runtime.runners.get(message.get("connection_id"))
            pending = runner.pending_approval if runner else None
            key = (runner.connection_id, pending["task_id"], pending["created"]) if pending else None
            if key is not None and key != notified:
                notified = key
                tray.notify_pending(pending["prompt"])

    versions = {"agent_version": __version__, "codex_version": codex_version(cmd), "auth_mode": "unknown"}
    runtime = AgentConnections(home, app_factory=factory, versions=versions, on_log=on_log)
    if invite is not None and not runtime.runners:
        await runtime.add(make_invite(invite.url, invite.token, invite.name))

    async def set_paused(paused):
        await runtime.set_paused(paused)
        tray.paused = paused
        tray.icon.update_menu()

    def toggle_pause():
        asyncio.run_coroutine_threadsafe(set_paused(not runtime.paused), loop)

    tray = Tray(on_quit=lambda: loop.call_soon_threadsafe(stop.set), on_pause_toggle=toggle_pause)
    tray.run_detached()

    async def move_data(new):
        nonlocal relaunch
        left = await runtime.move_data(new)
        relaunch = True
        loop.call_later(1.0, stop.set)
        return left

    logview = make_ui_server(None, runtime.settings, None, versions=versions, data_root=home,
                             connections=runtime, on_settings=runtime.apply_shared, on_move=move_data,
                             on_pause=set_paused)

    async def watchdog():
        while True:
            await asyncio.sleep(5)
            try:
                await runtime.settle()
            except Exception as e:
                store.add({"type": "error", "text": f"設定を反映できませんでした: {type(e).__name__}: {e}"})
            tray.set_status("一時停止中" if runtime.paused else
                            "作業中" if runtime.slot.owner else
                            f"接続中 {sum(row['connected'] for row in runtime.rows())} 件")

    tasks = []
    try:
        await runtime.start()
        tasks = [asyncio.create_task(logview.serve()), asyncio.create_task(watchdog())]
        await stop.wait()
    finally:
        try:
            try:
                await runtime.stop()
            except Exception as e:
                store.add({"type": "error", "text": f"終了処理でエラーがありました: {type(e).__name__}: {e}"})
        finally:
            logview.should_exit = True
            for task in tasks[1:]:
                task.cancel()
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
            finally:
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
    from ..entry import release_agent_instance
    release_agent_instance()
    # a one-file exe passes its unpacked folder on through the environment: the new one must unpack its own
    subprocess.Popen([sys.executable, "agent"], env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"}, close_fds=True,
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                     | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))


def _main(home: Path) -> bool:
    connections = ConnectionStore(home, on_log=LogStore(home / "logs").add)
    invite = None
    if not connections.list():
        invite = ask_invite()
        if invite is None:
            return False
        connections.add(make_invite(invite.url, invite.token, invite.name))
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
