"""The lender's local screen. Only the loopback listener in main.py serves this app."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, StrictBool, StrictFloat, StrictStr

from .appserver import AppServerError
from .settings import FEATURE_INFO, SettingsStore
from .ui_tasks import task_view

STATIC = Path(__file__).parent / "static"
COMMON = Path(__file__).parents[1] / "common" / "web"


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: StrictStr
    approve: StrictBool
    reason: StrictStr = ""
    created: StrictFloat | None = None


class Pause(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paused: StrictBool


class DataRoot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: StrictStr


def create_ui_app(runner, settings: SettingsStore, client, *, versions: dict, data_root: Path,
                  on_settings=None, on_move=None, on_pause=None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    mutations = asyncio.Lock()  # do not overlap a settings save and a data move
    moving = False

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        hosts = request.headers.getlist("host")
        host = hosts[0].lower() if len(hosts) == 1 else ""
        match = re.fullmatch(r"(?:127\.0\.0\.1|localhost)(?::([0-9]{1,5}))?", host)
        if (request.client is None or request.client.host != "127.0.0.1" or not match
                or (match.group(1) is not None and not 1 <= int(match.group(1)) <= 65535)):
            return JSONResponse({"detail": "forbidden host"}, status_code=403)
        if request.method not in {"GET", "HEAD", "OPTIONS"} and request.headers.get("X-Codex-Bridge") != "1":
            return JSONResponse({"detail": "X-Codex-Bridge: 1 is required"}, status_code=403)
        if moving and request.method not in {"GET", "HEAD", "OPTIONS"}:
            return JSONResponse({"detail": "保存場所を移動中です。起動し直すのを待ってください"}, status_code=409)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = ("default-src 'self'; script-src 'self'; "
                                                       "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
        return response

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/overview")
    async def overview():
        usage = {"five_hour": None, "weekly": None}
        if runner.last_usage is not None:
            for key, window in (("five_hour", runner.last_usage.five_hour), ("weekly", runner.last_usage.weekly)):
                if window is not None:
                    usage[key] = {"used_percent": window.used_percent, "resets_at": window.resets_at,
                                  "resets_at_local": datetime.fromtimestamp(window.resets_at).astimezone().isoformat()}
        state = ("paused" if client.paused else "awaiting_lender" if runner.pending_approval else
                 "running" if runner.cur is not None else "idle")
        # The invite's name identifies the lender. The protocol has no requester display name yet.
        return {"host_name": urlsplit(client.invite.url).netloc, "connected": client.ws is not None, "paused": client.paused,
                "state": state, **versions, "usage": usage, "limits": dict(runner.friend_limits),
                "thresholds": dict(runner.guard.thresholds), "effective_limits": runner.guard.effective_limits(),
                "blocked_until": runner.guard.blocked_until, "log_visibility": runner.log_visibility}

    @app.get("/api/tasks")
    async def tasks(thread_id: str | None = None):
        ids = None
        if thread_id is not None:
            session = next((s for s in runner.sessions.list() if s["thread_id"] == thread_id), None)
            if session is None:
                raise HTTPException(404, "このツールで作ったセッションではありません")
            ids = session["task_ids"]
        return [view for row in (runner.worklog.tasks() if runner.worklog else [])
                if (ids is None or row["task_id"] in ids)
                and (view := task_view(runner, row["task_id"], detail=False)) is not None]

    @app.get("/api/tasks/{task_id}")
    async def task(task_id: str):
        view = task_view(runner, task_id)
        if view is None:
            raise HTTPException(404, "タスクの記録がありません")
        return view

    @app.get("/api/pending")
    async def pending():
        return runner.pending_approval

    @app.post("/api/pending")
    async def decide(body: Decision):
        current = runner.pending_approval
        if body.created is not None and (current is None or current["created"] != body.created):
            raise HTTPException(409, "承認待ちが変わりました。画面を更新してください")
        if not await runner.decide_pending(body.approve, body.reason, task_id=body.task_id):
            raise HTTPException(409, "承認待ちが変わりました。画面を更新してください")
        return {"decided": True}

    @app.get("/api/sessions")
    async def sessions():
        return [{"thread_id": s["thread_id"], "created": s["created"], "task_ids": s["task_ids"],
                 "active": runner.cur is not None and runner.cur.thread_id == s["thread_id"],
                 "disposable": bool(s.get("ephemeral"))} for s in runner.sessions.list()]

    @app.delete("/api/sessions/{thread_id}")
    async def delete_session(thread_id: str):
        async with runner.exclusive():  # a resumed task cannot start while deletion is in flight
            if not runner.sessions.owns(thread_id):
                raise HTTPException(404, "このツールで作ったセッションではありません")
            if runner.cur is not None and runner.cur.thread_id == thread_id:
                raise HTTPException(409, "作業中のセッションは削除できません")
            try:
                await runner.app.thread_delete(thread_id)
            except (AppServerError, OSError, TimeoutError) as e:
                raise HTTPException(502, f"削除できませんでした: {e}") from e
            runner.sessions.remove(thread_id)
        return {"deleted": thread_id}

    @app.get("/api/settings")
    async def get_settings():
        return {"settings": settings.get(), "feature_info": FEATURE_INFO, "data_root": str(data_root),
                "workdir": str(runner.app.workdir)}

    @app.put("/api/settings")
    async def put_settings(body: dict):
        async with mutations:
            if moving:
                raise HTTPException(409, "保存場所を移しました。起動し直すのを待ってください")
            try:
                saved = settings.update(body)
            except (ValueError, TypeError) as e:
                raise HTTPException(400, str(e)) from e
            if on_settings:
                await on_settings(saved)
            else:
                runner.apply_settings(saved)
            return saved

    @app.post("/api/settings/data-root")
    async def move(body: DataRoot):
        nonlocal moving
        path = Path(body.path.strip())
        if not body.path.strip() or not path.is_absolute():
            raise HTTPException(400, "保存場所は、ドライブ名から始まるフォルダのパスで指定してください")
        async with mutations:
            if moving or runner.active_task_id() is not None:
                raise HTTPException(409, "作業中・承認待ち・移動中は保存場所を変えられません")
            if on_move is None:
                raise HTTPException(503, "保存場所の変更は利用できません")
            moving = True
            try:
                left = await on_move(path)
            except (ValueError, OSError) as e:
                moving = False
                raise HTTPException(400, f"移せませんでした（元の場所のまま使います）: {e}") from e
            except BaseException:
                moving = False
                raise
        return {"moved": True, "left": [str(p) for p in left or []]}

    @app.post("/api/logs/clear")
    async def clear():
        async with mutations:
            if moving:
                raise HTTPException(409, "起動し直すのを待ってください")
            return {"cleared": runner.worklog.clear() if runner.worklog else 0}

    @app.post("/api/pause")
    async def pause(body: Pause):
        if on_pause:
            await on_pause(body.paused)
        else:
            client.paused = body.paused
            if body.paused:
                await client.drop()
        return {"paused": client.paused}

    app.mount("/admin/common", StaticFiles(directory=COMMON), name="common")
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
