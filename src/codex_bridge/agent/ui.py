"""The lender's local API, mounted at /lend by the resident app."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictBool, StrictFloat, StrictInt, StrictStr

from ..common.protocol import EFFORT_ORDER
from .appserver import AppServerError
from .settings import FEATURE_INFO, SettingsStore
from .ui_tasks import task_view


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: StrictStr
    approve: StrictBool
    reason: StrictStr = ""
    created: StrictInt | StrictFloat
    connection_id: StrictStr | None = None


class Pause(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paused: StrictBool


class DataRoot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: StrictStr


class InviteCode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: StrictStr


def create_ui_app(runner, settings: SettingsStore, client, *, versions: dict, data_root: Path,
                  on_settings=None, on_move=None, on_pause=None, connections=None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    mutations = asyncio.Lock()  # do not overlap a settings save and a data move
    moving = False
    legacy_runner, legacy_client = runner, client

    @app.exception_handler(RequestValidationError)
    async def invalid_body(request, error):
        if "/api/connections" in request.url.path:  # also under a mount prefix (/lend/api/connections)
            return JSONResponse({"detail": "招待コードや設定の形式が正しくありません"}, status_code=400)
        return await request_validation_exception_handler(request, error)

    def runners(connection_id=None):
        if connections is None:
            return [runner]
        if connection_id is not None:
            if connection_id not in connections.runners:
                raise HTTPException(404, "接続先がありません")
            return [connections.runners[connection_id]]
        return list(connections.runners.values())

    def one(connection_id=None, *, task_id=None, thread_id=None):
        candidates = runners(connection_id)
        if connection_id is None and len(candidates) > 1:
            if task_id:
                try:
                    candidates = [r for r in candidates if r.worklog and r.worklog.task_events(task_id)]
                except ValueError as e:
                    raise HTTPException(404, "タスクの記録がありません") from e
            elif thread_id:
                candidates = [r for r in candidates if r.sessions.owns(thread_id)]
        if len(candidates) != 1:
            raise HTTPException(400, "connection_id に接続先を指定してください")
        return candidates[0]

    def require_connections():
        if connections is None:
            raise HTTPException(503, "接続先の変更は利用できません")

    async def connection_change(action):
        async with mutations:
            try:
                return await action()
            except KeyError as e:
                raise HTTPException(404, "接続先がありません") from e
            except RuntimeError as e:
                raise HTTPException(409, str(e)) from e
            except (ValueError, TypeError) as e:
                raise HTTPException(400, str(e)) from e

    @app.get("/api/connections")
    async def get_connections():
        return connections.rows() if connections else []

    @app.post("/api/connections", status_code=201)
    async def add_connection(body: InviteCode):
        require_connections()
        connection = await connection_change(lambda: connections.add(body.code))
        return next(row for row in connections.rows() if row["id"] == connection.id)

    @app.put("/api/connections/{id}/invite")
    async def replace_connection(id: str, body: InviteCode):
        require_connections()
        await connection_change(lambda: connections.replace_invite(id, body.code))
        return next(row for row in connections.rows() if row["id"] == id)

    @app.delete("/api/connections/{id}")
    async def remove_connection(id: str):
        require_connections()
        await connection_change(lambda: connections.remove(id))
        return {"removed": id}

    @app.get("/api/connections/{id}/settings")
    async def get_connection_settings(id: str):
        require_connections()
        one(id)
        return {"settings": connections.store._read(id).settings, "feature_info": FEATURE_INFO,
                "models": connections.catalog, "efforts": list(EFFORT_ORDER)}

    @app.put("/api/connections/{id}/settings")
    async def put_connection_settings(id: str, body: dict):
        require_connections()
        return await connection_change(lambda: connections.update_settings(id, body))

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

    @app.get("/api/overview")
    async def overview():
        current = next((r for r in runners() if r.active_task_ids()), next(iter(runners()), None))
        runner = current
        client = connections.clients.get(runner.connection_id) if connections and runner else None
        if connections is None:
            client = legacy_client
        if runner is None:
            return {"host_name": "", "connected": False, "paused": connections.paused,
                    "state": "paused" if connections.paused else "idle",
                    **versions, "usage": {"five_hour": None, "weekly": None}, "limits": settings.get()["limits"],
                    "thresholds": {}, "effective_limits": {}, "blocked_until": None, "log_visibility": "summary"}
        usage = {"five_hour": None, "weekly": None}
        if runner.last_usage is not None:
            for key, window in (("five_hour", runner.last_usage.five_hour), ("weekly", runner.last_usage.weekly)):
                if window is not None:
                    usage[key] = {"used_percent": window.used_percent, "resets_at": window.resets_at,
                                  "resets_at_local": datetime.fromtimestamp(window.resets_at).astimezone().isoformat()}
        state = ("paused" if client.paused else "awaiting_lender" if any(r.pending_approvals for r in runners()) else
                 "running" if any(r.currents for r in runners()) else "idle")
        # The invite's name identifies the lender. The protocol has no requester display name yet.
        connected = any(row["connected"] for row in connections.rows()) if connections else client.ws is not None
        return {"host_name": urlsplit(client.invite.url).netloc, "connected": connected, "paused": client.paused,
                "state": state, "active_tasks": sum(len(r.active_task_ids()) for r in runners()),
                "pending_approvals": sum(len(r.pending_approvals) for r in runners()), **versions, "usage": usage, "limits": dict(runner.friend_limits),
                "thresholds": dict(runner.guard.thresholds), "effective_limits": runner.guard.effective_limits(),
                "blocked_until": runner.guard.blocked_until, "log_visibility": runner.log_visibility}

    @app.get("/api/tasks")
    async def tasks(thread_id: str | None = None, connection_id: str | None = None):
        result = []
        for current in runners(connection_id):
            ids = None
            if thread_id is not None:
                session = next((s for s in current.sessions.list() if s["thread_id"] == thread_id), None)
                if session is None:
                    continue
                ids = session["task_ids"]
            result.extend(view for row in (current.worklog.tasks() if current.worklog else [])
                          if (ids is None or row["task_id"] in ids)
                          and (view := task_view(current, row["task_id"], detail=False)) is not None)
        if thread_id and not any(r.sessions.owns(thread_id) for r in runners(connection_id)):
            raise HTTPException(404, "このツールで作ったセッションではありません")
        return sorted(result, key=lambda row: row["created"], reverse=True)

    @app.get("/api/tasks/{task_id}")
    async def task(task_id: str, connection_id: str | None = None):
        view = task_view(one(connection_id, task_id=task_id), task_id)
        if view is None:
            raise HTTPException(404, "タスクの記録がありません")
        return view

    @app.get("/api/pending")
    async def pending(connection_id: str | None = None):
        return [p for r in runners(connection_id) for p in r.pending_approvals]

    @app.post("/api/pending")
    async def decide(body: Decision):
        runner = one(body.connection_id, task_id=body.task_id)
        if not await runner.decide_pending(body.approve, body.reason, task_id=body.task_id, created=body.created):
            raise HTTPException(409, "承認待ちが変わりました。画面を更新してください")
        return {"decided": True}

    @app.get("/api/sessions")
    async def sessions(connection_id: str | None = None):
        return [{"thread_id": s["thread_id"], "created": s["created"], "task_ids": s["task_ids"],
                 "active": r.thread_active(s["thread_id"]),
                 "disposable": bool(s.get("ephemeral")), **r.connection_fields()}
                for r in runners(connection_id) for s in r.sessions.list()]

    @app.delete("/api/sessions/{thread_id}")
    async def delete_session(thread_id: str, connection_id: str | None = None):
        runner = one(connection_id, thread_id=thread_id)
        async with runner.exclusive():  # a resumed task cannot start while deletion is in flight
            if not runner.sessions.owns(thread_id):
                raise HTTPException(404, "このツールで作ったセッションではありません")
            if runner.thread_active(thread_id):
                raise HTTPException(409, "作業中のセッションは削除できません")
            try:
                if runner.prepare:
                    await runner.prepare()
                await runner.app.thread_delete(thread_id)
            except (AppServerError, OSError, TimeoutError) as e:
                raise HTTPException(502, f"削除できませんでした: {e}") from e
            runner.sessions.remove(thread_id)
        return {"deleted": thread_id}

    @app.get("/api/settings")
    async def get_settings():
        runner = next(iter(runners()), None)
        return {"settings": settings.get(), "feature_info": FEATURE_INFO, "data_root": str(data_root),
                "workdir": str(runner.app.workdir) if runner else settings.get().get("workdir") or "既定"}

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
            if moving or any(r.active_task_id() is not None for r in runners()):
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
            return {"cleared": sum(r.worklog.clear() for r in runners() if r.worklog)}

    @app.post("/api/connections/{id}/logs/clear")
    async def clear_connection(id: str):
        require_connections()
        async with mutations:
            if moving:
                raise HTTPException(409, "起動し直すのを待ってください")
            runner, = runners(id)  # 404 for an unknown connection
            if getattr(connections, "moving", False):
                raise HTTPException(409, "保存場所を移動中です")
            return {"cleared": runner.worklog.clear() if runner.worklog else 0}

    @app.post("/api/pause")
    async def pause(body: Pause):
        if on_pause:
            await on_pause(body.paused)
        else:
            client.paused = body.paused
            if body.paused:
                await client.drop()
        return {"paused": connections.paused if connections else client.paused}

    return app
