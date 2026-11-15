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
from .pre_approval import CANCELLED, FOLLOWUP, START, WAITING, PreApproval
from .sessions import SessionRegistry
from .settings import defaults, feature_instructions
from .usage import BlockNew, Interrupt, QuotaGuard, Steer, Suspend, Unblock
from .worklog import WorkLog

PREAMBLE = ("ファイルの一覧・検索・読み書き・コマンド実行は、MCP サーバー bridge の道具だけを使ってください。"
            "パスは共有フォルダからの相対パスです。作業フォルダ（cwd）は空で、使いません。"
            "run_command や delete_path が承認待ち（waiting_approval）を返したら、作業を終えずに "
            "get_command_result（削除は同じ呼び出しのやり直し）で結果を待ってください。承認には数分かかることがあります。")
NOT_OWNED = "このツールで作ったセッションではありません"
NO_RESUME = "この PC の Codex は使い捨てセッションの設定のため、続きの依頼はできません。新しい依頼として出し直してください"
DISCARD_TIMEOUT = 5.0  # asking Codex to drop a disposable thread must never hold up the next event
SendFn = Callable[[dict], Awaitable[None]]
TOOL_ITEMS = ("mcpToolCall", "dynamicToolCall", "commandExecution", "fileChange")
PROGRESS_INTERVAL = 2.0  # the half-written message goes to the host at most this often (seconds)
PROGRESS_MAX = 20_000


@dataclass
class Current:
    task_id: str
    thread_id: str | None = None
    turn_id: str | None = None
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
                 progress_interval: float = PROGRESS_INTERVAL, clock: Callable[[], float] = time.monotonic) -> None:
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
        self.cur: Current | None = None
        self.settings = defaults()  # the lender's settings (settings.py); apply_settings() brings in a changed one
        self.approval = PreApproval(self)  # a request waiting for the lender's yes (design 4.5)
        self.log_visibility = "summary"
        self.friend_limits = {"five_hour": None, "weekly": None, "force": False}
        self._seq = itertools.count(int(time.time() * 1000))
        self._unacked: dict[int, dict] = {}
        self._tasks: list[asyncio.Task] = []
        self._lock = asyncio.Lock()

    # ---- lifecycle -----------------------------------------------------
    async def start(self) -> None:
        self.sessions.purge_ephemeral()
        self._prune_worklog()
        self._tasks = [asyncio.create_task(self._pump()), asyncio.create_task(self._usage_loop()),
                       asyncio.create_task(self._tick_loop())]

    @property
    def pending_approval(self) -> dict | None:
        """The request waiting for the lender's decision: task_id, prompt, kind (start | followup), created."""
        p = self.approval.pending
        return p.view() if p is not None else None

    async def decide_pending(self, approve: bool, reason: str = "") -> bool:
        """The lender's answer. False when nothing waits (decided already, timed out, or the task is over)."""
        return await self.approval.decide(approve, reason)

    async def expire_pending(self) -> None:
        await self.approval.expire()

    async def resend_pending(self) -> None:
        """After a reconnect (and its hello): tell the host again that a request waits."""
        await self.approval.resend()

    def active_task_id(self) -> str | None:
        """The task this agent is responsible for: the one working, or the one waiting for the lender's yes."""
        if self.cur is not None:
            return self.cur.task_id
        return self.approval.pending.task_id if self.approval.pending is not None else None

    async def shutdown(self) -> None:
        """The exe is ending: a request still waiting is refused, while the host can still be told."""
        await self.approval.shutdown()

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def apply_settings(self, settings: dict) -> None:
        """Settings the lender changed that work at once (what needs a Codex restart: SettingsApplier)."""
        self.settings = copy.deepcopy(settings)
        self.apply_local_limits(settings["limits"])
        if self.worklog is not None:
            self.worklog.retention_days = settings["log_retention_days"]

    @contextlib.asynccontextmanager
    async def exclusive(self):
        """No task can start inside; yields whether the agent is idle (to restart Codex or move the data)."""
        async with self._lock:
            yield self.cur is None

    async def _end(self, app_alive: bool = True) -> None:
        """The task is over (any way): a disposable session is dropped and no longer listed (design 4.6)."""
        cur, self.cur = self.cur, None
        if cur is not None:
            self.approval.drop(cur.task_id)  # a follow-up still waiting has nowhere to go any more
        if cur is not None and cur.ephemeral and cur.thread_id:
            self.sessions.remove(cur.thread_id)
            if app_alive:
                try:
                    await asyncio.wait_for(self.app.thread_delete(cur.thread_id), DISCARD_TIMEOUT)
                except Exception:
                    pass  # an ephemeral thread lives in memory only: nothing on disk to clean up

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
            self.worklog.add(task_id, kind, data)
        except Exception as e:
            self.on_log({"type": "error", "text": f"記録に失敗しました: {type(e).__name__}: {e}"})

    def _prune_worklog(self) -> None:
        if self.worklog is not None:
            try:
                self.worklog.prune()
            except Exception as e:
                self.on_log({"type": "error", "text": f"古い記録を消せませんでした: {type(e).__name__}: {e}"})

    async def _event(self, kind: str, **data) -> None:
        if self.cur:
            self._record(self.cur.task_id, kind, data)
            await self._send_seq("event", task_id=self.cur.task_id, kind=kind, data=data)

    async def _state(self, state: str, report: str | None = None, resume_at: int | None = None,
                     task_id: str | None = None, thread_id: str | None = None) -> None:
        cur = self.cur
        task_id = task_id or (cur.task_id if cur else None)
        self._record(task_id, "state", {"state": state, "report": report, "resume_at": resume_at})
        await self._send_seq("task_state", task_id=task_id, state=state,
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
            if self.cur is not None or self.approval.pending is not None:
                await self._state("failed", report="別のタスクが作業中です", task_id=m["task_id"])
                return
            if self.approval.enabled:
                await self.approval.hold(m, START)
                return
            await self._start_locked(m)

    async def _start_locked(self, m: dict, shown: bool = False) -> None:
        """Begin the task (the lock is held). `shown`: the request was shown to the lender already, when it was held."""
        if self.cur is not None:
            await self._state("failed", report="別のタスクが作業中です", task_id=m["task_id"])
            return
        self.cur = Current(task_id=m["task_id"], ephemeral=bool(self.settings["disposable"]))
        self._prune_worklog()
        if not shown:
            self.log_prompt(m["task_id"], m["prompt"])
        try:
            cont = m.get("continue_thread_id")
            if cont:
                if self.cur.ephemeral:
                    raise AppServerError(NO_RESUME)
                if not self.sessions.owns(cont):
                    raise AppServerError(NOT_OWNED)
                self.cur.thread_id = await self.app.thread_resume(cont, self._instructions())
            else:
                self.cur.thread_id = await self.app.thread_start(self._instructions(), self.cur.ephemeral)
            self.sessions.add(self.cur.thread_id, self.cur.task_id, self.cur.ephemeral)
            await self._state("running")
            await self._event("started")
            self.cur.turn_id = await self.app.turn_start(self.cur.thread_id, m["prompt"])
        except Exception as e:  # app-server gone, pipe broken, timeout...: never keep a stale task
            await self._state("failed", report=f"開始できませんでした: {type(e).__name__}: {e}")
            await self._end()

    async def _followup(self, m: dict) -> None:
        if self.approval.holds(m["task_id"]):  # what the lender is deciding on must not change under their hands
            text = {"text": WAITING}
            self._record(m["task_id"], "status", text)
            await self._send_seq("event", task_id=m["task_id"], kind="status", data=text)
            return
        cur = self.cur
        if cur is None or cur.task_id != m["task_id"] or cur.thread_id is None:
            return
        if self.approval.enabled:
            await self.approval.hold(m, FOLLOWUP)
            return
        await self._deliver_followup(m)

    async def _deliver_followup(self, m: dict, shown: bool = False) -> None:
        cur = self.cur
        if cur is None or cur.task_id != m["task_id"] or cur.thread_id is None:
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
            cur.turn_id = await self.app.turn_start(cur.thread_id, m["text"])
        except Exception as e:
            await self._state("failed", report=f"追加の指示を渡せませんでした: {type(e).__name__}: {e}")
            await self._end()

    async def _cancel(self, m: dict) -> None:
        held = self.approval.drop(m["task_id"])
        if held is not None and held.kind == START:  # it never began: nothing to interrupt
            await self._state("cancelled", report=CANCELLED, task_id=held.task_id)
            return
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
        await self._end()

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
                cur.tokens_in = cur.tokens_out = 0
                cur.tokens_seen = False
            await self._apply(self.guard.on_turn_started(p["turn"]["id"]))
        elif method == "item/agentMessage/delta":
            if cur:
                await self._on_delta(cur, p)
        elif method == "item/started":
            item = p.get("item") or {}
            if item.get("type") in TOOL_ITEMS and cur:
                await self._event("tool_started", tool=_tool_name(item), arguments=_cap(item.get("arguments")))
        elif method == "item/completed":
            item = p.get("item") or {}
            if item.get("type") == "agentMessage" and cur:
                cur.last_message = item.get("text")
                cur.progress, cur.progress_item = "", None  # complete: the next message starts afresh
                await self._event("agent_message", text=item.get("text"))
            elif item.get("type") == "reasoning" and cur:
                parts = [x.strip() for x in item.get("summary") or [] if isinstance(x, str) and x.strip()]
                if parts:  # the summary only, never the reasoning itself ("content")
                    await self._event("reasoning_summary", text=_summ("\n".join(parts), 4000))
            elif item.get("type") in TOOL_ITEMS and cur:
                await self._event("tool_finished", tool=_tool_name(item), arguments=_cap(item.get("arguments")),
                                  duration_ms=_duration_ms(item),
                                  success=item.get("status") == "completed" and not item.get("error")
                                  and item.get("exitCode") in (None, 0),
                                  result_summary=_tool_result(item))
        elif method == "turn/plan/updated":
            if cur:
                steps = [{"step": str(x.get("step")), "status": x.get("status")}
                         for x in p.get("plan") or [] if isinstance(x, dict)]
                await self._event("plan", steps=steps)
        elif method == "thread/tokenUsage/updated":
            last = (p.get("tokenUsage") or {}).get("last") or {}
            if cur:  # one model call; the turn's total goes out when the turn ends
                for attr, key in (("tokens_in", "inputTokens"), ("tokens_out", "outputTokens")):
                    n = last.get(key)
                    if isinstance(n, int) and not isinstance(n, bool):
                        setattr(cur, attr, getattr(cur, attr) + n)
                        cur.tokens_seen = True
        elif method == "turn/completed":
            await self._on_turn_completed(p.get("turn") or {})
        elif method == "account/rateLimits/updated":
            await self.refresh_usage()
        elif method == "_exited":
            if cur:
                await self._state("failed", report="Codex（app-server）が終了しました")
                await self._end(app_alive=False)

    async def _on_turn_completed(self, turn: dict) -> None:
        cur = self.cur
        actions = self.guard.on_turn_completed()
        if cur is None:
            return
        cur.turn_id = None
        if cur.tokens_seen:
            await self._event("token_usage", input=cur.tokens_in, output=cur.tokens_out)
        cur.tokens_in = cur.tokens_out = 0
        cur.tokens_seen = False
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
        await self._end()
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

    async def _apply(self, actions) -> None:
        cur = self.cur
        for a in actions:
            if isinstance(a, Steer):
                if cur and cur.turn_id and cur.thread_id:
                    try:
                        await self.app.turn_steer(cur.thread_id, cur.turn_id, a.text)
                        await self._event("status", text="使用量の上限が近いため、区切りで止めるよう指示しました")
                    except Exception:  # includes timeouts: retry at the next turn start
                        self.guard.on_steer_failed()
                else:
                    self.guard.on_steer_failed()
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

    # ---- periodic work -------------------------------------------------
    async def refresh_usage(self) -> None:
        if not self.guard.enabled:
            return
        try:
            snap = await self.app.rate_limits()
        except Exception:  # app-server restarting, pipe closed, timeout
            return
        await self._safe_send(protocol.msg("usage", **snap.to_message(), friend_limits=self.friend_limits))
        await self._apply(self.guard.on_usage(snap))

    def apply_local_limits(self, limits: dict) -> None:
        """The friend changed their own limits on the log page: apply now and tell the host."""
        self.friend_limits = {"five_hour": limits.get("five_hour"), "weekly": limits.get("weekly"),
                              "force": bool(limits.get("force"))}
        self.guard.set_local_limits(self.friend_limits, force=self.friend_limits["force"])
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
                await self._apply(self.guard.tick())
                await self.approval.expire()
            except Exception as e:
                self.on_log({"type": "error", "text": f"tick: {type(e).__name__}: {e}"})
