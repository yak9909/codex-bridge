"""Runs host tasks on the local app-server and reports back to the host."""

from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from ..common import protocol
from .appserver import AppServer, AppServerError
from .sessions import SessionRegistry
from .usage import BlockNew, Interrupt, QuotaGuard, Steer, Suspend, Unblock

PREAMBLE = ("ファイルの一覧・検索・読み書き・コマンド実行は、MCP サーバー bridge の道具だけを使ってください。"
            "パスは共有フォルダからの相対パスです。作業フォルダ（cwd）は空で、使いません。")
NOT_OWNED = "このツールで作ったセッションではありません"
SendFn = Callable[[dict], Awaitable[None]]


@dataclass
class Current:
    task_id: str
    thread_id: str | None = None
    turn_id: str | None = None
    last_message: str | None = None
    cancelling: bool = False
    suspend_at: int | None = None
    followups: list[str] = field(default_factory=list)


def _summ(v, n=300):
    s = v if isinstance(v, str) else str(v)
    return s if len(s) <= n else s[:n] + "…"


class AgentRunner:
    def __init__(self, app: AppServer, guard: QuotaGuard, sessions: SessionRegistry, send: SendFn,
                 usage_interval: float = 60, tick_interval: float = 5,
                 on_log: Callable[[dict], None] | None = None) -> None:
        self.app = app
        self.guard = guard
        self.sessions = sessions
        self.send = send
        self.usage_interval = usage_interval
        self.tick_interval = tick_interval
        self.on_log = on_log or (lambda e: None)
        self.cur: Current | None = None
        self.log_visibility = "summary"
        self._seq = itertools.count(int(time.time() * 1000))
        self._unacked: dict[int, dict] = {}
        self._tasks: list[asyncio.Task] = []
        self._lock = asyncio.Lock()

    # ---- lifecycle -----------------------------------------------------
    async def start(self) -> None:
        self._tasks = [asyncio.create_task(self._pump()), asyncio.create_task(self._usage_loop()),
                       asyncio.create_task(self._tick_loop())]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # ---- sending -------------------------------------------------------
    async def _safe_send(self, m: dict) -> None:
        try:
            await self.send(m)
        except Exception:
            pass  # disconnected; seq'd messages are re-sent after reconnect

    async def _send_seq(self, type_: str, **fields) -> None:
        m = protocol.msg(type_, seq=next(self._seq), **fields)
        self._unacked[m["seq"]] = m
        self.on_log(m)
        await self._safe_send(m)

    async def resend_unacked(self) -> None:
        for seq in sorted(self._unacked):
            await self._safe_send(self._unacked[seq])

    async def _event(self, kind: str, **data) -> None:
        if self.cur:
            await self._send_seq("event", task_id=self.cur.task_id, kind=kind, data=data)

    async def _state(self, state: str, report: str | None = None, resume_at: int | None = None,
                     task_id: str | None = None, thread_id: str | None = None) -> None:
        cur = self.cur
        await self._send_seq("task_state", task_id=task_id or (cur.task_id if cur else None), state=state,
                             thread_id=thread_id or (cur.thread_id if cur else None), report=report,
                             resume_at=resume_at)

    async def _reply(self, req_id: str, ok: bool, data=None, error: str | None = None) -> None:
        await self._safe_send(protocol.msg("reply", req_id=req_id, ok=ok, data=data, error=error))

    # ---- host messages -------------------------------------------------
    async def handle(self, m: dict) -> None:
        t = m["type"]
        if t == "ack":
            self._unacked.pop(m["seq"], None)
        elif t == "settings":
            self.guard.set_thresholds(m.get("thresholds") or {})
            self.log_visibility = m.get("log_visibility", self.log_visibility)
            self.on_log({"type": "settings", "log_visibility": self.log_visibility})
        elif t == "task_start":
            await self._task_start(m)
        elif t == "task_followup":
            await self._followup(m)
        elif t == "task_cancel":
            await self._cancel(m)
        elif t == "session_list":
            await self._reply(m["req_id"], True, data=self.sessions.list())
        elif t == "session_read":
            await self._session_op(m, read=True)
        elif t == "session_delete":
            await self._session_op(m, read=False)

    async def _task_start(self, m: dict) -> None:
        async with self._lock:
            if self.cur is not None:
                await self._state("failed", report="別のタスクが作業中です", task_id=m["task_id"])
                return
            self.cur = Current(task_id=m["task_id"])
            self.on_log({"type": "task_prompt", "task_id": m["task_id"], "prompt": m["prompt"]})
            try:
                cont = m.get("continue_thread_id")
                if cont:
                    if not self.sessions.owns(cont):
                        raise AppServerError(NOT_OWNED)
                    self.cur.thread_id = await self.app.thread_resume(cont, PREAMBLE)
                else:
                    self.cur.thread_id = await self.app.thread_start(PREAMBLE)
                self.sessions.add(self.cur.thread_id, self.cur.task_id)
                await self._state("running")
                await self._event("started")
                self.cur.turn_id = await self.app.turn_start(self.cur.thread_id, m["prompt"])
            except (AppServerError, asyncio.TimeoutError) as e:
                await self._state("failed", report=f"開始できませんでした: {e}")
                self.cur = None

    async def _followup(self, m: dict) -> None:
        cur = self.cur
        if cur is None or cur.task_id != m["task_id"] or cur.thread_id is None:
            return
        self.on_log({"type": "task_prompt", "task_id": cur.task_id, "prompt": m["text"]})
        if cur.turn_id:
            try:
                await self.app.turn_steer(cur.thread_id, cur.turn_id, m["text"])
                return
            except AppServerError:
                pass
        cur.turn_id = await self.app.turn_start(cur.thread_id, m["text"])

    async def _cancel(self, m: dict) -> None:
        cur = self.cur
        if cur is None or cur.task_id != m["task_id"]:
            return
        cur.cancelling = True
        if cur.turn_id and cur.thread_id:
            try:
                await self.app.turn_interrupt(cur.thread_id, cur.turn_id)
                return
            except AppServerError:
                pass
        await self._state("cancelled", report=cur.last_message)
        self.cur = None

    async def _session_op(self, m: dict, read: bool) -> None:
        tid = m["thread_id"]
        if not self.sessions.owns(tid):
            await self._reply(m["req_id"], False, error=NOT_OWNED)
            return
        try:
            if read:
                await self._reply(m["req_id"], True, data=await self.app.thread_read(tid))
            else:
                if self.cur and self.cur.thread_id == tid:
                    raise AppServerError("作業中のセッションは削除できません")
                await self.app.thread_delete(tid)
                self.sessions.remove(tid)
                await self._reply(m["req_id"], True, data={"deleted": tid})
        except AppServerError as e:
            await self._reply(m["req_id"], False, error=str(e))

    # ---- app-server events --------------------------------------------
    async def _pump(self) -> None:
        while True:
            ev = await self.app.events.get()
            try:
                await self._on_event(ev)
            except Exception as e:  # never let one bad event stop the pump
                self.on_log({"type": "error", "text": f"{type(e).__name__}: {e}"})

    async def _on_event(self, ev: dict) -> None:
        method, p = ev.get("method"), ev.get("params") or {}
        cur = self.cur
        if method == "turn/started":
            if cur:
                cur.turn_id = p["turn"]["id"]
            await self._apply(self.guard.on_turn_started(p["turn"]["id"]))
        elif method == "item/completed":
            item = p.get("item") or {}
            if item.get("type") == "agentMessage" and cur:
                cur.last_message = item.get("text")
                await self._event("agent_message", text=item.get("text"))
            elif item.get("type") in ("mcpToolCall", "dynamicToolCall", "commandExecution", "fileChange") and cur:
                await self._event("tool_call", item_type=item["type"], tool=item.get("tool") or item.get("command"),
                                  arguments=_summ(item.get("arguments", "")), status=item.get("status"))
        elif method == "turn/completed":
            await self._on_turn_completed(p.get("turn") or {})
        elif method == "account/rateLimits/updated":
            await self.refresh_usage()
        elif method == "_exited":
            if cur:
                await self._state("failed", report="Codex（app-server）が終了しました")
                self.cur = None

    async def _on_turn_completed(self, turn: dict) -> None:
        cur = self.cur
        actions = self.guard.on_turn_completed()
        if cur is None:
            return
        cur.turn_id = None
        await self._event("turn_completed", status=turn.get("status"))
        suspend = next((a for a in actions if isinstance(a, Suspend)), None)
        if suspend or cur.suspend_at is not None:
            await self._state("suspended_quota", report=cur.last_message,
                              resume_at=suspend.resume_at if suspend else cur.suspend_at)
        elif cur.cancelling or turn.get("status") == "interrupted":
            await self._state("cancelled", report=cur.last_message)
        elif turn.get("status") == "failed":
            err = turn.get("error") or {}
            await self._state("failed", report=cur.last_message or err.get("message") or "Codex の作業が失敗しました")
        else:
            await self._state("completed", report=cur.last_message)
        self.on_log({"type": "final_report", "task_id": cur.task_id, "text": cur.last_message})
        self.cur = None
        asyncio.create_task(self.refresh_usage())

    async def _apply(self, actions) -> None:
        cur = self.cur
        for a in actions:
            if isinstance(a, Steer):
                if cur and cur.turn_id and cur.thread_id:
                    try:
                        await self.app.turn_steer(cur.thread_id, cur.turn_id, a.text)
                        await self._event("status", text="使用量の上限が近いため、区切りで止めるよう指示しました")
                    except AppServerError:
                        self.guard.on_steer_failed()
                else:
                    self.guard.on_steer_failed()
            elif isinstance(a, Interrupt):
                if cur and cur.turn_id and cur.thread_id:
                    try:
                        await self.app.turn_interrupt(cur.thread_id, cur.turn_id)
                    except AppServerError:
                        pass
            elif isinstance(a, Suspend):
                if cur:
                    cur.suspend_at = a.resume_at
            elif isinstance(a, (BlockNew, Unblock)):
                pass  # the host blocks new tasks from the usage numbers

    # ---- periodic work -------------------------------------------------
    async def refresh_usage(self) -> None:
        if not self.guard.enabled:
            return
        try:
            snap = await self.app.rate_limits()
        except (AppServerError, asyncio.TimeoutError):
            return
        await self._safe_send(protocol.msg("usage", **snap.to_message()))
        await self._apply(self.guard.on_usage(snap))

    async def _usage_loop(self) -> None:
        while True:
            await self.refresh_usage()
            await asyncio.sleep(self.usage_interval)

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(self.tick_interval)
            await self._apply(self.guard.tick())
