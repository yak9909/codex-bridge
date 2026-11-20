"""The lender's role: runs on the friend's PC (tray app)."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from .. import __version__
from ..common.applog import AppLog
from ..common.invite import parse_invite
from ..common.protocol import FINAL_STATES
from .appserver import AppServer, codex_command
from .control_client import ControlClient
from .connections import ConnectionStore
from .logview import LogStore
from .models import apply_config_default, parse_model_list
from .runner import AgentRunner, TaskSlot
from .sessions import SessionRegistry
from .settings import SettingsApplier, SettingsStore, hello_fields, resolve_workdir, move_data_root
from .setup import (MIN_CODEX_VERSION, choose_codex, data_root, download_codex, make_guard, run_login,
                    user_mcp_names)
from .ui import create_ui_app
from .worklog import WorkLog

TITLE = "codex-bridge"
SHUTDOWN_TIMEOUT = 5.0


# ---- small dialogs (tkinter) ---------------------------------------------
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

    def __init__(self, home: Path, *, app_factory, versions: dict, initial_backoff=1.0, on_log=None, before_add=None):
        self.home = Path(home)
        self.before_add = before_add  # async; runs first when a connection is added (the app resolves Codex here)
        self.on_log = on_log or (lambda m: None)
        self.store = ConnectionStore(home, on_log=self.on_log)  # migrate before projecting the shared store
        self.settings = SettingsStore(home / "settings.json", legacy_limits=home / "limits.json", shared=True)
        self.settings.update({})
        self.app_factory, self.versions = app_factory, versions
        self.initial_backoff = initial_backoff
        self.slot = TaskSlot()
        self.catalog: list[dict] | None = None
        self._catalog_stale = False
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
                    and not any(c.thread_id for c in runner.currents.values())):
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
            if self.catalog is None or self._catalog_stale:
                await self._load_catalog(app)
            await runner.refresh_usage()

        runner = AgentRunner(app, make_guard(self.versions.get("auth_mode", "chatgpt")),
                             SessionRegistry(root / "sessions.json"), send=None, slot=self.slot,
                             connection_id=connection.id, host_name=urlsplit(connection.invite.url).netloc,
                             prepare=prepare, on_log=lambda m: self.on_log({**m, **runner.connection_fields()}),
                             worklog=WorkLog(root / "logs" / "tasks", current["log_retention_days"]))
        runner.apply_settings(current)
        runner.catalog = self.catalog
        runner.last_usage = self.slot.last_usage
        runner.log_visibility = current["log_visibility"]
        last_status = [None]

        def on_status(text):  # connection state changes only: the client repeats its status on every retry
            if text != last_status[0]:
                last_status[0] = text
                self.on_log({"type": "connection", "text": f"{runner.host_name}: {text}"})

        client = ControlClient(connection.invite, runner,
                               hello=lambda: {**self.versions, **hello_fields(runner.settings, runner.catalog)},
                               initial_backoff=self.initial_backoff, on_status=on_status)
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

    async def _load_catalog(self, app) -> None:
        try:
            catalog = parse_model_list(await app.model_list())
        except Exception as e:
            self._error("Codex のモデルの一覧を取得できませんでした", e)
            return
        try:
            config = await app.config_read()
        except Exception:
            pass
        else:
            catalog = apply_config_default(catalog, config)
        self.catalog = catalog
        self._catalog_stale = False
        runners = list(self.runners.values())
        for runner in runners:
            runner.catalog = catalog
        for runner in runners:
            try:
                await runner.send_models()
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
        parse_invite(code)  # a bad code must not start a Codex download
        if self.before_add is not None:
            await self.before_add()
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
        parse_invite(code)  # a bad code must not start a Codex download
        async with self.slot.lock:
            self._require_idle(id)
            if self.before_add is not None:
                await self.before_add()  # retry Codex setup if startup failed
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
        self._catalog_stale = True
        runner = self.runners[id]
        runner.apply_settings({**self.settings.get(), **saved})
        await self.clients[id].refresh_hello()
        await runner.send_models()  # the borrower's read-only view of this connection's settings
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
            await runner.send_models()  # the over-limit behaviour (limits.force) is part of the lender's view

        await self.slot.announce()

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
            if self.slot.count:
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
                 "invite_problem": self.clients[id].invite_problem, "busy": bool(runner.active_task_ids()),
                 "active_task_ids": runner.active_task_ids()}
                for id, runner in self.runners.items()]


def applog_line(message: dict) -> tuple[str, str] | None:
    """(level, text) for the shared app log, or None when a runner message is not worth a line.
    Never includes prompts or file contents."""
    kind = message.get("type")
    if kind == "error":
        return "error", str(message.get("text", ""))
    if kind == "task_state":
        task_id, state = message.get("task_id"), message.get("state")
        if state in FINAL_STATES:
            return "info", f"{task_id} が終わりました"
        if state == "running":
            return "info", f"{task_id} を受け付けました"
    if kind == "connection":
        return "warn", str(message.get("text", ""))
    return None


def resolve_codex(home: Path) -> Path | None:
    """Find a usable Codex, or ask to download one. Blocks (dialogs, download): call it from a worker thread.
    None means the user declined; a failed download raises."""
    on_path = shutil.which("codex")
    bundled = home / "bin" / "codex.exe"
    codex = choose_codex(Path(on_path) if on_path else None, bundled if bundled.exists() else None)
    if codex is not None:
        return codex
    need = ".".join(map(str, MIN_CODEX_VERSION))
    if not ask_yes_no(f"Codex（コマンド版、{need} 以降）が見つからないか、古い版です。公式サイト"
                      "（GitHub の openai/codex）から最新版をダウンロードしてよいですか？（約 200MB）\n"
                      "※ あなたが普段使っている Codex には影響しません。"):
        return None
    return download_codex(home / "bin")


class AgentRole:
    """The lender's side inside the resident app. It runs with zero connections; Codex is resolved when the
    first connection is added (or at start when there are connections already)."""

    def __init__(self, home: Path, applog: AppLog, notify: Callable[[str, str], None]) -> None:
        self.home, self.applog, self.notify = Path(home), applog, notify  # notify(title, body): a tray balloon
        self.store = LogStore(self.home / "logs")
        self.cmd: list[str] = []  # Codex command, known once resolved
        self.error: str | None = None  # why Codex could not be set up, for the screen
        self.moved = False  # the data was moved: the app must start again
        self.on_moved: Callable[[], None] = lambda: None
        self.versions = {"agent_version": __version__, "codex_version": "?", "auth_mode": "unknown"}
        self._notified = None
        self._codex_lock = asyncio.Lock()
        self._watchdog: asyncio.Task | None = None
        self.runtime = AgentConnections(self.home, app_factory=self._make_app, versions=self.versions,
                                        on_log=self._on_log, before_add=self._before_add)

    @property
    def paused(self) -> bool:
        return self.runtime.paused

    async def set_paused(self, paused: bool) -> None:
        await self.runtime.set_paused(paused)

    def counts(self) -> tuple[int, int, int]:
        """(borrowers connected, connections, approvals waiting) for the tray."""
        rows = self.runtime.rows()
        return (sum(bool(r["connected"]) for r in rows), len(rows),
                sum(len(r.pending_approvals) for r in self.runtime.runners.values()))

    def _make_app(self, connection, current):
        app = AppServer(self.cmd, connection.invite.url, connection.invite.token,
                        resolve_workdir(current, self.home), disabled_mcp=user_mcp_names(),
                        features=current["features"])
        original_start = app.start

        async def authenticated_start():
            await original_start()
            mode = await app.auth_mode()
            if mode == "none":
                await app.stop()
                await asyncio.to_thread(show_info, "Codex にログインします。開いた画面の案内に従って ChatGPT でログインしてください。")
                await asyncio.to_thread(run_login, self.cmd)
                await original_start()
                mode = await app.auth_mode()
                if mode == "none":
                    raise RuntimeError("Codex にログインできませんでした。もう一度起動してください。")
        app.start = authenticated_start
        return app

    def _on_log(self, message: dict) -> None:
        if message.get("type") in {"settings", "error"}:
            self.store.add(message)
        if (line := applog_line(message)) is not None:
            self.applog.add("lend", *line)
        if message.get("type") == "task_state" and message.get("state") == "awaiting_lender":
            runner = self.runtime.runners.get(message.get("connection_id"))
            pending = next((p for p in runner.pending_approvals if p["task_id"] == message.get("task_id")), None) if runner else None
            key = (runner.connection_id, pending["task_id"], pending["created"]) if pending else None
            if key is not None and key != self._notified:
                self._notified = key
                try:
                    self.notify("codex-bridge: 事前承認待ち", pending["prompt"][:200])
                except Exception:
                    pass

    async def _ensure_codex(self) -> None:
        if self.cmd:
            return
        async with self._codex_lock:
            if self.cmd:
                return
            try:
                codex = await asyncio.to_thread(resolve_codex, self.home)
            except Exception as e:
                self.error = f"Codex をダウンロードできませんでした: {e}"
                raise RuntimeError(self.error) from e
            if codex is None:
                self.error = "Codex が見つからないため、貸す人の接続を始められません"
                raise RuntimeError(self.error)
            cmd = codex_command(codex)
            for runner in self.runtime.runners.values():
                runner.app.codex_cmd = list(cmd)  # connections saved before Codex was known
            self.cmd = cmd
            self.versions["codex_version"] = await asyncio.to_thread(codex_version, cmd)
            self.error = None

    async def _before_add(self) -> None:
        await self._ensure_codex()
        if not self.runtime.started:
            await self._begin()

    async def _begin(self) -> None:
        await self.runtime.start()
        if self._watchdog is None:
            self._watchdog = asyncio.create_task(self._watch())

    async def start(self) -> None:
        if self.runtime.runners:
            try:
                await self._ensure_codex()
            except RuntimeError as e:  # no runner starts; adding a connection tries again
                self.error = str(e)
                self.applog.add("lend", "error", self.error)
                try:
                    self.notify("codex-bridge", self.error)
                except Exception:
                    pass
                return
        await self._begin()

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(5)
            try:
                await self.runtime.settle()
            except Exception as e:
                self.store.add({"type": "error", "text": f"設定を反映できませんでした: {type(e).__name__}: {e}"})

    async def stop(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
            await asyncio.gather(self._watchdog, return_exceptions=True)
            self._watchdog = None
        try:
            await self.runtime.stop()
        except Exception as e:
            self.store.add({"type": "error", "text": f"終了処理でエラーがありました: {type(e).__name__}: {e}"})

    async def _move_data(self, new: Path):
        left = await self.runtime.move_data(new)
        self.moved = True
        self.on_moved()
        return left

    def ui_app(self):
        """The lender's local API (mounted at /lend in the resident app)."""
        return create_ui_app(None, self.runtime.settings, None, versions=self.versions, data_root=self.home,
                             connections=self.runtime, on_settings=self.runtime.apply_shared,
                             on_move=self._move_data, on_pause=self.set_paused)


def ensure_std_streams() -> None:
    """The windowed exe has no console (sys.stdout/stderr are None); give libraries somewhere to write."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")


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
