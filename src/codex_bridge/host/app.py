"""Wire all host services together and build the two ASGI apps.

public app (Tailscale address): /codex/mcp (Codex tools), /agent/ws (agent control channel)
local app  (127.0.0.1 only):    /claude/mcp (Claude Code), /admin/ (web UI + API)
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from fastapi import FastAPI

from ..common.audit import AuditLog
from .approvals import Approvals, make_approve_fn
from .claude_mcp import build_claude_mcp
from .codex_mcp import HostServices, build_codex_mcp
from .config import HostConfig
from .control import add_control_route
from .fs_read import ReadTools
from .fs_write import WriteTools
from .mcp_util import LoopbackOnly, McpEndpoint
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
    tokens: TokenStore
    services: HostServices
    codex_ep: McpEndpoint
    claude_ep: McpEndpoint
    events: Broadcaster
    shutdown: asyncio.Event = field(default_factory=asyncio.Event)  # set by stop.bat via /admin/api/shutdown


def build_host(config: HostConfig) -> Host:
    data = config.data_dir
    data.mkdir(parents=True, exist_ok=True)
    policy = PolicyStore(data / "policy.yaml")
    audit = AuditLog(data / "audit.jsonl")
    paths = SafePaths(config.share_root, policy)
    snapshots = Snapshots(data)
    approvals = Approvals(policy)
    mgr = TaskManager(data, touched_fn=snapshots.touched)
    tokens = TokenStore(data)
    task_id_fn = mgr.current_task_id
    write = WriteTools(paths, policy, snapshots, audit, task_id_fn,
                       make_approve_fn(approvals, task_id_fn, wait_sec=30))
    services = HostServices(
        paths=paths, policy=policy, read=ReadTools(paths, policy), search=SearchTools(paths, policy, config.rg_path),
        write=write, runner=CommandRunner(paths, policy, approvals, snapshots, audit, task_id_fn),
        approvals=approvals, snapshots=snapshots, audit=audit, task_id_fn=task_id_fn)
    events = Broadcaster()
    approvals.on_change.append(events.notify)
    mgr.on_change.append(events.notify)
    return Host(config=config, policy=policy, audit=audit, paths=paths, snapshots=snapshots, approvals=approvals,
                mgr=mgr, tokens=tokens, services=services, codex_ep=build_codex_mcp(services, tokens.hashes),
                claude_ep=build_claude_mcp(mgr, approvals, snapshots), events=events)


def create_public_app(host: Host) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        async with host.codex_ep.run():
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    add_control_route(app, host.mgr, host.tokens)
    app.mount("/codex", host.codex_ep.asgi)
    return app


def create_local_app(host: Host) -> FastAPI:
    from .web.app import admin_router, mount_static

    @asynccontextmanager
    async def lifespan(app):
        async with host.claude_ep.run():
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(admin_router(host))
    mount_static(app)
    app.mount("/claude", host.claude_ep.asgi)
    return app
