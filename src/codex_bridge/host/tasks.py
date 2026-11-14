"""Task and session bookkeeping on the host; talks to the agent through an attached link."""

from __future__ import annotations

import asyncio
import itertools
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from ..common import protocol
from ..common.protocol import FINAL_STATES, LOG_VISIBILITY
from .paths import ToolError

SendFn = Callable[[dict], Awaitable[None]]
ACTIVE_STATES = ("queued", "running", "agent_offline")
DEFAULT_THRESHOLDS = {"five_hour": 90, "weekly": 95}
REQUEST_TIMEOUT = 30.0


def iso_local(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).astimezone().isoformat(timespec="minutes")


@dataclass
class TaskRecord:
    task_id: str
    prompt: str
    created: float
    state: str = "queued"
    thread_id: str | None = None
    report: str | None = None
    resume_at: float | None = None
    continue_task_id: str | None = None
    state_before_offline: str | None = None


class TaskManager:
    def __init__(self, data_dir: Path, touched_fn: Callable[[str | None], list[str]]) -> None:
        self.dir = Path(data_dir) / "tasks"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.settings_path = Path(data_dir) / "settings.json"
        self.touched_fn = touched_fn
        self.tasks: dict[str, TaskRecord] = {}
        self._events: dict[str, list[dict]] = {}
        self._seen_seq: set[int] = set()
        self._link_ids = itertools.count(1)
        self._link: tuple[int, SendFn] | None = None
        self._agent: dict = {"connected": False}
        self._usage: dict = {"five_hour": None, "weekly": None, "ts": None}
        self._pending: dict[str, asyncio.Future] = {}
        self._req_ids = itertools.count(1)
        self._task_ids = itertools.count(1)
        self.on_change: list[Callable[[], None]] = []
        self.settings = {"thresholds": dict(DEFAULT_THRESHOLDS), "log_visibility": "summary"}
        if self.settings_path.exists():
            self.settings.update(json.loads(self.settings_path.read_text(encoding="utf-8")))
        self._load()

    # ---- persistence ---------------------------------------------------
    def _load(self) -> None:
        for f in sorted(self.dir.glob("*.json")):
            rec = TaskRecord(**json.loads(f.read_text(encoding="utf-8")))
            if rec.state in ACTIVE_STATES:
                rec.state = "agent_offline"
            self.tasks[rec.task_id] = rec
            ev = f.with_suffix(".jsonl")
            self._events[rec.task_id] = [json.loads(l) for l in ev.read_text(encoding="utf-8").splitlines()] \
                if ev.exists() else []

    def _save(self, rec: TaskRecord) -> None:
        (self.dir / f"{rec.task_id}.json").write_text(json.dumps(asdict(rec), ensure_ascii=False), encoding="utf-8")
        self._changed()

    def _append_event(self, task_id: str, ev: dict) -> None:
        self._events.setdefault(task_id, []).append(ev)
        with open(self.dir / f"{task_id}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        self._changed()

    def _save_settings(self) -> None:
        self.settings_path.write_text(json.dumps(self.settings, ensure_ascii=False), encoding="utf-8")

    def _changed(self) -> None:
        for cb in list(self.on_change):
            try:
                cb()
            except Exception:
                pass

    # ---- agent link ----------------------------------------------------
    def attach(self, send: SendFn) -> int:
        link_id = next(self._link_ids)
        self._link = (link_id, send)
        self._agent = {"connected": True, "since": time.time()}
        self._changed()
        return link_id

    def detach(self, link_id: int) -> None:
        if self._link is None or self._link[0] != link_id:
            return
        self._link = None
        self._agent = {**self._agent, "connected": False}
        for rec in self.tasks.values():
            if rec.state in ("queued", "running"):
                rec.state_before_offline = rec.state
                rec.state = "agent_offline"
                self._save(rec)
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ToolError("知人側の agent との接続が切れました"))
        self._changed()

    async def _send(self, m: dict) -> None:
        if self._link is None:
            raise ToolError("知人側の agent が接続されていません。知人の PC で codex-bridge-agent が動いているか確かめてください。")
        await self._link[1](m)

    def agent_info(self) -> dict:
        return dict(self._agent)

    async def on_agent_message(self, link_id: int, m: dict) -> None:
        if self._link is None or self._link[0] != link_id:
            return
        t = m["type"]
        if "seq" in m:
            await self._link[1](protocol.msg("ack", seq=m["seq"]))
            if m["seq"] in self._seen_seq:
                return
            self._seen_seq.add(m["seq"])
        if t == "hello":
            self._agent.update({k: m.get(k) for k in ("agent_version", "codex_version", "auth_mode")})
            await self._send_settings()
            self._changed()
        elif t == "usage":
            self._usage = {"five_hour": m.get("five_hour"), "weekly": m.get("weekly"), "ts": m.get("ts")}
            self._changed()
        elif t == "event":
            if m["task_id"] in self.tasks:
                self._append_event(m["task_id"], {"ts": time.time(), "kind": m["kind"], "data": m.get("data")})
        elif t == "task_state":
            rec = self.tasks.get(m["task_id"])
            if rec is None:
                return
            rec.state = m["state"]
            rec.state_before_offline = None
            rec.thread_id = m.get("thread_id") or rec.thread_id
            if m.get("report") is not None:
                rec.report = m["report"]
            if m.get("resume_at") is not None:
                rec.resume_at = m["resume_at"]
            self._save(rec)
        elif t == "reply":
            fut = self._pending.pop(m.get("req_id"), None)
            if fut and not fut.done():
                if m.get("ok"):
                    fut.set_result(m.get("data"))
                else:
                    fut.set_exception(ToolError(m.get("error") or "agent でエラーが起きました"))

    async def _send_settings(self) -> None:
        if self._link is not None:
            await self._link[1](protocol.msg("settings", thresholds=dict(self.settings["thresholds"]),
                                             log_visibility=self.settings["log_visibility"]))

    # ---- usage ---------------------------------------------------------
    def usage(self) -> dict:
        out = dict(self._usage)
        out["thresholds"] = dict(self.settings["thresholds"])
        for k in ("five_hour", "weekly"):
            if out.get(k):
                out[k] = {**out[k], "resets_at_local": iso_local(out[k].get("resets_at"))}
        blocked = self._blocked_until()
        out["blocked_until"] = iso_local(blocked)
        return out

    def _blocked_until(self) -> float | None:
        until = None
        now = time.time()
        for k in ("five_hour", "weekly"):
            w = self._usage.get(k)
            limit = self.settings["thresholds"].get(k)
            if w and limit is not None and w.get("used_percent", 0) >= limit and (w.get("resets_at") or 0) > now:
                until = max(until or 0, w["resets_at"])
        return until

    async def set_thresholds(self, five_hour: float | None = None, weekly: float | None = None) -> dict:
        for k, v in (("five_hour", five_hour), ("weekly", weekly)):
            if v is not None:
                if not 0 < float(v) <= 100:
                    raise ToolError("しきい値は 0 より大きく 100 以下の数で指定してください")
                self.settings["thresholds"][k] = v
        self._save_settings()
        await self._send_settings()
        self._changed()
        return dict(self.settings["thresholds"])

    async def set_log_visibility(self, value: str) -> None:
        if value not in LOG_VISIBILITY:
            raise ToolError(f"log_visibility は {LOG_VISIBILITY} のどれかです")
        self.settings["log_visibility"] = value
        self._save_settings()
        await self._send_settings()

    # ---- tasks ---------------------------------------------------------
    def current_task_id(self) -> str | None:
        for rec in self.tasks.values():
            if rec.state in ACTIVE_STATES:
                return rec.task_id
        return None

    def _get(self, task_id: str) -> TaskRecord:
        rec = self.tasks.get(task_id)
        if rec is None:
            raise ToolError(f"タスクが見つかりません: {task_id}")
        return rec

    async def start(self, prompt: str, continue_task_id: str | None = None) -> str:
        if self._link is None:
            await self._send({})  # raises the "not connected" ToolError
        cur = self.current_task_id()
        if cur:
            raise ToolError(f"作業中のタスクがあります: {cur}（終わるのを待つか cancel_task で止めてください）")
        blocked = self._blocked_until()
        if blocked:
            raise ToolError(f"使用量の上限のため受け付けません。再開可能: {iso_local(blocked)}")
        thread_id = None
        if continue_task_id:
            prev = self._get(continue_task_id)
            if not prev.thread_id:
                raise ToolError(f"このタスクには続けられるセッションがありません: {continue_task_id}")
            thread_id = prev.thread_id
        task_id = time.strftime("t%Y%m%d-%H%M%S") + f"-{next(self._task_ids)}"
        rec = TaskRecord(task_id=task_id, prompt=prompt, created=time.time(), continue_task_id=continue_task_id)
        self.tasks[task_id] = rec
        self._events[task_id] = []
        self._save(rec)
        await self._send(protocol.msg("task_start", task_id=task_id, prompt=prompt, continue_thread_id=thread_id))
        return task_id

    async def followup(self, task_id: str, text: str) -> None:
        rec = self._get(task_id)
        if rec.state not in ACTIVE_STATES:
            raise ToolError(f"このタスクは作業中ではありません（{rec.state}）。続けるなら send_task の continue_task_id を使ってください")
        await self._send(protocol.msg("task_followup", task_id=task_id, text=text))

    async def cancel(self, task_id: str) -> None:
        rec = self._get(task_id)
        if rec.state not in ACTIVE_STATES:
            raise ToolError(f"このタスクは作業中ではありません（{rec.state}）")
        await self._send(protocol.msg("task_cancel", task_id=task_id))

    def events(self, task_id: str) -> list[dict]:
        return list(self._events.get(task_id, []))

    def status(self, task_id: str | None = None) -> dict:
        if task_id is None:
            task_id = self.current_task_id() or (max(self.tasks, key=lambda k: self.tasks[k].created) if self.tasks else None)
            if task_id is None:
                return {"state": "idle", "agent": self.agent_info()}
        rec = self._get(task_id)
        return {
            "task_id": rec.task_id, "state": rec.state, "thread_id": rec.thread_id,
            "resume_at": iso_local(rec.resume_at), "recent_events": self.events(task_id)[-15:],
            "agent": self.agent_info(),
        }

    def result(self, task_id: str) -> dict:
        rec = self._get(task_id)
        return {
            "task_id": rec.task_id, "state": rec.state, "prompt": rec.prompt, "report": rec.report,
            "thread_id": rec.thread_id, "resume_at": iso_local(rec.resume_at),
            "changed_files": self.touched_fn(task_id), "event_count": len(self._events.get(task_id, [])),
        }

    def list_tasks(self) -> list[dict]:
        return [{"task_id": r.task_id, "state": r.state, "prompt": r.prompt[:200], "created": r.created}
                for r in sorted(self.tasks.values(), key=lambda r: r.created, reverse=True)]

    # ---- sessions (round trips to the agent) ---------------------------
    async def _request(self, type_: str, **fields):
        req_id = f"r{next(self._req_ids)}"
        fut = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        try:
            await self._send(protocol.msg(type_, req_id=req_id, **fields))
            return await asyncio.wait_for(fut, REQUEST_TIMEOUT)
        except asyncio.TimeoutError:
            raise ToolError("agent から返事がありませんでした") from None
        finally:
            self._pending.pop(req_id, None)

    async def sessions_list(self):
        return await self._request("session_list")

    async def session_read(self, thread_id: str):
        return await self._request("session_read", thread_id=thread_id)

    async def session_delete(self, thread_id: str):
        return await self._request("session_delete", thread_id=thread_id)
