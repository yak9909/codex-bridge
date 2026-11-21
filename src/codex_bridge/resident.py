"""The resident app: one tray icon, one local screen, both roles (lender and borrower) inside one process."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Awaitable, Callable

import uvicorn
import yaml
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders
from starlette.routing import Route

from . import __version__, updater
from .common.applog import AppLog
from .host.config import load_host_config
from .host.policy import PolicyStore
from .host.role import HostStartError, bind_socket
from .host.web.app import APP_PAGE, _guard, admin_router

UI_PORT = 8766
HANDOVER_RECOVERY_TIMEOUT = 60.0
BACKUP_CLEANUP_DELAY = 60.0
TITLE = "codex-bridge"
DEFAULT_MUTEX = r"Local\codex-bridge-agent"
CSP = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
SIDES = {"use": "使う", "lend": "貸す"}
LEVELS = {"info": "情報", "warn": "警告", "error": "失敗"}
STUB_503 = "使う側は動いていません"


def ui_port() -> int:
    try:
        return int(os.environ.get("CODEX_BRIDGE_UI_PORT") or UI_PORT)
    except ValueError:
        return UI_PORT


def mutex_name() -> str:
    return os.environ.get("CODEX_BRIDGE_MUTEX") or DEFAULT_MUTEX


def host_config_path() -> Path:
    """host.yaml next to the exe (frozen) or in the working directory; CODEX_BRIDGE_HOST_CONFIG overrides it."""
    if os.environ.get("CODEX_BRIDGE_HOST_CONFIG"):
        return Path(os.environ["CODEX_BRIDGE_HOST_CONFIG"])
    base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path.cwd()
    return base / "host.yaml"


# ---- the local app -----------------------------------------------------------
class _Guard:
    """Loopback only, writes need X-Codex-Bridge: 1, and the response headers of the lender's screen."""

    def __init__(self, app, writes_ready: Callable[[], bool] = lambda: True) -> None:
        self.app = app
        self.writes_ready = writes_ready

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = [value.decode("latin-1").lower() for key, value in scope["headers"] if key == b"host"]
        match = re.fullmatch(r"(?:127\.0\.0\.1|localhost)(?::([0-9]{1,5}))?", headers[0] if len(headers) == 1 else "")
        client = scope.get("client")
        refusal = None
        if (client is None or client[0] != "127.0.0.1" or not match
                or (match.group(1) is not None and not 1 <= int(match.group(1)) <= 65535)):
            refusal = "forbidden host"
        elif scope["method"] not in SAFE_METHODS and (b"x-codex-bridge", b"1") not in scope["headers"]:
            refusal = "X-Codex-Bridge: 1 is required"

        async def send_with_headers(message) -> None:
            if message["type"] == "http.response.start":
                out = MutableHeaders(scope=message)
                out["Cache-Control"] = "no-store"
                out["X-Content-Type-Options"] = "nosniff"
                out["Content-Security-Policy"] = CSP
            await send(message)

        if refusal is not None:
            await JSONResponse({"detail": refusal}, status_code=403)(scope, receive, send_with_headers)
            return
        if scope["method"] not in SAFE_METHODS and not self.writes_ready():
            await JSONResponse({"detail": "アップデート後の起動中です"}, status_code=503)(scope, receive, send_with_headers)
            return
        await self.app(scope, receive, send_with_headers)


class _AdminDispatch:
    """/admin/* goes to the host's admin router while the host runs, else to a stub that says it is not running."""

    def __init__(self, state: "Resident") -> None:
        self.state = state
        self._host = None
        self._real: FastAPI | None = None
        self._stub = self._build_stub()

    def _build_stub(self) -> FastAPI:
        state = self.state
        router = APIRouter(prefix="/admin", dependencies=[Depends(_guard)])

        @router.get("/")
        async def index():
            return RedirectResponse(state.page_url, status_code=302)

        @router.get("/api/overview")
        async def overview():
            return {"configured": state.config_path.exists(), "running": False, "error": state.use_error()}

        @router.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
        async def not_running(rest: str):
            return JSONResponse({"detail": STUB_503}, status_code=503)

        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        app.include_router(router)
        return app

    async def __call__(self, scope, receive, send) -> None:
        host = self.state.host_role.host if self.state.host_role.running else None
        if host is None:
            app = self._stub
        else:
            if host is not self._host:
                self._real = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
                self._real.include_router(admin_router(host, page_url=self.state.page_url))
                self._host = host
            app = self._real
        await app(scope, receive, send)


def create_resident_app(state: "Resident") -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(_Guard, writes_ready=lambda: not state.starting_from_update)

    @app.get("/")
    async def index():
        return FileResponse(APP_PAGE / "index.html")

    @app.get("/app/api/state")
    async def get_state():
        return {"use": {"configured": state.config_path.exists(), "running": state.host_role.running,
                        "error": state.use_error()},
                "lend": {"connections": state.agent.counts()[1], "error": state.agent.error}, "paused": state.paused,
                "update": state.update_status()}

    @app.post("/app/api/update/check", status_code=202)
    async def check_update():
        state.check_update(manual=True)
        return state.update_status()

    @app.post("/app/api/update/install", status_code=202)
    async def install_update():
        if not state.prepare_update():
            raise HTTPException(409, "今はアップデートを始められません")
        return state.update_status()

    @app.post("/app/api/update/cancel")
    async def cancel_update():
        if not await state.cancel_update():
            raise HTTPException(409, "今はアップデートをやめられません")
        return state.update_status()

    @app.post("/app/api/update/mode")
    async def update_mode(body: dict):
        mode = body.get("mode")
        if mode not in ("confirm", "auto"):
            raise HTTPException(400, "毎回確認するか、自動で更新するかを選んでください")
        try:
            updater.save_mode(state.update_settings, mode)
        except OSError as e:
            raise HTTPException(500, "アップデートの設定を保存できませんでした") from e
        state.update_mode = mode
        if mode == "auto":
            state.prepare_update()
        return state.update_status()

    @app.post("/app/api/pause")
    async def pause(body: dict):
        if not isinstance(body.get("paused"), bool):
            raise HTTPException(400, "paused は true か false です")
        await state.set_paused(body["paused"])
        return {"paused": state.paused}

    @app.post("/app/api/host/setup", status_code=202)
    async def setup():
        if state.update_waiting:
            raise HTTPException(409, updater.SWITCH_WAIT)
        if state.host_role.running:
            raise HTTPException(409, "使う側はもう動いています")
        if state.setup_running:
            raise HTTPException(409, "初期設定はもう開いています")
        try:
            state.start_setup()
        except OSError as e:
            text = f"初期設定を始められませんでした: {e}"
            state.applog.add("use", "error", text)
            raise HTTPException(500, text) from e
        return {"started": True}

    @app.get("/app/api/logs")
    async def logs(side: str | None = None, level: str | None = None, q: str | None = None, limit: int = 1000):
        return state.applog.entries(side or None, level or None, q or None, limit)

    @app.get("/app/api/logs/export")
    async def export():
        lines = []
        for e in state.applog.entries(limit=100000):
            text = " ".join(e["text"].split("\n")).replace("\t", " ")
            lines.append("\t".join((time.strftime("%m/%d %H:%M:%S", time.localtime(e["t"])),
                                    SIDES.get(e.get("side"), "?"), LEVELS.get(e.get("level"), "?"), text)))
        return PlainTextResponse("".join(line + "\n" for line in lines),
                                 headers={"Content-Disposition": 'attachment; filename="codex-bridge-log.txt"'})

    @app.post("/app/api/logs/open-folder")
    async def open_folder():
        try:
            os.startfile(state.applog.path.parent)
        except OSError as e:
            raise HTTPException(500, f"フォルダを開けませんでした: {e}") from e
        return {"ok": True}

    app.mount("/app/static", StaticFiles(directory=APP_PAGE), name="app-static")
    app.mount("/lend", state.agent.ui_app())
    dispatch = _AdminDispatch(state)
    app.router.routes.append(Route("/admin", dispatch))
    app.router.routes.append(Route("/admin/{rest:path}", dispatch))
    return app


# ---- tray --------------------------------------------------------------------
class Tray:
    def __init__(self, *, status: Callable[[], str], paused: Callable[[], bool], on_open: Callable[[], None],
                 on_pause_toggle: Callable[[], None], on_quit: Callable[[], None]) -> None:
        import pystray
        from PIL import Image, ImageDraw
        icon_path = Path(__file__).with_name("assets") / "codex-bridge.ico"
        try:
            image = Image.open(icon_path)
            image.load()
        except (OSError, ValueError):
            image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            ImageDraw.Draw(image).rounded_rectangle((4, 4, 60, 60), 14, fill=(47, 111, 222, 255))
        self._status = status
        self.icon = pystray.Icon("codex-bridge", image, TITLE, menu=pystray.Menu(
            pystray.MenuItem(lambda item: status(), None, enabled=False),
            pystray.MenuItem("画面を開く", lambda: on_open(), default=True),
            pystray.MenuItem(lambda item: "再開" if paused() else "一時停止", lambda: on_pause_toggle()),
            pystray.MenuItem("終了", lambda: on_quit()),
        ))

    def refresh(self) -> None:
        try:
            self.icon.title = f"{TITLE}: {self._status()}"
            self.icon.update_menu()
        except Exception:
            pass

    def notify(self, title: str, body: str) -> None:
        try:
            self.icon.notify(body[:200], title)
        except Exception:
            pass  # Windows notification policy must never fail a request

    def run_detached(self) -> None:
        import threading
        threading.Thread(target=self.icon.run, daemon=True).start()

    def stop(self) -> None:
        self.icon.stop()


class UiServer:
    """The screen's server on a socket we bind ourselves (a taken port is an error here, not a sys.exit)."""

    def __init__(self, app, port: int) -> None:
        try:
            self._sock = bind_socket("127.0.0.1", port)
        except OSError as e:
            raise RuntimeError(f"画面用のポート {port} は使われています") from e
        # log_config=None: a windowed exe has no sys.stdout for uvicorn's logging setup.
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                                    log_config=None, timeout_graceful_shutdown=5))

    @property
    def should_exit(self) -> bool:
        return self.server.should_exit

    @should_exit.setter
    def should_exit(self, value: bool) -> None:
        self.server.should_exit = value

    async def serve(self) -> None:
        try:
            await self.server.serve(sockets=[self._sock])
        finally:
            self._sock.close()


# ---- the resident ------------------------------------------------------------
class Resident:
    def __init__(self, *, applog: AppLog, agent, host_role, config_path: Path, port: int = UI_PORT,
                 tray_factory: Callable[["Resident"], Tray] | None = None,
                 server_factory: Callable[[FastAPI, int], UiServer] = UiServer) -> None:
        self.applog, self.agent, self.host_role = applog, agent, host_role
        self.config_path, self.port = Path(config_path), port
        self.page_url = f"http://127.0.0.1:{port}/"
        self.tray_factory = tray_factory or self._make_tray
        self.server_factory = server_factory
        self.tray: Tray | None = None
        self.setup_proc = None
        self.setup_task: asyncio.Task | None = None
        self.setup_error: str | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.stop_event: asyncio.Event | None = None
        self._background: list[asyncio.Task] = []
        self._seen_approvals: set[str] = set()
        self.retry_interval = 10.0
        self._retry: asyncio.Task | None = None
        self._watched_host = None
        self.update_settings = self.config_path.with_name("update-settings.json")
        self.update_data = self.config_path.parent / "data"
        self.update_mode = updater.mode(self.update_settings)
        self._cancelled_version = updater.cancelled_version(self.update_settings)
        self.update_exe: Path | None = Path(sys.executable).resolve() if getattr(sys, "frozen", False) else None
        self.update_release: updater.Release | None = None
        self.update_phase = "idle"
        self.update_message = ""
        self._update_job: asyncio.Task | None = None
        self._installing: asyncio.Task | None = None
        self._cancel_requested = False
        self._handover = False
        self._recovery_failed = False
        self.starting_from_update = False
        self._ui_server: UiServer | None = None
        self._ui_serving: asyncio.Task | None = None
        self.host_role.update_waiting = lambda: self.update_waiting

    @property
    def update_waiting(self) -> bool:
        return self.update_phase == "waiting"

    def update_status(self) -> dict:
        return {"version": __version__, "latest": self.update_release.version if self.update_release else None,
                "phase": self.update_phase, "message": self.update_message, "mode": self.update_mode,
                "can_install": self.update_exe is not None and not self._recovery_failed,
                "suppressed": bool(self.update_release and self.update_release.version == self._cancelled_version),
                "can_cancel": self.update_waiting and self._installing is None and not self._cancel_requested}

    def check_update(self, *, manual: bool = False) -> None:
        if self._recovery_failed:
            return
        if self._update_job is None or self._update_job.done():
            if manual and self._cancelled_version is not None:
                self._remember_cancelled_version(None)
            self.update_phase, self.update_message = "checking", ""
            self._update_job = asyncio.create_task(self._check_update())

    async def _check_update(self) -> None:
        previous = self.update_release
        try:
            self.update_release = await asyncio.to_thread(updater.check_release)
        except updater.UpdateError as e:
            self.update_phase, self.update_message = "failed", str(e)
            return
        except Exception:
            self.update_phase, self.update_message = "failed", "アップデートを確かめられませんでした"
            return
        if self.update_release is None:
            self.update_phase, self.update_message = "current", ""
            return
        if self.update_release.version == self._cancelled_version:
            self.update_phase = "cancelled"
            return
        self.update_phase = "available"
        if self.update_mode == "auto" and self.update_exe is not None:
            self.update_phase = "preparing"
            await self._prepare_update(self.update_release, self.update_exe)
        elif self.update_release != previous:
            self.notify("codex-bridge", f"新しい版 v{self.update_release.version} があります")

    def prepare_update(self) -> bool:
        if (self._recovery_failed or self.update_phase not in ("available", "failed")
                or not self.update_release or self.update_exe is None
                or self.update_release.version == self._cancelled_version
                or (self._update_job is not None and not self._update_job.done())):
            return False
        self.update_phase, self.update_message = "preparing", ""
        self._update_job = asyncio.create_task(self._prepare_update(self.update_release, self.update_exe))
        return True

    def _update_busy(self) -> bool:
        runtime = getattr(self.agent, "runtime", None)
        host = self.host_role.host
        return (self.setup_running or bool(runtime and (runtime.moving or runtime.slot.count))
                or bool(host and host.mgr.blocks_update()))

    async def _announce_update(self) -> None:
        runtime = getattr(self.agent, "runtime", None)
        if runtime is not None:
            runtime.slot.update_waiting = self.update_waiting
            await runtime.slot.announce()

    def _remember_cancelled_version(self, version: str | None) -> None:
        self._cancelled_version = version
        try:
            updater.save_cancelled_version(self.update_settings, version)
        except OSError as e:
            self.applog.add("use", "error", f"アップデートの設定を保存できませんでした: {e}")

    async def cancel_update(self) -> bool:
        job = self._update_job
        if not job or job.done() or not self.update_status()["can_cancel"]:
            return False
        self._cancel_requested = True
        await asyncio.shield(job)
        return True

    async def _release_held_updates(self) -> None:
        if (host := self.host_role.host) is not None:
            try:
                await host.mgr.resume_update_waits(release_local=True)
            except Exception as e:
                self.applog.add("use", "error", f"アップデート待ちの依頼を処理できませんでした: {e}")

    async def _prepare_update(self, release: updater.Release, exe: Path) -> None:
        package = None
        self._cancel_requested = False
        try:
            await asyncio.to_thread(updater.check_install_folder, exe)
            package = await asyncio.to_thread(updater.prepare_release, release, self.update_data)
            self.update_phase = "waiting"
            await self._announce_update()
            while not self._cancel_requested and self._update_busy():
                await asyncio.sleep(0.25)
            if self._cancel_requested:
                self._remember_cancelled_version(release.version)
                self.update_phase = "cancelled"
                await self._announce_update()
                await self._release_held_updates()
                return
            kwargs = {}
            if self._ui_serving is not None:
                loop = asyncio.get_running_loop()

                def before_launch():
                    asyncio.run_coroutine_threadsafe(self._release_update_handover(), loop).result()

                kwargs["before_launch"] = before_launch
            self._installing = asyncio.create_task(asyncio.to_thread(updater.install_release, exe, package, **kwargs))
            await asyncio.shield(self._installing)
        except Exception as e:
            self.update_phase = "failed"
            self.update_message = (str(e) if isinstance(e, updater.UpdateError)
                                   else f"アップデートを適用できませんでした: {e}")
            self.applog.add("use", "error", self.update_message)
            await self._announce_update()
            try:
                if self._handover:
                    await self._restore_update_handover()
            finally:
                await self._release_held_updates()
        else:
            self.request_quit()
        finally:
            self._installing = None
            self._cancel_requested = False
            if package is not None and self.update_phase in ("failed", "cancelled"):
                shutil.rmtree(package.parent, ignore_errors=True)

    async def _release_update_handover(self) -> None:
        from .entry import release_agent_instance

        self._handover = True
        await self._stop_ui()
        # Keep the borrower connections alive so that rollback can send the held tasks.
        release_agent_instance()

    async def _restore_update_handover(self) -> None:
        from .entry import claim_agent_instance

        loop = asyncio.get_running_loop()
        deadline = loop.time() + HANDOVER_RECOVERY_TIMEOUT
        reasons = []
        try:
            while not self.stop_event.is_set():
                if claim_agent_instance():
                    break
                remaining = deadline - loop.time()
                if remaining <= 0:
                    reasons.append("二重起動を防ぐミューテックスを取り直せませんでした")
                    break
                await asyncio.sleep(min(0.25, remaining))

            # The original port is exclusive. Try it even if the mutex could not be recovered.
            while not self.stop_event.is_set():
                try:
                    self._start_ui()
                    break
                except (OSError, RuntimeError) as e:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        reasons.append(f"画面の待ち受けを再開できませんでした（{e}）")
                        break
                    await asyncio.sleep(min(1.0, remaining))
        finally:
            self._handover = False
        if reasons:
            self._recovery_failed = True
            reason = "。".join(reasons)
            self.update_message = f"{self.update_message}。{reason}" if self.update_message else reason
            self.applog.add("use", "error", reason)
            self.notify("codex-bridge", reason)

    def _clean_old_exes(self) -> None:
        if self.update_exe is not None and self._installing is None and not self._recovery_failed:
            updater.clean_old_exes(self.update_exe)

    async def _retry_old_exes_after_start(self) -> None:
        await asyncio.sleep(BACKUP_CLEANUP_DELAY)
        self._clean_old_exes()

    async def _periodic_update(self) -> None:
        self.check_update()
        while True:
            await asyncio.sleep(updater.CHECK_INTERVAL)
            self._clean_old_exes()
            self.check_update()

    # -- what the screen and the tray read
    @property
    def setup_running(self) -> bool:
        return self.setup_proc is not None

    def use_error(self) -> str | None:
        return self.host_role.error or self.setup_error

    def _host_policy(self) -> PolicyStore | None:
        host = self.host_role.host
        if host is not None:
            return host.policy
        if self.config_path.exists():
            try:
                cfg = load_host_config(self.config_path)
                return PolicyStore(cfg.data_dir / "policy.yaml")
            except (OSError, yaml.YAMLError, ValueError, TypeError, AttributeError):
                pass  # HostRole.start reports configuration problems; lending can still start.
        return None

    @property
    def paused(self) -> bool:
        policy = self._host_policy()
        return bool(self.agent.paused or (policy is not None and policy.current().paused))

    def status_line(self) -> str:
        lenders, invited, host_waiting = self.host_role.counts()
        borrowers, connections, lender_waiting = self.agent.counts()
        return (f"貸す人 {lenders}/{invited}・依頼主 {borrowers}/{connections}"
                f"・承認待ち {host_waiting + lender_waiting}"
                + (f"・{updater.SWITCH_WAIT}" if self.update_waiting else ""))

    async def set_paused(self, paused: bool) -> None:
        policy = self._host_policy()
        if policy is not None:
            policy.update(paused=paused)
        host = self.host_role.host
        if host is not None:
            host.events.notify()
        await self.agent.set_paused(paused)
        if self.tray is not None:
            self.tray.refresh()

    def notify(self, title: str, body: str) -> None:
        if self.tray is not None:
            self.tray.notify(title, body)

    def request_quit(self) -> None:
        if self.loop is not None and self.stop_event is not None:
            self.loop.call_soon_threadsafe(self.stop_event.set)

    def open_page(self) -> None:
        webbrowser.open(self.page_url)

    def _make_tray(self, resident: "Resident") -> Tray:
        def toggle() -> None:
            if self.loop is not None:
                asyncio.run_coroutine_threadsafe(self.set_paused(not self.paused), self.loop)

        return Tray(status=self.status_line, paused=lambda: self.paused, on_open=self.open_page,
                    on_pause_toggle=toggle, on_quit=self.request_quit)

    # -- the borrower's first-time setup runs in its own console window
    def start_setup(self) -> None:
        command = ([sys.executable, "host", "setup", "--from-app", "--config", str(self.config_path)]
                   if getattr(sys, "frozen", False) else
                   [sys.executable, "-m", "codex_bridge", "host", "setup", "--from-app", "--config",
                    str(self.config_path)])
        # a one-file exe passes its unpacked folder on through the environment: the new one must unpack its own
        proc = subprocess.Popen(command, cwd=str(self.config_path.parent),
                                env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"},
                                creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10))
        self.setup_proc = proc
        self.setup_error = None
        self.applog.add("use", "info", "初期設定を始めました")
        self.setup_task = asyncio.get_running_loop().create_task(self._after_setup(proc))

    async def _after_setup(self, proc) -> None:
        try:
            code = await asyncio.to_thread(proc.wait)
        finally:
            self.setup_proc = None
        # Closing the finished console with X gives a non-zero code: what counts is a usable host.yaml.
        try:
            load_host_config(self.config_path)
        except Exception:
            self.setup_error = "初期設定が終わっていません。もう一度「準備を始める」を押してください"
            self.applog.add("use", "error", f"{self.setup_error}（終了コード {code}）")
            return
        if code != 0:
            self.applog.add("use", "warn", "初期設定が途中で終わった可能性があります。"
                                           "うまく動かないときは、もう一度「準備を始める」を押してください。")
        else:
            self.applog.add("use", "info", "初期設定が終わりました")
        await self.start_host()

    async def start_host(self) -> None:
        """Start the borrower's side. A failure is kept for the screen (host_role.error), the app goes on,
        and a stopped host is tried again every retry_interval seconds until the app quits."""
        await self._try_start_host()
        if self._retry is None or self._retry.done():
            self._retry = asyncio.create_task(self._retry_host())
            self._background.append(self._retry)

    async def _try_start_host(self) -> bool:
        try:
            await self.host_role.start()
        except HostStartError:
            return False
        except Exception as e:  # never let the borrower's side take the lender's side down
            self.host_role.error = f"使う側を始められませんでした: {type(e).__name__}: {e}"
            self.applog.add("use", "error", self.host_role.error)
            return False
        host = self.host_role.host
        if host is not None and host is not self._watched_host:
            if self.agent.paused != host.policy.current().paused:
                await self.set_paused(self.paused)
            self._watched_host = host
            host.approvals.on_change.append(self._approvals_changed)
            self._background.append(asyncio.create_task(self._watch_shutdown(host)))
        return True

    async def _retry_host(self) -> None:
        while True:
            await asyncio.sleep(self.retry_interval)
            if not self._handover and not self.host_role.running:
                await self._try_start_host()

    def _approvals_changed(self) -> None:
        host = self.host_role.host
        if host is None:
            return
        for approval in host.approvals.pending():
            if approval.id not in self._seen_approvals:
                self._seen_approvals.add(approval.id)
                self.notify("codex-bridge: 承認待ち", approval.summary)

    async def _watch_shutdown(self, host) -> None:
        await host.shutdown.wait()  # /admin/api/shutdown (stop.bat)
        self.request_quit()

    async def _refresh_tray(self) -> None:
        while True:
            await asyncio.sleep(2)
            if self.tray is not None:
                self.tray.refresh()

    # -- run
    def _start_ui(self) -> None:
        self._ui_server = self.server_factory(create_resident_app(self), self.port)
        self._ui_serving = asyncio.create_task(self._ui_server.serve())
        self._ui_serving.add_done_callback(self._ui_stopped)

    def _ui_stopped(self, serving: asyncio.Task) -> None:
        if serving is self._ui_serving:
            self.stop_event.set()

    async def _stop_ui(self) -> None:
        server, serving = self._ui_server, self._ui_serving
        self._ui_server = self._ui_serving = None
        if server is None or serving is None:
            return
        server.should_exit = True
        try:
            await asyncio.wait_for(asyncio.gather(serving, return_exceptions=True), 15)
        except asyncio.TimeoutError:
            serving.cancel()

    def run(self, *, check_updates: bool = False, update_ready: Callable[[], Awaitable[None]] | None = None) -> bool:
        """Runs until quit. True: the data was moved, the exe must start again."""
        return asyncio.run(self._run(check_updates=check_updates, update_ready=update_ready))

    async def _run(self, *, check_updates: bool = False,
                   update_ready: Callable[[], Awaitable[None]] | None = None) -> bool:
        self.loop = asyncio.get_running_loop()
        self.stop_event = asyncio.Event()
        self.agent.on_moved = lambda: self.loop.call_later(1.0, self.stop_event.set)
        self.starting_from_update = update_ready is not None
        deadline = self.loop.time() + 15
        while True:
            try:
                self._start_ui()
                break
            except RuntimeError:
                if update_ready is None or self.loop.time() >= deadline:
                    raise
                await asyncio.sleep(0.25)
        try:
            if update_ready is not None:
                while not self._ui_server.server.started:
                    if self._ui_serving.done():
                        raise updater.UpdateError("画面の待ち受けを始められませんでした")
                    await asyncio.sleep(0.05)
                await update_ready()
                self.starting_from_update = False
            if self.update_exe is not None:
                updater.clean_up(self.update_exe, self.update_data)
            self.tray = self.tray_factory(self)
            self.tray.run_detached()
            policy = self._host_policy()
            if policy is not None:
                await self.agent.set_paused(policy.current().paused)
            await self.agent.start()
            if self.config_path.exists():
                await self.start_host()
            self._background.append(asyncio.create_task(self._refresh_tray()))
            if check_updates:
                self._background.append(asyncio.create_task(self._periodic_update()))
                if self.update_exe is not None:
                    self._background.append(asyncio.create_task(self._retry_old_exes_after_start()))
            await self.stop_event.wait()
        finally:
            await self._shutdown()
        return bool(getattr(self.agent, "moved", False))

    async def _shutdown(self) -> None:
        try:
            if self._installing is not None:
                await asyncio.gather(self._installing, return_exceptions=True)
            if self._update_job is not None:
                self._background.append(self._update_job)
            for task in self._background:
                task.cancel()
            await asyncio.gather(*self._background, return_exceptions=True)
            for role in (self.host_role, self.agent):
                try:
                    await role.stop()
                except Exception as e:
                    self.applog.add("use" if role is self.host_role else "lend", "error",
                                    f"終了処理でエラーがありました: {type(e).__name__}: {e}")
        finally:
            try:
                await self._stop_ui()
            finally:
                if self.tray is not None:
                    self.tray.stop()


def main(*, update_ready: Callable[[], Awaitable[None]] | None = None) -> int:
    from .agent.main import ensure_std_streams, run_guarded, start_again
    from .agent.setup import data_root
    from .host.role import HostRole
    from .agent.main import AgentRole

    ensure_std_streams()
    home = data_root()
    handover_complete = False

    async def finish_handover() -> None:
        nonlocal handover_complete
        await update_ready()
        handover_complete = True

    def run() -> bool:
        applog = AppLog(home / "logs" / "app.jsonl")
        port = ui_port()
        resident: list[Resident] = []
        agent = AgentRole(home, applog, notify=lambda title, body: resident[0].notify(title, body))
        host_role = HostRole(host_config_path(), applog, page_url=f"http://127.0.0.1:{port}/")
        resident.append(Resident(applog=applog, agent=agent, host_role=host_role,
                                 config_path=host_config_path(), port=port))
        app = resident[0]
        return app.run(check_updates=True, update_ready=finish_handover if update_ready is not None else None)

    if update_ready is not None:
        moved = run_guarded(run, home, should_report=lambda: handover_complete)
    else:
        moved = run_guarded(run, home)
    if moved:
        start_again()
    return 0
