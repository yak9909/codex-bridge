"""The borrower's role as a start/stop object inside the resident app (no console output)."""

from __future__ import annotations

import asyncio
import errno
import socket
import sys
from pathlib import Path

import uvicorn
import yaml

from ..common.applog import AppLog
from .app import Host, build_host, create_local_app, create_public_app
from .config import effective_share_root, load_host_config

START_TIMEOUT = 10.0
STOP_TIMEOUT = 10.0
_IN_USE = {errno.EADDRINUSE, 10048, 10013}  # 10013: WSAEACCES, what an exclusive bind reports on Windows


class HostStartError(Exception):
    """The reason (Japanese, shown on the screen) why the borrower side could not start."""


def bind_socket(host: str, port: int) -> socket.socket:
    """Bind a listening socket ourselves, so that a taken port is an error here and not a sys.exit inside uvicorn."""
    sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET)
    try:
        if sys.platform == "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)  # SO_REUSEADDR would share the port
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
    except OSError:
        sock.close()
        raise
    return sock


class HostRole:
    def __init__(self, config_path: Path, applog: AppLog, page_url: str | None = None) -> None:
        self.config_path = Path(config_path)
        self.applog = applog
        self.page_url = page_url  # where /admin/ on the local port sends the browser
        self.host: Host | None = None
        self.running = False
        self.error: str | None = None
        self._servers: list[uvicorn.Server] = []
        self._tasks: list[asyncio.Task] = []
        self._stopping = False

    async def start(self) -> None:
        if self.running:
            return
        self.error = None
        try:
            await self._start()
        except HostStartError as e:
            await self._teardown()
            self.error = str(e)
            self.applog.add("use", "error", self.error)
            raise
        self.running = True
        self.applog.add("use", "info", "使う側を始めました")

    async def _start(self) -> None:
        try:
            cfg = load_host_config(self.config_path)
        except (OSError, yaml.YAMLError, ValueError, TypeError, AttributeError) as e:
            raise HostStartError(f"設定ファイル（{self.config_path.name}）を読めません: {e}") from e
        for root in (cfg.share_root, effective_share_root(cfg)):
            if root is not None and cfg.data_dir.resolve().is_relative_to(root.resolve()):
                raise HostStartError("data_dir は共有フォルダの外に置いてください")
        try:
            pub_host, pub_port = cfg.public_bind.rsplit(":", 1)
            pub_port = int(pub_port)
        except ValueError as e:
            raise HostStartError(f"public_bind の書き方が正しくありません: {cfg.public_bind}") from e
        try:
            host = build_host(cfg, config_path=self.config_path)
        except Exception as e:
            raise HostStartError(f"使う側を始められませんでした: {type(e).__name__}: {e}") from e
        self.host = host
        host.mgr.log = lambda level, text: self.applog.add("use", level, text)
        host.approvals.log = host.mgr.log
        public = uvicorn.Server(uvicorn.Config(create_public_app(host), host=pub_host, port=pub_port,
                                               log_level="warning", log_config=None, ws_max_size=64 * 1024 * 1024))
        local = uvicorn.Server(uvicorn.Config(create_local_app(host, page_url=self.page_url), host="127.0.0.1",
                                              port=cfg.local_port, log_level="warning", log_config=None,
                                              timeout_graceful_shutdown=5))
        sockets: list[socket.socket] = []
        try:
            for name, (addr, port) in (("public", (pub_host, pub_port)), ("local", ("127.0.0.1", cfg.local_port))):
                try:
                    sockets.append(bind_socket(addr, port))
                except OSError as e:
                    if e.errno in _IN_USE or getattr(e, "winerror", None) in _IN_USE:
                        raise HostStartError(f"ポート {port} は使われています") from e
                    raise HostStartError(f"{addr}:{port} で待ち受けられません"
                                         f"（Tailscale につながっているか確かめてください）: {e}") from e
        except BaseException:
            for sock in sockets:
                sock.close()
            raise
        self._servers = [public, local]
        self._stopping = False
        self._tasks = [asyncio.create_task(server.serve(sockets=[sock]))
                       for server, sock in zip(self._servers, sockets)]
        for task in self._tasks:
            task.add_done_callback(self._ended)
        for _ in range(int(START_TIMEOUT / 0.05)):
            if all(server.started for server in self._servers):
                return
            if any(task.done() for task in self._tasks):
                break
            await asyncio.sleep(0.05)
        failure = next((t.exception() for t in self._tasks if t.done() and not t.cancelled() and t.exception()), None)
        raise HostStartError(f"サーバーを始められませんでした: {failure}" if failure else "サーバーを始められませんでした")

    def _ended(self, task: asyncio.Task) -> None:
        if self._stopping or not self.running:
            return
        self.running = False
        failure = None if task.cancelled() else task.exception()
        self.error = f"使う側が止まりました: {failure}" if failure else "使う側が止まりました"
        self.applog.add("use", "error", self.error)

    async def _teardown(self) -> None:
        self._stopping = True
        if self.host is not None:
            self.host.notifier.close()
        for server in self._servers:
            server.should_exit = True
        if self._tasks:
            _, pending = await asyncio.wait(self._tasks, timeout=STOP_TIMEOUT)
            for task in pending:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._servers, self._tasks = [], []
        self.host = None

    async def stop(self) -> None:
        was_running = self.running
        self.running = False
        await self._teardown()
        if was_running:
            self.applog.add("use", "info", "使う側を止めました")

    def counts(self) -> tuple[int, int, int]:
        """(lenders connected, lenders invited, approvals waiting) for the tray."""
        host = self.host
        if host is None or not self.running:
            return 0, 0, 0
        names = [i["name"] for i in host.tokens.list()]
        rows = {row["name"]: row for row in host.mgr.lenders(names)}
        return sum(bool(rows[n]["connected"]) for n in names if n in rows), len(names), len(host.approvals.pending())
