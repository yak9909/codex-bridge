"""Admin API (/admin/api/*) and the single-page UI (/admin/)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path, PureWindowsPath

import yaml
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ...common.invite import make_invite
from ..paths import ToolError, UNSET_SHARE_MSG
from ..policy import OUTPUTS_TOP, normalize_rel
from ..policy_form import _abs, form_from_raw, save_form


def _key(p) -> str:
    return os.path.normcase(os.path.abspath(str(p)))
from ..share import (ShareBusy, add_shared_folder, change_share_root, pick_folder_dialog,
                     remove_shared_folder, set_main_shared_folder)
from .. import share as share_ops

STATIC = Path(__file__).parent / "static"
COMMON = Path(__file__).parents[2] / "common" / "web"  # UI parts shared with the lender's page
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
        root = host.paths.root
        return {
            "share_root": str(root) if root else None, "share_root_message": None if root else UNSET_SHARE_MSG,
            "folders": host.paths.roots(), "public_bind": host.config.public_bind,
            "paused": p.paused, "exec_mode": p.exec_mode, "policy_error": host.policy.last_error,
            "lender_exec_modes": p.lender_exec_modes,
            "policy_problem": host.policy.problems(),
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

    @r.get("/api/policy-form")
    async def get_policy_form():
        return form_from_raw(host.policy.as_dict())

    @r.put("/api/policy-form")
    async def put_policy_form(body: dict):
        try:
            save_form(host.policy, body)
        except ValueError as e:
            raise HTTPException(400, str(e))
        host.events.notify()
        return {"ok": True}

    @r.get("/api/folders")
    async def folders(path: str = ""):
        """Without a path: the registered shared folders. With an absolute path: its sub-folders.

        This is the owner's view: hidden folders are listed too (so they can be changed)."""
        form = form_from_raw(host.policy.as_dict())
        rules = {_key(f["path"]): f["level"] for f in form["folders"]}
        main_key = _key(form["main"]) if form["main"] else None

        def has_children(p: str) -> bool:
            try:
                return any(c.is_dir(follow_symlinks=False) for c in os.scandir(p))
            except OSError:
                return False

        def row(p: Path, name: str) -> dict:
            return {"name": name, "path": p.as_posix(), "rule": rules.get(_key(p)),
                    "effective": host.paths.level_of(p), "has_children": has_children(str(p)),
                    "main": _key(p) == main_key, "exists": p.is_dir()}

        if not path:
            top = [f for f in form["folders"]
                   if not any(_key(f["path"]) != _key(o["path"]) and _key(f["path"]).startswith(_key(o["path"]) + os.sep)
                              for o in form["folders"])]
            return {"path": "", "folders": [row(Path(f["path"]), f["path"]) for f in top], "main": form["main"]}
        if not PureWindowsPath(path).drive or ".." in Path(path).parts:
            raise HTTPException(400, "フォルダはフルパスで指定してください")
        base = Path(os.path.abspath(path))
        if host.paths.level_of(base) is None or not base.is_dir():
            raise HTTPException(400, "共有フォルダの中ではありません")
        try:
            entries = sorted((e for e in os.scandir(base) if e.is_dir(follow_symlinks=False)),
                             key=lambda e: e.name.lower())[:500]
        except OSError as e:
            raise HTTPException(400, f"フォルダを読めません: {e}")
        out = [row(base / e.name, e.name) for e in entries
               if not (form["main"] and _key(base) == _key(form["main"]) and e.name.lower() == OUTPUTS_TOP)]
        return {"path": base.as_posix(), "folders": out, "main": form["main"]}

    @r.post("/api/folders/add")
    async def folders_add(body: dict):
        level = body.get("level", "read")
        result = _folder_op(lambda: add_shared_folder(host, body.get("path"), level))
        if level == "write" and not await asyncio.to_thread(share_ops.folder_is_writable, Path(_abs(body["path"]))):
            result["warning"] = share_ops.UNWRITABLE_WARNING
        return result

    @r.post("/api/folders/remove")
    async def folders_remove(body: dict):
        return _folder_op(lambda: remove_shared_folder(host, body.get("path")))

    @r.post("/api/folders/main")
    async def folders_main(body: dict):
        return _folder_op(lambda: set_main_shared_folder(host, body.get("path")))

    def _folder_op(fn):
        try:
            fn()
        except ShareBusy as e:
            raise HTTPException(409, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
        root = host.paths.root
        result = {"ok": True, "folders": host.paths.roots(), "main": root.as_posix() if root else None}
        if host.share_warning:
            result["warning"] = host.share_warning
        return result

    @r.put("/api/share-root")
    async def put_share_root(body: dict):
        try:
            p = change_share_root(host, body.get("path"))
        except ShareBusy as e:
            raise HTTPException(409, str(e))
        except (ValueError, OSError) as e:
            raise HTTPException(400, str(e))
        result = {"share_root": str(p)}
        warnings = [host.share_warning] if host.share_warning else []
        if not await asyncio.to_thread(share_ops.folder_is_writable, p):
            warnings.append(share_ops.UNWRITABLE_WARNING)
        if warnings:
            result["warning"] = " ".join(warnings)
        return result

    @r.post("/api/pick-folder")
    async def pick_folder():
        """Open the folder picker on this PC and return the chosen path (null if cancelled)."""
        chosen = await asyncio.to_thread(pick_folder_dialog, host.paths.root)
        return {"path": chosen}

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

    @r.get("/api/lenders")
    async def lenders():
        """One row per lender: connection, versions, usage, own limits, capabilities (disposable sessions,
        pre-approval) and the task in progress. Invited names that have not connected are listed too."""
        modes = host.policy.current().lender_exec_modes
        return [{**row, "exec_mode": modes.get(row["name"])}
                for row in host.mgr.lenders(i["name"] for i in host.tokens.list())]

    @r.put("/api/lenders/{name}/exec-mode")
    async def lender_exec_mode(name: str, body: dict):
        if name not in {i["name"] for i in host.tokens.list()}:
            raise HTTPException(404, f"その名前の招待コードがありません: {name}")
        try:
            host.policy.set_lender_exec_mode(name, body.get("mode"))
        except ValueError as e:
            raise HTTPException(400, str(e))
        host.events.notify()
        return {"ok": True}

    @r.get("/api/tasks")
    async def tasks():
        return host.mgr.list_tasks()

    @r.get("/api/tasks/{task_id}")
    async def task(task_id: str):
        try:
            return {**host.mgr.result(task_id), "events": host.mgr.events(task_id)[-200:],
                    "progress": host.mgr.progress(task_id)}
        except ToolError as e:
            raise HTTPException(404, str(e))

    @r.post("/api/tasks")
    async def send_task(body: dict):
        """lender: the invite name of who gets the task; left out (or empty) the host picks by headroom."""
        if any(k in body and not isinstance(body[k], str) for k in ("model", "effort")):
            raise HTTPException(400, "モデルと推論の強さは名前で指定してください")
        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            raise HTTPException(400, "依頼の内容を書いてください")
        lender = body.get("lender")
        if lender is not None and not isinstance(lender, str):  # 0, false, [] ... are not "automatic"
            raise HTTPException(400, "送り先は名前で指定してください")
        try:
            task_id = await host.mgr.start(prompt, body.get("continue_task_id") or None, lender or None,
                                           model=body.get("model") or None, effort=body.get("effort") or None,
                                           origin="admin")
        except ToolError as e:
            raise HTTPException(409, str(e))
        rec = host.mgr.tasks[task_id]
        return {"task_id": task_id, "lender": rec.lender, "model": rec.model, "effort": rec.effort}

    @r.post("/api/tasks/{task_id}/followup")
    async def followup(task_id: str, body: dict):
        text = str(body.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "追加の指示を書いてください")
        try:
            await host.mgr.followup(task_id, text)
        except ToolError as e:
            raise HTTPException(409, str(e))
        return {"ok": True}

    @r.post("/api/tasks/{task_id}/cancel")
    async def cancel(task_id: str):
        try:
            await host.mgr.cancel(task_id)
        except ToolError as e:
            raise HTTPException(409, str(e))
        return {"ok": True}

    @r.post("/api/tasks/{task_id}/undo")
    async def undo(task_id: str):
        if task_id not in host.mgr.tasks:
            raise HTTPException(404, "タスクが見つかりません")
        if host.mgr.is_active(task_id):
            raise HTTPException(409, "作業中のタスクは取り消せません")
        try:
            return {"restored": host.snapshots.undo(task_id)}
        except ValueError as e:
            raise HTTPException(400, str(e))

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

    @r.post("/api/shutdown")
    async def shutdown():
        host.shutdown.set()
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
    app.mount("/admin/common", StaticFiles(directory=COMMON), name="common")
