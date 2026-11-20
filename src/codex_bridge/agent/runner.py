"""Runs host tasks on the local app-server and reports back to the host."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import itertools
import json
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from ..common import protocol
from .appserver import AppServer, AppServerError
from .models import ModelRefused, choose
from .pre_approval import CANCELLED, FOLLOWUP, START, WAITING, PreApproval
from .sessions import SessionRegistry
from .settings import defaults, feature_instructions, models_fields
from .usage import BlockNew, Interrupt, QuotaGuard, Steer, Suspend, Unblock
from .worklog import WorkLog

PREAMBLE = ("ファイルの一覧・検索・読み書き・コマンド実行は、MCP サーバー bridge の道具だけを使ってください。"
            "共有フォルダは複数登録できます。"
            'list_dir("/")（path 省略・空文字も同じ）で登録フォルダ一覧を確認してください。'
            "メイン内は相対パス、ほかの共有フォルダ内は絶対パス（区切りは /）です。"
            '明示的な "." はメインを指し、find_files と grep は path 省略で見えるすべてのフォルダを検索します。'
            "結果のパスも同じ規則です。作業フォルダ（cwd）は空で、使いません。"
            "run_command や delete_path が承認待ち（waiting_approval）を返したら、作業を終えずに "
            "get_command_result（削除は同じ呼び出しのやり直し）で結果を待ってください。承認には数分かかることがあります。")
NOT_OWNED = "このツールで作ったセッションではありません"
NO_RESUME = "この PC の Codex は使い捨てセッションの設定のため、続きの依頼はできません。新しい依頼として出し直してください"
DISCARD_TIMEOUT = 5.0  # asking Codex to drop a disposable thread must never hold up the next event
SendFn = Callable[[dict], Awaitable[None]]
TOOL_ITEMS = ("mcpToolCall", "dynamicToolCall", "commandExecution", "fileChange")
PROGRESS_INTERVAL = 2.0  # the half-written message goes to the host at most this often (seconds)
PROGRESS_MAX = 20_000
INTERRUPTED = "アプリが終了したため、タスクは中断されました。"


class TaskSlot:
    """Admission lock and capacity shared by every requester on this PC."""

    def __init__(self):
        self.lock = asyncio.Lock()
        self.max_parallel = 3
        self.runners = []
        self.last_usage = None
        self.moving = False
        self.blocked_runners = set()

    @property
    def count(self) -> int:
        return sum(len(r.active_task_ids()) for r in self.runners)

    @property
    def owner(self):
        """Compatibility view for callers that only need to know whether anything is active."""
        return next((r for r in self.runners if r.active_task_ids()), None)

    async def announce(self):
        for runner in list(self.runners):
            await runner.send_busy_status()

    async def usage(self, snapshot):
        self.last_usage = snapshot
        for runner in list(self.runners):
            runner.last_usage = snapshot
            await runner._safe_send(protocol.msg("usage", **snapshot.to_message(), friend_limits=runner.friend_limits))
            await runner._on_usage(snapshot)


@dataclass
class Current:
    task_id: str
    thread_id: str | None = None
    turn_id: str | None = None
    model: str | None = None
    effort: str | None = None
    last_message: str | None = None
    cancelling: bool = False
    ephemeral: bool = False            # a disposable session: fixed when the task starts, dropped when it ends
    suspend_at: int | None = None
    followups: list[str] = field(default_factory=list)
    progress: str = ""                 # the agent message being written (item/agentMessage/delta)
    progress_item: str | None = None
    progress_sent_at: float | None = None
    tokens_in: int = 0                 # this turn so far, summed over its model calls
    tokens_out: int = 0
    tokens_seen: bool = False
    guard: QuotaGuard | None = None
    prepared: bool = False  # startup RPCs have begun; another prepare must not restart this app-server


def _summ(v, n=300):
    s = v if isinstance(v, str) else str(v)
    return s if len(s) <= n else s[:n] + "…"


def _cap(v, n=1000):
    """A value of a tool call as it is sent: as it is when small, else as cut-off text (a write_file call carries a whole file)."""
    if v is None:
        return ""
    if isinstance(v, str):
        return _summ(v, n)
    try:
        dumped = json.dumps(v, ensure_ascii=False)
    except (TypeError, ValueError):
        return _summ(v, n)
    return v if len(dumped) <= n else dumped[:n] + "…"


def _tool_name(item: dict):
    return item.get("tool") or item.get("command") or item.get("type")


def _tool_result(item: dict) -> str:
    err = item.get("error")
    if err:
        return _summ(err.get("message") if isinstance(err, dict) and err.get("message") else err)
    res = item.get("result")
    if isinstance(res, dict):
        texts = [c.get("text") for c in res.get("content") or [] if isinstance(c, dict) and isinstance(c.get("text"), str)]
        return _summ("\n".join(texts)) if texts else ""
    return _summ(res) if res else _summ(item.get("aggregatedOutput") or "")


def _duration_ms(item: dict):
    ms = item.get("durationMs")
    if isinstance(ms, (int, float)) and not isinstance(ms, bool):
        return ms
    a, b = item.get("startedAtMs"), item.get("completedAtMs")
    return b - a if isinstance(a, (int, float)) and isinstance(b, (int, float)) else None


class AgentRunner:
    def __init__(self, app: AppServer, guard: QuotaGuard, sessions: SessionRegistry, send: SendFn,
                 usage_interval: float = 60, tick_interval: float = 5,
                 on_log: Callable[[dict], None] | None = None, worklog: WorkLog | None = None,
                 progress_interval: float = PROGRESS_INTERVAL, clock: Callable[[], float] = time.monotonic,
                 slot: TaskSlot | None = None, connection_id: str | None = None, host_name: str | None = None,
                 prepare: Callable[[], Awaitable[None]] | None = None) -> None:
        self.app = app
        self.guard = guard
        self.sessions = sessions
        self.send = send
        self.usage_interval = usage_interval
        self.tick_interval = tick_interval
        self.on_log = on_log or (lambda e: None)
        self.worklog = worklog
        self.progress_interval = progress_interval
        self.clock = clock
        self.currents: dict[str, Current] = {}
        self.settings = defaults()  # the lender's settings (settings.py); apply_settings() brings in a changed one
        self.catalog: list[dict] | None = None
        self.approval = PreApproval(self)  # a request waiting for the lender's yes (design 4.5)
        self.last_usage = None  # latest snapshot for the local screen
        self.log_visibility = "summary"
        self.friend_limits = {"five_hour": None, "weekly": None, "force": False}
        self._seq = itertools.count(int(time.time() * 1000))
        self._unacked: dict[int, dict] = {}
        self._tasks: list[asyncio.Task] = []
        self.slot = slot
        self.connection_id, self.host_name, self.prepare = connection_id, host_name, prepare
        self._lock = asyncio.Lock()
        self._prepare_lock = asyncio.Lock()
        self._reservations: dict[str, str | None] = {}
        self._incoming: set[asyncio.Task] = set()
        self._closing = False
        if slot:
            slot.runners.append(self)

    def connection_fields(self) -> dict:
        return {"connection_id": self.connection_id, "host_name": self.host_name} if self.connection_id else {}

    @property
    def cur(self) -> Current | None:
        """Compatibility view, usable only when a single task is running."""
        return next(iter(self.currents.values())) if len(self.currents) == 1 else None

    @cur.setter
    def cur(self, cur: Current | None) -> None:
        self.currents = {cur.task_id: cur} if cur else {}

    def thread_active(self, thread_id: str, except_task: str | None = None) -> bool:
        return any(c.task_id != except_task and c.thread_id == thread_id for c in self.currents.values()) or any(
            p.task_id != except_task and p.kind == START and p.msg.get("continue_thread_id") == thread_id
            for p in self.approval.pendings.values()) or any(
            task != except_task and thread == thread_id for task, thread in self._reservations.items())

    async def send_busy_status(self) -> None:
        if self.slot:
            await self._safe_send(protocol.msg("lender_status", busy_elsewhere=self.slot.count >= self.slot.max_parallel))

    async def send_models(self) -> None:
        await self._safe_send(protocol.msg("lender_models", **models_fields(self.settings, self.catalog)))

    async def _release_slot(self, task_id: str | None = None) -> None:
        if self.slot:
            async with self.slot.lock:
                if task_id is not None:
                    self._reservations.pop(task_id, None)
            await self.slot.announce()
        elif task_id is not None:
            self._reservations.pop(task_id, None)

    async def _reserve_slot(self, m: dict) -> str | None:
        def reserve():
            if self._closing:
                return "アプリの終了処理中のため、今は依頼を受け付けません。"
            if self.slot and (self.slot.moving or self in self.slot.blocked_runners):
                return "接続先や保存場所を変更中のため、今は依頼を受け付けません。"
            count = self.slot.count if self.slot else len(self.active_task_ids())
            maximum = self.slot.max_parallel if self.slot else self.settings["max_parallel"]
            if count >= maximum:
                return f"同時に受ける依頼の上限（{maximum} 件）に達しています。"
            self._reservations[m["task_id"]] = m.get("continue_thread_id")
            return None
        if self.slot:
            async with self.slot.lock:
                return reserve()
        return reserve()

    # ---- lifecycle -----------------------------------------------------
    async def start(self) -> None:
        self._closing = False
        self.sessions.purge_ephemeral()
        self._prune_worklog()
        self._tasks = [asyncio.create_task(self._pump()), asyncio.create_task(self._usage_loop()),
                       asyncio.create_task(self._tick_loop())]

    @property
    def pending_approvals(self) -> list[dict]:
        return [{**p.view(), **self.connection_fields()} for p in self.approval.pendings.values()]

    @property
    def pending_approval(self) -> dict | None:
        """Compatibility view for a single pending decision."""
        return next(iter(self.pending_approvals), None)

    async def decide_pending(self, approve: bool, reason: str = "", task_id: str | None = None,
                             created: float | None = None) -> bool:
        return await self.approval.decide(approve, reason, task_id=task_id, created=created)

    async def expire_pending(self) -> None:
        await self.approval.expire()

    async def resend_pending(self) -> None:
        await self.approval.resend()

    def active_task_ids(self) -> list[str]:
        return list(dict.fromkeys([*self.currents, *self.approval.pendings, *self._reservations]))

    def active_task_id(self) -> str | None:
        return next(iter(self.active_task_ids()), None)

    async def shutdown(self) -> None:
        """Record the interruption while the host is still connected, before event intake stops."""
        self._closing = True
        await self.approval.shutdown()
        async with self._lock:
            for cur in list(self.currents.values()):
                await self._state("failed", report=INTERRUPTED, cur=cur)
                await self._end(cur=cur, app_alive=False)
            for task_id in list(self._reservations):
                await self._state("failed", report=INTERRUPTED, task_id=task_id)
                await self._release_slot(task_id)

    async def stop(self) -> None:
        tasks = [*self._tasks, *self._incoming]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def apply_settings(self, settings: dict) -> None:
        """Settings the lender changed that work at once (what needs a Codex restart: SettingsApplier)."""
        self.settings = {**defaults(), **copy.deepcopy(settings)}
        if self.slot:
            self.slot.max_parallel = self.settings["max_parallel"]
        self.apply_local_limits(settings["limits"])
        if self.worklog is not None:
            self.worklog.retention_days = settings["log_retention_days"]

    @contextlib.asynccontextmanager
    async def exclusive(self):
        """No task can start inside; yields whether the agent is idle (to restart Codex or move the data)."""
        async with self._lock:
            yield (not self.active_task_ids() and not self._closing and
                   not (self.slot and (self.slot.moving or self in self.slot.blocked_runners)))

    async def _end(self, app_alive: bool = True, *, cur: Current | None = None) -> None:
        """End only this task; disposable threads and pending follow-ups belong to it alone."""
        cur = cur or self.cur
        if cur is not None:
            self.currents.pop(cur.task_id, None)
            if cur.guard:
                cur.guard.on_turn_completed()
        if cur is not None:
            self.approval.drop(cur.task_id)  # a follow-up still waiting has nowhere to go any more
        if cur is not None and cur.ephemeral and cur.thread_id:
            self.sessions.remove(cur.thread_id)
            if app_alive:
                try:
                    await asyncio.wait_for(self.app.thread_delete(cur.thread_id), DISCARD_TIMEOUT)
                except Exception:
                    pass  # an ephemeral thread lives in memory only: nothing on disk to clean up
        await self._release_slot(cur.task_id if cur else None)

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

    def _record(self, task_id: str | None, kind: str, data) -> None:
        """Into the work log (design 4.10); a disk error must never keep Codex's progress from the host."""
        if self.worklog is None or not task_id:
            return
        try:
            self.worklog.add(task_id, kind, data, **self.connection_fields())
        except Exception as e:
            self.on_log({"type": "error", "text": f"記録に失敗しました: {type(e).__name__}: {e}"})

    def _prune_worklog(self) -> None:
        if self.worklog is not None:
            try:
                self.worklog.prune()
            except Exception as e:
                self.on_log({"type": "error", "text": f"古い記録を消せませんでした: {type(e).__name__}: {e}"})

    async def _event(self, kind: str, *, cur: Current | None = None, **data) -> None:
        cur = cur or self.cur
        if cur:
            self._record(cur.task_id, kind, data)
            await self._send_seq("event", task_id=cur.task_id, kind=kind, data=data)

    async def _state(self, state: str, report: str | None = None, resume_at: int | None = None,
                     task_id: str | None = None, thread_id: str | None = None, cur: Current | None = None) -> None:
        cur = cur or (self.currents.get(task_id) if task_id else self.cur)
        task_id = task_id or (cur.task_id if cur else None)
        self._record(task_id, "state", {"state": state, "report": report, "resume_at": resume_at})
        await self._send_seq("task_state", task_id=task_id, state=state,
                             thread_id=thread_id or (cur.thread_id if cur else None), report=report,
                             resume_at=resume_at)

    async def _reply(self, req_id: str, ok: bool, data=None, error: str | None = None) -> None:
        await self._safe_send(protocol.msg("reply", req_id=req_id, ok=ok, data=data, error=error))

    # ---- host messages -------------------------------------------------
    def handle_later(self, m: dict) -> None:
        """Keep control-channel intake responsive while an app-server request is pending."""
        task = asyncio.create_task(self.handle(m))
        self._incoming.add(task)
        task.add_done_callback(self._incoming.discard)

    async def handle(self, m: dict) -> None:
        t = m["type"]
        if t == "ack":
            self._unacked.pop(m["seq"], None)
        elif t == "settings":
            # m["thresholds"] is ignored: the borrower sets none any more, the lender's own limits are the only ones
            if self.connection_id is None:  # v0.1-style single runner: keep the old host-controlled display
                self.log_visibility = m.get("log_visibility", self.log_visibility)
            self.on_log({"type": "settings", "log_visibility": self.log_visibility})
            # Re-check usage now so a lowered threshold takes effect without waiting for the next poll.
            asyncio.create_task(self.refresh_usage())
        else:
            try:
                await self._dispatch(t, m)
            except Exception as e:  # one bad message must never stop the control loop
                self.on_log({"type": "error", "text": f"{t}: {type(e).__name__}: {e}"})
                if "req_id" in m:
                    await self._reply(m["req_id"], False, error=f"{type(e).__name__}: {e}")

    async def _dispatch(self, t: str, m: dict) -> None:
        if t == "task_start":
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

    def _instructions(self) -> str:
        return PREAMBLE + feature_instructions(self.settings["features"])

    def log_prompt(self, task_id: str, text: str) -> None:
        """What the borrower asked is always shown to the lender (design 4.9)."""
        self.on_log({"type": "task_prompt", "task_id": task_id, "prompt": text})
        self._record(task_id, "task_prompt", {"text": text})

    async def _task_start(self, m: dict) -> None:
        async with self._lock:
            if m["task_id"] in self.active_task_ids():
                return
            cont = m.get("continue_thread_id")
            if cont and self.thread_active(cont):
                await self._state("rejected_by_lender", report="この会話は作業中です。終わるのを待ってください。",
                                  task_id=m["task_id"])
                return
            if self.guard.enabled and self.guard.blocked_until is not None:
                await self._state("suspended_quota", resume_at=self.guard.blocked_until, task_id=m["task_id"])
                return
            refusal = await self._reserve_slot(m)
            if refusal:
                await self._state("rejected_by_lender", report=refusal, task_id=m["task_id"])
                return
            if self.approval.applies(START):
                await self.approval.hold(m, START)
                await self._release_slot()
                return
        await self._start_locked(m)

    def _new_guard(self) -> QuotaGuard:
        if not any(c.guard is self.guard for c in self.currents.values()):
            return self.guard
        guard = QuotaGuard(self.guard.thresholds, self.guard.grace_sec, self.guard.clock)
        guard.enabled = self.guard.enabled
        guard.set_local_limits(self.friend_limits, force=self.friend_limits["force"])
        guard.blocked_until = self.guard.blocked_until
        return guard

    async def _start_locked(self, m: dict, shown: bool = False) -> None:
        """Create local state under the runner lock; never hold it during startup RPCs."""
        async with self._lock:
            if m["task_id"] not in self._reservations:
                return  # Cancelled while the approved request was being handed over.
            if self._closing:
                await self._state("failed", report=INTERRUPTED, task_id=m["task_id"])
                await self._release_slot(m["task_id"])
                return
            if m.get("continue_thread_id") and self.thread_active(m["continue_thread_id"], except_task=m["task_id"]):
                await self._state("rejected_by_lender", report="この会話は作業中です。終わるのを待ってください。",
                                  task_id=m["task_id"])
                await self._release_slot(m["task_id"])
                return
            guard = self._new_guard()
            cur = Current(task_id=m["task_id"], ephemeral=bool(self.settings["disposable"]), guard=guard)
            self.currents[cur.task_id] = cur
        await self._release_slot()
        self._prune_worklog()
        if not shown:
            self.log_prompt(m["task_id"], m["prompt"])
        try:
            if self.prepare:
                await self.prepare()
            cur.prepared = True
            if self.currents.get(cur.task_id) is not cur:
                return
            if guard.enabled and guard.blocked_until is not None:
                await self._state("suspended_quota", resume_at=guard.blocked_until, cur=cur)
                await self._end(cur=cur)
                return
            model, effort = choose(self.catalog, self.settings["allowed_models"], self.settings["max_effort"],
                                   m.get("model"), m.get("effort"))
            cont = m.get("continue_thread_id")
            if cont:
                if cur.ephemeral:
                    raise AppServerError(NO_RESUME)
                if not self.sessions.owns(cont):
                    raise AppServerError(NOT_OWNED)
                info = await self.app.thread_resume(cont, self._instructions(), model=model)
            else:
                info = await self.app.thread_start(self._instructions(), cur.ephemeral, model=model)
            cur.thread_id = info["id"]
            if self.currents.get(cur.task_id) is not cur:
                if cur.ephemeral or not cont:
                    with contextlib.suppress(Exception):
                        await self.app.thread_delete(cur.thread_id)
                return
            cur.model = info["model"] or model
            cur.effort = effort
            self.sessions.add(cur.thread_id, cur.task_id, cur.ephemeral)
            await self._state("running", cur=cur)
            await self._event("started", cur=cur)
            await self._event("model", cur=cur, model=cur.model, effort=cur.effort)
            turn_id = await self.app.turn_start(cur.thread_id, m["prompt"], model=cur.model, effort=cur.effort)
            if self.currents.get(cur.task_id) is cur:
                cur.turn_id = turn_id
            elif cur.cancelling:
                with contextlib.suppress(Exception):
                    await self.app.turn_interrupt(cur.thread_id, turn_id)
                if cur.ephemeral:
                    with contextlib.suppress(Exception):
                        await self.app.thread_delete(cur.thread_id)
        except asyncio.CancelledError:
            if self.currents.get(cur.task_id) is cur:
                await self._state("failed", report=INTERRUPTED, cur=cur)
                await self._end(cur=cur, app_alive=False)
            raise
        except Exception as e:
            if self.currents.get(cur.task_id) is cur:
                state = "rejected_by_lender" if isinstance(e, ModelRefused) else "failed"
                report = str(e) if isinstance(e, ModelRefused) else f"開始できませんでした: {type(e).__name__}: {e}"
                await self._state(state, report=report, cur=cur)
                await self._end(cur=cur)
        finally:
            if self.currents.get(cur.task_id) is not cur:
                await self._release_slot(cur.task_id)

    async def _followup(self, m: dict) -> None:
        await self._followup_locked(m)

    async def _followup_locked(self, m: dict) -> None:
        if self.approval.holds(m["task_id"]):  # what the lender is deciding on must not change under their hands
            text = {"text": WAITING}
            self._record(m["task_id"], "status", text)
            await self._send_seq("event", task_id=m["task_id"], kind="status", data=text)
            return
        cur = self.currents.get(m["task_id"])
        if cur is None or cur.thread_id is None:
            return
        if self.approval.applies(FOLLOWUP):
            await self.approval.hold(m, FOLLOWUP)
            return
        await self._deliver_followup(m)

    async def _deliver_followup(self, m: dict, shown: bool = False) -> None:
        cur = self.currents.get(m["task_id"])
        if cur is None or cur.thread_id is None:
            return
        if not shown:
            self.log_prompt(cur.task_id, m["text"])
        if cur.turn_id:
            try:
                await self.app.turn_steer(cur.thread_id, cur.turn_id, m["text"])
                return
            except Exception:
                pass  # the turn may have just ended: start a new one below
        try:
            cur.turn_id = await self.app.turn_start(cur.thread_id, m["text"], model=cur.model, effort=cur.effort)
        except Exception as e:
            await self._state("failed", cur=cur, report=f"追加の指示を渡せませんでした: {type(e).__name__}: {e}")
            await self._end(cur=cur)

    async def _cancel(self, m: dict) -> None:
        async with self._lock:
            await self._cancel_locked(m)

    async def _cancel_locked(self, m: dict) -> None:
        held = self.approval.drop(m["task_id"])
        if held is not None and held.kind == START:  # it never began: nothing to interrupt
            await self._state("cancelled", report=CANCELLED, task_id=held.task_id)
            await self._release_slot(held.task_id)
            return
        cur = self.currents.get(m["task_id"])
        if cur is None:
            if m["task_id"] in self._reservations:
                await self._state("cancelled", report=CANCELLED, task_id=m["task_id"])
                await self._release_slot(m["task_id"])
            return
        cur.cancelling = True
        if cur.turn_id and cur.thread_id:
            try:
                await self.app.turn_interrupt(cur.thread_id, cur.turn_id)
                return
            except AppServerError:
                pass
        await self._state("cancelled", cur=cur, report=cur.last_message)
        await self._end(cur=cur)

    async def _session_op(self, m: dict, read: bool) -> None:
        async with self._lock:
            await self._session_op_locked(m, read)

    async def _session_op_locked(self, m: dict, read: bool) -> None:
        tid = m["thread_id"]
        if not self.sessions.owns(tid):
            await self._reply(m["req_id"], False, error=NOT_OWNED)
            return
        try:
            if self.prepare:
                await self.prepare()
            if read:
                await self._reply(m["req_id"], True, data=await self.app.thread_read(tid))
            else:
                if self.thread_active(tid):
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
                async with self._lock:
                    await self._on_event(ev)
            except Exception as e:  # never let one bad event stop the pump
                self.on_log({"type": "error", "text": f"{type(e).__name__}: {e}"})

    async def _on_event(self, ev: dict) -> None:
        method, p = ev.get("method"), ev.get("params") or {}
        thread_id = p.get("threadId")
        cur = next((c for c in self.currents.values() if c.thread_id == thread_id), None) if thread_id else self.cur
        turn_id = p.get("turnId") or (p.get("turn") or {}).get("id")
        if cur and turn_id and cur.turn_id and turn_id != cur.turn_id:
            return  # A late event from a previous turn must not finish a continuation.
        if method == "model/rerouted":
            if cur and p.get("threadId") == cur.thread_id:
                old = cur.model
                cur.model = p.get("toModel")
                await self._event("model", cur=cur, model=cur.model, effort=cur.effort,
                                  rerouted_from=p.get("fromModel") or old, reason=p.get("reason"))
        elif method == "turn/started":
            if cur:
                cur.turn_id = p["turn"]["id"]
                cur.tokens_in = cur.tokens_out = 0
                cur.tokens_seen = False
                await self._apply(self._guard_for(cur).on_turn_started(p["turn"]["id"]), cur=cur)
        elif method == "item/agentMessage/delta":
            if cur:
                await self._on_delta(cur, p)
        elif method == "item/started":
            item = p.get("item") or {}
            if item.get("type") in TOOL_ITEMS and cur:
                await self._event("tool_started", cur=cur, tool=_tool_name(item), arguments=_cap(item.get("arguments")))
        elif method == "item/completed":
            item = p.get("item") or {}
            if item.get("type") == "agentMessage" and cur:
                cur.last_message = item.get("text")
                cur.progress, cur.progress_item = "", None  # complete: the next message starts afresh
                await self._event("agent_message", cur=cur, text=item.get("text"))
            elif item.get("type") == "reasoning" and cur:
                parts = [x.strip() for x in item.get("summary") or [] if isinstance(x, str) and x.strip()]
                if parts:  # the summary only, never the reasoning itself ("content")
                    await self._event("reasoning_summary", cur=cur, text=_summ("\n".join(parts), 4000))
            elif item.get("type") in TOOL_ITEMS and cur:
                await self._event("tool_finished", cur=cur, tool=_tool_name(item), arguments=_cap(item.get("arguments")),
                                  duration_ms=_duration_ms(item),
                                  success=item.get("status") == "completed" and not item.get("error")
                                  and item.get("exitCode") in (None, 0),
                                  result_summary=_tool_result(item))
        elif method == "turn/plan/updated":
            if cur:
                steps = [{"step": str(x.get("step")), "status": x.get("status")}
                         for x in p.get("plan") or [] if isinstance(x, dict)]
                await self._event("plan", cur=cur, steps=steps)
        elif method == "thread/tokenUsage/updated":
            last = (p.get("tokenUsage") or {}).get("last") or {}
            if cur:  # one model call; the turn's total goes out when the turn ends
                for attr, key in (("tokens_in", "inputTokens"), ("tokens_out", "outputTokens")):
                    n = last.get(key)
                    if isinstance(n, int) and not isinstance(n, bool):
                        setattr(cur, attr, getattr(cur, attr) + n)
                        cur.tokens_seen = True
        elif method == "turn/completed":
            if cur:
                await self._on_turn_completed(p.get("turn") or {}, cur=cur)
        elif method == "account/rateLimits/updated":
            await self.refresh_usage()
        elif method == "_exited":
            for cur in list(self.currents.values()):
                await self._state("failed", report="Codex（app-server）が終了しました", cur=cur)
                await self._end(app_alive=False, cur=cur)

    async def _on_turn_completed(self, turn: dict, *, cur: Current | None = None) -> None:
        cur = cur or self.cur
        actions = self._guard_for(cur).on_turn_completed() if cur else []
        if cur is None:
            return
        cur.turn_id = None
        if cur.tokens_seen:
            await self._event("token_usage", cur=cur, input=cur.tokens_in, output=cur.tokens_out)
        cur.tokens_in = cur.tokens_out = 0
        cur.tokens_seen = False
        await self._event("turn_completed", cur=cur, status=turn.get("status"))
        suspend = next((a for a in actions if isinstance(a, Suspend)), None)
        if suspend or cur.suspend_at is not None:
            await self._state("suspended_quota", cur=cur, report=cur.last_message,
                              resume_at=suspend.resume_at if suspend else cur.suspend_at)
        elif cur.cancelling or turn.get("status") == "interrupted":
            await self._state("cancelled", cur=cur, report=cur.last_message)
        elif turn.get("status") == "failed":
            err = turn.get("error") or {}
            await self._state("failed", cur=cur, report=cur.last_message or err.get("message") or "Codex の作業が失敗しました")
        else:
            await self._state("completed", cur=cur, report=cur.last_message)
        self.on_log({"type": "final_report", "task_id": cur.task_id, "text": cur.last_message})
        await self._end(cur=cur)
        asyncio.create_task(self.refresh_usage())

    async def _on_delta(self, cur: Current, p: dict) -> None:
        """The message being written goes to the host at most every progress_interval seconds. It has no
        seq (an old one is not worth re-sending) and is not recorded: the finished message follows."""
        if cur.progress_item != p.get("itemId"):
            cur.progress, cur.progress_item = "", p.get("itemId")
        delta = p.get("delta")
        if isinstance(delta, str) and len(cur.progress) < PROGRESS_MAX:
            cur.progress = (cur.progress + delta)[:PROGRESS_MAX]
        now = self.clock()
        if cur.progress and (cur.progress_sent_at is None or now - cur.progress_sent_at >= self.progress_interval):
            cur.progress_sent_at = now
            await self._safe_send(protocol.msg("event", task_id=cur.task_id, kind="progress",
                                               data={"text": cur.progress}))

    def _guard_for(self, cur: Current) -> QuotaGuard:
        guard = cur.guard or self.guard
        guard.enabled = self.guard.enabled
        if not guard.enabled:
            cur.suspend_at = None
        return guard

    def set_usage_enabled(self, enabled: bool) -> None:
        self.guard.enabled = enabled
        for cur in self.currents.values():
            self._guard_for(cur)

    async def _apply(self, actions, *, cur: Current | None = None) -> None:
        cur = cur or self.cur
        guard = self._guard_for(cur) if cur else self.guard
        for a in actions:
            if isinstance(a, Steer):
                if cur and cur.turn_id and cur.thread_id:
                    try:
                        await self.app.turn_steer(cur.thread_id, cur.turn_id, a.text)
                        await self._event("status", cur=cur, text="使用量の上限に達したため、作業をすぐ止めて報告するよう指示しました")
                    except Exception:  # includes timeouts: retry at the next turn start
                        guard.on_steer_failed()
                else:
                    guard.on_steer_failed()
            elif isinstance(a, Interrupt):
                if cur and cur.turn_id and cur.thread_id:
                    try:
                        await self.app.turn_interrupt(cur.thread_id, cur.turn_id)
                    except Exception:
                        pass
            elif isinstance(a, Suspend):
                if cur:
                    cur.suspend_at = a.resume_at
            elif isinstance(a, (BlockNew, Unblock)):
                pass  # the host blocks new tasks from the usage numbers

    def _guards(self) -> list[QuotaGuard]:
        return list(dict.fromkeys([self.guard, *(self._guard_for(c) for c in self.currents.values())]))

    async def _on_usage(self, snapshot) -> None:
        guards = self._guards()
        for guard in guards:
            actions = guard.on_usage(snapshot)
            for cur in list(self.currents.values()):
                if self._guard_for(cur) is guard:
                    await self._apply(actions, cur=cur)
        if self.guard.blocked_until is not None:
            for pending in list(self.approval.pendings.values()):
                if pending.kind == START and self.approval.pendings.get(pending.task_id) is pending:
                    self.approval.drop(pending.task_id)
                    await self._state("suspended_quota", task_id=pending.task_id, resume_at=self.guard.blocked_until)
                    await self._release_slot(pending.task_id)
            await self._release_slot()

    # ---- periodic work -------------------------------------------------
    async def refresh_usage(self) -> None:
        if not self.guard.enabled:
            return
        try:
            snap = await self.app.rate_limits()
        except Exception:  # app-server restarting, pipe closed, timeout
            return
        self.last_usage = snap
        if self.slot:
            await self.slot.usage(snap)
            return
        await self._safe_send(protocol.msg("usage", **snap.to_message(), friend_limits=self.friend_limits))
        await self._on_usage(snap)

    def apply_local_limits(self, limits: dict) -> None:
        """The friend changed their own limits on the log page: apply now and tell the host."""
        self.friend_limits = {"five_hour": limits.get("five_hour"), "weekly": limits.get("weekly"),
                              "force": bool(limits.get("force"))}
        for guard in self._guards():
            guard.set_local_limits(self.friend_limits, force=self.friend_limits["force"])
        try:
            asyncio.get_running_loop().create_task(self.refresh_usage())
        except RuntimeError:
            pass  # not in the loop thread; the next poll reports it

    async def _usage_loop(self) -> None:
        while True:
            try:
                await self.refresh_usage()
            except Exception as e:  # the guard must keep running
                self.on_log({"type": "error", "text": f"usage: {type(e).__name__}: {e}"})
            await asyncio.sleep(self.usage_interval)

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(self.tick_interval)
            try:
                for cur in list(self.currents.values()):
                    await self._apply(self._guard_for(cur).tick(), cur=cur)
                await self.approval.expire()
            except Exception as e:
                self.on_log({"type": "error", "text": f"tick: {type(e).__name__}: {e}"})
