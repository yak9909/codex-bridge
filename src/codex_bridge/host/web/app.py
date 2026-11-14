"""Admin API (/admin/api/*) and the single-page UI (/admin/)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import yaml
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ...common.invite import make_invite
from ..paths import ToolError

STATIC = Path(__file__).parent / "static"
LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def _guard(request: Request) -> None:
    """Block DNS rebinding (foreign Host) and cross-site writes (custom header forces a CORS preflight)."""
    host = (request.headers.get("host") or "").rsplit(":", 1)[0]
    if host not in LOCAL_HOSTS:
        raise HTTPException(403, "forbidden host")
    if request.method not in SAFE_METHODS and request.headers.get("x-codex-bridge") != "1":
        raise HTTPException(403, "missing X-Codex-Bridge header")


def admin_router(host) -> APIRouter:
    from fastapi import Depends
    r = APIRouter(prefix="/admin", dependencies=[Depends(_guard)])

    @r.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @r.get("/api/overview")
    async def overview():
        p = host.policy.current()
        return {
            "share_root": str(host.config.share_root), "public_bind": host.config.public_bind,
            "paused": p.paused, "exec_mode": p.exec_mode, "policy_error": host.policy.last_error,
            "agent": host.mgr.agent_info(), "current_task": host.mgr.current_task_id(),
            "pending_approvals": len(host.approvals.pending()), "usage": host.mgr.usage(),
            "log_visibility": host.mgr.settings["log_visibility"],
        }

    @r.get("/api/policy")
    async def get_policy():
        path = host.policy.path
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        return {"text": text, "error": host.policy.last_error}

    @r.put("/api/policy")
    async def put_policy(request: Request):
        text = (await request.body()).decode("utf-8")
        try:
            host.policy.save_text(text)
        except ValueError as e:
            raise HTTPException(400, str(e))
        host.events.notify()
        return {"ok": True}

    @r.post("/api/pause")
    async def pause(body: dict):
        host.policy.update(paused=bool(body.get("paused")))
        host.events.notify()
        return {"paused": host.policy.current().paused}

    @r.put("/api/exec-mode")
    async def exec_mode(body: dict):
        try:
            host.policy.update(exec_mode=body.get("mode"))
        except ValueError as e:
            raise HTTPException(400, str(e))
        host.events.notify()
        return {"ok": True}

    @r.get("/api/approvals")
    async def approvals():
        return [a.to_dict() for a in host.approvals.pending()]

    @r.post("/api/approvals/{approval_id}")
    async def decide(approval_id: str, body: dict):
        try:
            host.approvals.decide(approval_id, bool(body.get("approve")))
        except KeyError:
            raise HTTPException(409, "この承認待ちはもうありません")
        return {"ok": True}

    @r.get("/api/tasks")
    async def tasks():
        return host.mgr.list_tasks()

    @r.get("/api/tasks/{task_id}")
    async def task(task_id: str):
        try:
            return {**host.mgr.result(task_id), "events": host.mgr.events(task_id)[-200:]}
        except ToolError as e:
            raise HTTPException(404, str(e))

    @r.post("/api/tasks/{task_id}/undo")
    async def undo(task_id: str):
        if task_id == host.mgr.current_task_id():
            raise HTTPException(409, "作業中のタスクは取り消せません")
        return {"restored": host.snapshots.undo(task_id)}

    @r.get("/api/usage")
    async def usage():
        return host.mgr.usage()

    @r.put("/api/thresholds")
    async def thresholds(body: dict):
        try:
            return await host.mgr.set_thresholds(body.get("five_hour"), body.get("weekly"))
        except ToolError as e:
            raise HTTPException(400, str(e))

    @r.put("/api/log-visibility")
    async def log_visibility(body: dict):
        try:
            await host.mgr.set_log_visibility(body.get("value"))
        except ToolError as e:
            raise HTTPException(400, str(e))
        return {"ok": True}

    @r.get("/api/invites")
    async def invites():
        return host.tokens.list()

    @r.post("/api/invites")
    async def issue(body: dict):
        name = str(body.get("name") or "friend").strip()
        token = host.tokens.issue(name)
        return {"code": make_invite(f"http://{host.config.public_bind}", token, name)}

    @r.delete("/api/invites/{name}")
    async def revoke(name: str):
        host.tokens.revoke(name)
        return {"ok": True}

    @r.get("/api/events")
    async def events():
        q: asyncio.Queue = asyncio.Queue()
        host.events.queues.add(q)

        async def stream():
            try:
                yield "data: hello\n\n"
                while True:
                    try:
                        await asyncio.wait_for(q.get(), 15)
                        yield "data: change\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                host.events.queues.discard(q)

        return StreamingResponse(stream(), media_type="text/event-stream")

    return r


def mount_static(app: FastAPI) -> None:
    app.mount("/admin/static", StaticFiles(directory=STATIC), name="static")
