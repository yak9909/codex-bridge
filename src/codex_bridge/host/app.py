"""Wire all host services together and build the two ASGI apps.

public app (Tailscale address): /codex/mcp (Codex tools), /agent/ws (agent control channel)
local app  (127.0.0.1 only):    /claude/mcp (Claude Code), /admin/ (web UI + API)
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI

from ..common.audit import AuditLog
from .approvals import Approvals, make_approve_fn
from .claude_mcp import build_claude_mcp
from .codex_mcp import HostServices, build_codex_mcp, current_lender
from .config import HostConfig
from .control import add_control_route
from .share import (add_shared_folder, change_share_root, remove_shared_folder,
                    set_main_shared_folder)
from .fs_read import ReadTools
from .fs_write import WriteTools
from .mcp_util import LoopbackOnly, McpEndpoint
from .notify import Notifier
from .paths import SafePaths
from .policy import PolicyStore
from .runner import CommandRunner
from .search import SearchTools
from .snapshots import Snapshots
from .tasks import TaskManager
from .tokens import TokenStore


class Broadcaster:
    """Fan-out of 'something changed' pings to the admin page (Server-Sent Events)."""

    def __init__(self) -> None:
        self.queues: set[asyncio.Queue] = set()

    def notify(self) -> None:
        for q in list(self.queues):
            if q.qsize() < 10:
                q.put_nowait("change")


@dataclass
class Host:
    config: HostConfig
    policy: PolicyStore
    audit: AuditLog
    paths: SafePaths
    snapshots: Snapshots
    approvals: Approvals
    mgr: TaskManager
    notifier: Notifier
    tokens: TokenStore
    services: HostServices
    codex_ep: McpEndpoint
    claude_ep: McpEndpoint
    events: Broadcaster
    shutdown: asyncio.Event = field(default_factory=asyncio.Event)  # set by stop.bat via /admin/api/shutdown
    config_path: Path | None = None  # host.yaml, updated when settings change from the admin page
    share_warning: str | None = None  # set when the last folder change could not be copied to host.yaml


def build_host(config: HostConfig, config_path: Path | None = None) -> Host:
    data = config.data_dir
    data.mkdir(parents=True, exist_ok=True)
    policy = PolicyStore(data / "policy.yaml", share_root=config.share_root)  # converts a v0.1 file
    audit = AuditLog(data / "audit.jsonl")
    paths = SafePaths(config.share_root, policy,
                      install_dir=config.install_dir or (config_path.parent if config_path else None),
                      protected=(config.rg_path, *((config_path,) if config_path else ())))
    snapshots = Snapshots(data)
    approvals = Approvals(policy)
    mgr = TaskManager(data, touched_fn=snapshots.touched, check_start=paths.check_configured)
    notifier = Notifier(mgr, approvals)
    tokens = TokenStore(data)
    def invite_changed(name):
        if name not in {i["name"] for i in tokens.list()}:
            policy.drop_lender(name)
            mgr.drop_lender_settings(name)

    tokens.on_change.append(invite_changed)
    mgr.invited_names = lambda: {i["name"] for i in tokens.list()}

    def task_id_fn() -> str | None:
        """A Codex tool call belongs to the task of the lender whose token it came with, never to
        another's. Without a known lender it belongs to no task (not to "any active task")."""
        lender = current_lender.get()
        return mgr.current_task_id(lender) if lender is not None else None

    write = WriteTools(paths, policy, snapshots, audit, task_id_fn,
                       make_approve_fn(approvals, task_id_fn, wait_sec=30), lender_fn=current_lender.get)
    services = HostServices(
        paths=paths, policy=policy, read=ReadTools(paths, policy), search=SearchTools(paths, policy, config.rg_path),
        write=write, runner=CommandRunner(paths, policy, approvals, snapshots, audit, task_id_fn,
                                          lender_fn=current_lender.get),
        approvals=approvals, snapshots=snapshots, audit=audit, task_id_fn=task_id_fn)
    events = Broadcaster()
    approvals.on_change.append(events.notify)
    mgr.on_change.append(events.notify)
    ref: dict = {}  # the agents' tools need the finished Host (share folder can change at runtime)
    host = Host(config=config, policy=policy, audit=audit, paths=paths, snapshots=snapshots, approvals=approvals,
                mgr=mgr, notifier=notifier, tokens=tokens, services=services, codex_ep=build_codex_mcp(services, tokens.match),
                claude_ep=build_claude_mcp(mgr, approvals, snapshots, policy=policy, tokens=tokens,
                                           public_bind=config.public_bind,
                                           share_root=lambda: ref["host"].paths.root,
                                           folder_ops={
                                               "add": lambda p, lv: add_shared_folder(ref["host"], p, lv),
                                               "remove": lambda p: remove_shared_folder(ref["host"], p),
                                               "main": lambda p: set_main_shared_folder(ref["host"], p),
                                               "share_root": lambda p: change_share_root(ref["host"], p)},
                                           roots=lambda: ref["host"].paths.roots()),
                events=events, config_path=Path(config_path) if config_path else None)
    ref["host"] = host
    return host


def create_public_app(host: Host) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        async with host.codex_ep.run():
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    add_control_route(app, host.mgr, host.tokens)
    app.mount("/codex", host.codex_ep.asgi)
    return app


def create_local_app(host: Host, page_url: str | None = None) -> FastAPI:
    """page_url: the resident app's screen. Set, /admin/ redirects there; unset (console way), /admin/ serves
    the same page itself and its files are mounted at /app/static."""
    from .web.app import admin_router, mount_static

    @asynccontextmanager
    async def lifespan(app):
        async with host.claude_ep.run():
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(admin_router(host, page_url=page_url))
    mount_static(app, page=page_url is None)
    app.mount("/claude", host.claude_ep.asgi)
    return app
