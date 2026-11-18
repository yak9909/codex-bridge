"""The resident app: one tray icon, one local screen, both roles (lender and borrower) inside one process."""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Callable

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders
from starlette.routing import Route

from .common.applog import AppLog
from .host.config import load_host_config
from .host.role import HostStartError, bind_socket
from .host.web.app import APP_PAGE, _guard, admin_router

UI_PORT = 8766
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

    def __init__(self, app) -> None:
        self.app = app

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
    app.add_middleware(_Guard)

    @app.get("/")
    async def index():
        return FileResponse(APP_PAGE / "index.html")

    @app.get("/app/api/state")
    async def get_state():
        return {"use": {"configured": state.config_path.exists(), "running": state.host_role.running,
                        "error": state.use_error()},
                "lend": {"connections": state.agent.counts()[1]}, "paused": state.paused}

    @app.post("/app/api/pause")
    async def pause(body: dict):
        if not isinstance(body.get("paused"), bool):
            raise HTTPException(400, "paused は true か false です")
        await state.set_paused(body["paused"])
        return {"paused": state.paused}

    @app.post("/app/api/host/setup", status_code=202)
    async def setup():
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

    # -- what the screen and the tray read
    @property
    def setup_running(self) -> bool:
        return self.setup_proc is not None

    def use_error(self) -> str | None:
        return self.host_role.error or self.setup_error

    @property
    def paused(self) -> bool:
        host = self.host_role.host if self.host_role.running else None
        return bool(self.agent.paused or (host is not None and host.policy.current().paused))

    def status_line(self) -> str:
        lenders, invited, host_waiting = self.host_role.counts()
        borrowers, connections, lender_waiting = self.agent.counts()
        return (f"貸す人 {lenders}/{invited}・依頼主 {borrowers}/{connections}"
                f"・承認待ち {host_waiting + lender_waiting}")

    async def set_paused(self, paused: bool) -> None:
        host = self.host_role.host if self.host_role.running else None
        if host is not None:
            host.policy.update(paused=paused)
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
        and the start is tried again every retry_interval seconds until the host runs or the app quits."""
        if not await self._try_start_host():
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
        if host is not None:
            host.approvals.on_change.append(self._approvals_changed)
            self._background.append(asyncio.create_task(self._watch_shutdown(host)))
        return True

    async def _retry_host(self) -> None:
        while not self.host_role.running:
            await asyncio.sleep(self.retry_interval)
            if self.host_role.running or await self._try_start_host():
                return

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
    def run(self) -> bool:
        """Runs until quit. True: the data was moved, the exe must start again."""
        return asyncio.run(self._run())

    async def _run(self) -> bool:
        self.loop = asyncio.get_running_loop()
        self.stop_event = asyncio.Event()
        self.agent.on_moved = lambda: self.loop.call_later(1.0, self.stop_event.set)
        server = self.server_factory(create_resident_app(self), self.port)
        serving = asyncio.create_task(server.serve())
        serving.add_done_callback(lambda task: self.stop_event.set())
        self.tray = self.tray_factory(self)
        self.tray.run_detached()
        try:
            await self.agent.start()
            if self.config_path.exists():
                await self.start_host()
            self._background.append(asyncio.create_task(self._refresh_tray()))
            await self.stop_event.wait()
        finally:
            await self._shutdown(server, serving)
        return bool(getattr(self.agent, "moved", False))

    async def _shutdown(self, server, serving: asyncio.Task) -> None:
        try:
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
            server.should_exit = True
            try:
                await asyncio.wait_for(asyncio.gather(serving, return_exceptions=True), 15)
            except asyncio.TimeoutError:
                serving.cancel()
            finally:
                if self.tray is not None:
                    self.tray.stop()


def main() -> None:
    from .agent.main import ensure_std_streams, run_guarded, start_again
    from .agent.setup import data_root
    from .host.role import HostRole
    from .agent.main import AgentRole

    ensure_std_streams()
    home = data_root()

    def run() -> bool:
        applog = AppLog(home / "logs" / "app.jsonl")
        port = ui_port()
        resident: list[Resident] = []
        agent = AgentRole(home, applog, notify=lambda title, body: resident[0].notify(title, body))
        host_role = HostRole(host_config_path(), applog, page_url=f"http://127.0.0.1:{port}/")
        resident.append(Resident(applog=applog, agent=agent, host_role=host_role,
                                 config_path=host_config_path(), port=port))
        return resident[0].run()

    if run_guarded(run, home):
        start_again()
