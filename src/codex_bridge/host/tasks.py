"""Task and session bookkeeping on the host; talks to each lender's agent through its attached link."""

from __future__ import annotations

import asyncio
import itertools
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from ..common import protocol
from ..common.protocol import FINAL_STATES, LOG_VISIBILITY, TASK_STATES
from .lenders import (Lender, SendFn, as_protocol, as_text, blocked_until, lender_accepts, moment,
                      normalize_capabilities, normalize_effort, normalize_lender_view, normalize_limits,
                      normalize_models,
                      normalize_window, number, pick_lender)
from .paths import ToolError

ACTIVE_STATES = ("queued", "running", "agent_offline", "awaiting_lender")  # awaiting_lender: the lender has not approved yet
NO_THRESHOLDS: dict = {}  # the borrower sets no limit of its own: only the lender's `limits` count
REQUEST_TIMEOUT = 30.0
NOT_CONNECTED = "貸す人が接続されていません。貸す人の PC で codex-bridge が起動し、「貸す」タブに招待コードを入れているか確かめてください。"
BUSY = "同時に受ける依頼の上限（{} 件）に達しています"
QUOTA = "使用量の上限のため受け付けません。再開可能: {}"
# A continuation can only go to the lender whose Codex holds the session (design §3.3).
RETRY_FRESH = "続きとしては送れないので、前回の報告を添えて新しく依頼してください。"
NOT_READY = "貸す人「{}」は接続したばかりで、まだ準備ができていません。数秒待ってからやり直してください。"
AWAITING = ("{} は、貸す人の承認待ちです。承認されるまで追加の指示は送れません"
            "（待つか、取り消してください）。")
PROGRESS_MAX = 20_000  # characters of the half-written message that are kept
SPOILT ="記録が壊れていたため、このタスクがどう終わったかは分かりません。"


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
    lender: str | None = None  # invite name of the lender doing the task (None: a v0.1 record)
    finished: float | None = None  # when it reached a final state (None: still going, or an older record)
    guessed: bool = False  # "failed" is the host's own guess (the agent did not name the task in hello): the agent may still say how it ended
    origin: str | None = None
    session: str | None = None
    model: str | None = None
    effort: str | None = None
    actual_model: str | None = None
    actual_effort: str | None = None
    rerouted: dict | None = None
    notified_final: bool = False


def _known_state(v) -> str | None:
    return v if isinstance(v, str) and v in TASK_STATES else None


def _record(raw) -> TaskRecord | None:
    """A task record as read from disk, every value checked again: a file written before the values
    from an agent were checked on receipt may hold anything (NaN as the state, ...), and a later
    version may have added values this one does not know. None when it is not a record at all."""
    if not isinstance(raw, dict) or not as_text(raw.get("task_id")):
        return None
    state = _known_state(raw.get("state"))
    rerouted = raw.get("rerouted")
    rerouted = ({"from": rerouted["from"], "reason": as_text(rerouted.get("reason"))}
                if isinstance(rerouted, dict) and isinstance(rerouted.get("from"), str) else None)
    return TaskRecord(
        task_id=raw["task_id"], prompt=as_text(raw.get("prompt")) or "", created=number(raw.get("created")) or 0.0,
        state=state or "failed", thread_id=as_text(raw.get("thread_id")),
        report=as_text(raw.get("report")) or (None if state else SPOILT), resume_at=moment(raw.get("resume_at")),
        continue_task_id=as_text(raw.get("continue_task_id")),
        state_before_offline=_known_state(raw.get("state_before_offline")), lender=as_text(raw.get("lender")),
        finished=number(raw.get("finished")), guessed=raw.get("guessed") is True and state == "failed",
        origin=as_text(raw.get("origin")), session=as_text(raw.get("session")),
        model=as_text(raw.get("model")), effort=as_text(raw.get("effort")),
        actual_model=as_text(raw.get("actual_model")), actual_effort=as_text(raw.get("actual_effort")),
        rerouted=rerouted, notified_final=raw.get("notified_final") is True)


def _read_events(path: Path) -> list[dict]:
    """The events of a task from disk; lines that cannot be read are left out."""
    out: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    except (ValueError, OSError):
        return out
    for line in lines:
        try:
            ev = protocol.finite(json.loads(line))
        except (ValueError, RecursionError):
            continue
        if isinstance(ev, dict):
            out.append(ev)
    return out


class TaskManager:
    def __init__(self, data_dir: Path, touched_fn: Callable[[str | None], list[str]], *,
                 check_start: Callable[[], None] | None = None) -> None:
        self.check_start = check_start
        self.dir = Path(data_dir) / "tasks"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.settings_path = Path(data_dir) / "settings.json"
        self.touched_fn = touched_fn
        self.tasks: dict[str, TaskRecord] = {}
        self._events: dict[str, list[dict]] = {}
        self._progress: dict[str, str] = {}        # the message Codex is writing now; memory only, never an event
        self._lenders: dict[str, Lender] = {}      # by invite name, in order of first connection
        self._links: dict[int, Lender] = {}        # live link id -> lender (one link per lender)
        self._last: Lender | None = None           # the latest to connect, even if gone since
        self._seen_seq: dict[str, set[int]] = {}   # per lender: each agent numbers its own messages
        self._link_ids = itertools.count(1)
        self._pending: dict[str, tuple[str, asyncio.Future]] = {}  # req_id -> (lender name, reply)
        self._req_ids = itertools.count(1)
        self._task_ids = itertools.count(1)
        self.on_change: list[Callable[[], None]] = []
        self.log: Callable[[str, str], None] = lambda level, text: None  # (level, text); set by the app
        self.invited_names: Callable[[], set[str]] | None = None
        # "log_visibility" is the default for lenders without their own value in "lender_log_visibility"
        self.settings = {"log_visibility": "summary", "lender_log_visibility": {}}
        if self.settings_path.exists():
            saved = json.loads(self.settings_path.read_text(encoding="utf-8"))
            saved.pop("thresholds", None)  # the borrower no longer sets thresholds; an old file may still hold them
            self.settings.update(saved)
        self._load()

    # ---- persistence ---------------------------------------------------
    def _load(self) -> None:
        for f in sorted(self.dir.glob("*.json")):
            try:
                rec = _record(protocol.finite(json.loads(f.read_text(encoding="utf-8"))))
            except (ValueError, RecursionError, OSError):
                rec = None
            if rec is None:
                continue  # not readable (cut off while being written, ...): left on disk, not listed
            if rec.state in FINAL_STATES:
                rec.notified_final = True
            if rec.state in ACTIVE_STATES:
                if rec.state == "awaiting_lender":  # a pending approval is still pending when the agent comes back
                    rec.state_before_offline = rec.state
                rec.state = "agent_offline"
            self.tasks[rec.task_id] = rec
            self._events[rec.task_id] = _read_events(f.with_suffix(".jsonl"))

    def _save(self, rec: TaskRecord) -> None:
        rec.finished = (rec.finished or time.time()) if rec.state in FINAL_STATES else None
        (self.dir / f"{rec.task_id}.json").write_text(json.dumps(asdict(rec), ensure_ascii=False), encoding="utf-8")
        lender = self._lenders.get(rec.lender)
        if lender is not None:
            lender.active_task_ids = self.active_task_ids(lender.name)
            lender.current_task_id = next(iter(lender.active_task_ids), None)
        self._changed()

    def mark_notified_final(self, task_id: str) -> None:
        rec = self._get(task_id)
        rec.notified_final = True
        self._save(rec)

    def _append_event(self, task_id: str, ev: dict) -> None:
        self._events.setdefault(task_id, []).append(ev)
        with open(self.dir / f"{task_id}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        self._changed()

    def _save_settings(self) -> None:
        self.settings_path.write_text(json.dumps(self.settings, ensure_ascii=False), encoding="utf-8")

    def _say(self, level: str, text: str) -> None:
        try:
            self.log(level, text)
        except Exception:
            pass

    def _changed(self) -> None:
        for cb in list(self.on_change):
            try:
                cb()
            except Exception:
                pass

    # ---- agent link ----------------------------------------------------
    def attach(self, name: str, send: SendFn) -> int:
        """A lender's agent connected. `name` is the invite name its token matched;
        a second connection under the same name replaces the first."""
        lender = self._lenders.get(name)
        if lender is None:
            lender = self._lenders[name] = Lender(name=name)
            lender.active_task_ids = self.active_task_ids(name)
            lender.current_task_id = next(iter(lender.active_task_ids), None)
        for stale in [i for i, l in self._links.items() if l is lender]:
            del self._links[stale]
        link_id = next(self._link_ids)
        self._links[link_id] = self._last = lender
        lender.send, lender.connected, lender.since = send, True, time.time()
        lender.ready = False  # until this connection's hello
        lender.busy_elsewhere = False
        self._say("warn", f"{name} とつながりました")
        self._changed()
        return link_id

    def detach(self, link_id: int) -> None:
        lender = self._links.pop(link_id, None)
        if lender is None:
            return
        lender.send, lender.connected, lender.ready = None, False, False
        self._say("warn", f"{lender.name} との接続が切れました")
        for rec in self.tasks.values():
            if rec.lender == lender.name and rec.state in ("queued", "running", "awaiting_lender"):
                rec.state_before_offline = rec.state
                rec.state = "agent_offline"
                self._save(rec)
        for owner, fut in self._pending.values():
            if owner == lender.name and not fut.done():
                fut.set_exception(ToolError("貸す人との接続が切れました"))
        self._changed()

    def _connected(self) -> list[Lender]:
        return [l for l in self._lenders.values() if l.connected and self._invited(l.name)]

    def _invited(self, name: str) -> bool:
        return self.invited_names is None or name in self.invited_names()

    def _resolve(self, name: str | None) -> Lender:
        """The named lender, or the only connected one when no name is given."""
        if name is not None:
            lender = self._lenders.get(name)
            if lender is None or not lender.connected or not self._invited(name):
                raise ToolError(f"貸す人「{name}」が接続されていません。"
                                "その人の PC で codex-bridge が起動し、「貸す」タブに招待コードを入れているか確かめてください。")
            return lender
        online = self._connected()
        if not online:
            raise ToolError(NOT_CONNECTED)
        if len(online) > 1:
            raise ToolError("複数の人が接続しています。lender に名前を指定してください: "
                            + ", ".join(l.name for l in online))
        return online[0]

    @staticmethod
    def _ready(lender: Lender) -> Lender:
        """A task only goes to a lender whose hello has arrived: before it, nothing is known
        about its sessions (disposable or not) or about the task it may still be running."""
        if not lender.ready:
            raise ToolError(NOT_READY.format(lender.name))
        return lender

    def _primary(self) -> Lender | None:
        """For the single-lender views (agent_info, top level of usage): the latest connection."""
        return self._links[max(self._links)] if self._links else self._last

    @staticmethod
    def _info(l: Lender) -> dict:
        return {"connected": l.connected, "ready": l.ready, "since": l.since, "name": l.name,
                "agent_version": l.agent_version, "codex_version": l.codex_version, "auth_mode": l.auth_mode,
                "protocol": l.protocol, "capabilities": dict(l.capabilities),
                "models": l.models, "max_effort": l.max_effort, "max_parallel": l.max_parallel}

    def agent_info(self, lender: str | None = None) -> dict:
        """Without a name: the latest connection (the single-lender view). With a name: that lender
        and nobody else, also when it has not connected since the host started."""
        if lender is None:
            l = self._primary()
            return self._info(l) if l else {"connected": False}
        l = self._lenders.get(lender)
        return self._info(l) if l else {"connected": False, "name": lender}

    def lenders(self, invited: Iterable[str] = ()) -> list[dict]:
        """One row per lender seen since the host started, then one per invited name that has not
        connected since: not connected, no usage, and what it can do is not known (capabilities None)."""
        def row(l: Lender) -> dict:
            return {**self._info(l), "limits": l.limits, "lender_view": l.view, "usage": self._usage_of(l),
                    "active_task_ids": list(l.active_task_ids), "current_task_id": l.current_task_id, "last_assigned": l.last_assigned,
                    "busy_elsewhere": l.busy_elsewhere}
        never = [{**row(Lender(name=n, active_task_ids=self.active_task_ids(n), current_task_id=self.current_task_id(n))), "capabilities": None}
                 for n in dict.fromkeys(invited) if n not in self._lenders]
        return [row(l) for l in self._lenders.values()] + never

    def _own_task(self, lender: Lender, task_id: str) -> TaskRecord | None:
        """The task, unless it belongs to another lender (a v0.1 record has no lender yet)."""
        rec = self.tasks.get(task_id)
        return rec if rec is not None and rec.lender in (None, lender.name) else None

    async def on_agent_message(self, link_id: int, m: dict) -> None:
        lender = self._links.get(link_id)
        if lender is None:
            return
        t = m["type"]
        if "seq" in m:
            await lender.send(protocol.msg("ack", seq=m["seq"]))
            seen = self._seen_seq.setdefault(lender.name, set())
            if m["seq"] in seen:
                return
            seen.add(m["seq"])
        if t == "hello":
            # everything from the other PC is checked here: it is stored, computed with and shown as JSON
            lender.agent_version, lender.codex_version, lender.auth_mode = (
                as_text(m.get(k)) for k in ("agent_version", "codex_version", "auth_mode"))
            lender.protocol = as_protocol(m.get("protocol"))
            lender.capabilities = normalize_capabilities(m.get("capabilities"))
            lender.max_parallel = lender.capabilities["max_parallel"] if lender.protocol >= 5 else 1
            lender.capabilities["max_parallel"] = lender.max_parallel
            lender.models = normalize_models(m.get("models"))
            lender.max_effort = normalize_effort(m.get("max_effort"))
            lender.view = normalize_lender_view(m.get("lender_view"))
            if lender.view is not None:
                lender.view["max_parallel"] = lender.max_parallel
            limits = normalize_limits(m.get("limits"))
            if limits is not None:
                lender.limits = limits
            if lender.protocol >= 5 and isinstance(m.get("active_task_ids"), list):
                self._reconcile(lender, [v for v in m["active_task_ids"] if isinstance(v, str)])
            elif "current_task_id" in m:
                self._reconcile(lender, [m["current_task_id"]] if isinstance(m["current_task_id"], str) else [])
            lender.ready = True
            await lender.send(self._settings_msg(lender.name))
            self._changed()
        elif t == "lender_models":
            if lender.protocol >= 5 and isinstance(m.get("capabilities"), dict):
                lender.capabilities = normalize_capabilities(m["capabilities"])
                lender.max_parallel = lender.capabilities["max_parallel"]
            lender.models = normalize_models(m.get("models"))
            lender.max_effort = normalize_effort(m.get("max_effort"))
            lender.view = normalize_lender_view(m.get("lender_view"))
            self._changed()
        elif t == "lender_status":
            if isinstance(m.get("busy_elsewhere"), bool):
                lender.busy_elsewhere = m["busy_elsewhere"]
                self._changed()
        elif t == "usage":
            # numbers from the other PC are checked here, so that nothing computed from them later can fail
            limits = normalize_limits(m.get("friend_limits"))
            lender.usage = {"five_hour": normalize_window(m.get("five_hour")),
                            "weekly": normalize_window(m.get("weekly")), "ts": number(m.get("ts")),
                            "friend_limits": limits}
            if limits is not None:
                lender.limits = limits
            self._changed()
        elif t == "event":
            # kept and shown as it comes, so: a kind in words, and no number in the data that JSON cannot carry
            task_id, kind = as_text(m.get("task_id")), as_text(m.get("kind"))
            if kind and (rec := self._own_task(lender, task_id)):
                if kind == "progress":  # the half-written message: shown under "working", not part of the record
                    data = m.get("data")
                    text = data.get("text") if isinstance(data, dict) else None
                    if isinstance(text, str) and text:
                        self._progress[task_id] = text[:PROGRESS_MAX]
                        self._changed()
                    return
                if kind == "agent_message":
                    self._progress.pop(task_id, None)  # it is written now
                if kind == "model":
                    data = m.get("data")
                    if isinstance(data, dict):
                        rec.actual_model = as_text(data.get("model")) or rec.actual_model
                        rec.actual_effort = as_text(data.get("effort")) or rec.actual_effort
                        if rerouted_from := as_text(data.get("rerouted_from")):
                            rec.rerouted = {"from": rerouted_from, "reason": as_text(data.get("reason"))}
                        self._save(rec)
                self._append_event(task_id, {"ts": time.time(), "kind": kind, "data": protocol.finite(m.get("data"))})
        elif t == "task_state":
            rec = self._own_task(lender, as_text(m.get("task_id")))
            state = _known_state(m.get("state"))
            if rec is None or state is None:  # a state this host does not know is not a state: nothing changes
                return
            if rec.state in FINAL_STATES and state != rec.state and rec.report != SPOILT and not (
                    rec.guessed and state in FINAL_STATES):
                return  # over is over (a message sent again after a reconnect must not bring a task back);
                #         a record that was spoilt only says "failed" for want of knowing: the agent's word counts,
                #         and so does its final word on a task the host only guessed was lost (v0.1 says hello first)
            if rec.guessed and state in FINAL_STATES:
                rec.report = None  # the guess is replaced, not added to
            rec.guessed = False
            rec.lender = lender.name
            rec.state = state
            rec.state_before_offline = None
            if state in FINAL_STATES:
                self._progress.pop(rec.task_id, None)
            rec.thread_id = as_text(m.get("thread_id")) or rec.thread_id
            report, resume_at = as_text(m.get("report")), moment(m.get("resume_at"))
            if report is not None:
                rec.report = report
            if resume_at is not None:
                rec.resume_at = resume_at
            self._save(rec)
            if state in FINAL_STATES:
                self._say("error" if state == "failed" else "info", f"{rec.task_id} が終わりました（{state}）")
        elif t == "reply":
            owner, fut = self._pending.get(m.get("req_id"), (None, None))
            if owner != lender.name:
                return
            del self._pending[m["req_id"]]
            if not fut.done():
                if m.get("ok"):
                    fut.set_result(m.get("data"))
                else:
                    fut.set_exception(ToolError(m.get("error") or "貸す人の PC でエラーが起きました"))

    def _reconcile(self, lender: Lender, agent_task_ids: list[str]) -> None:
        """After (re)connect: keep the tasks this lender's agent still runs, fail the ones it lost
        (e.g. it restarted). Other lenders' tasks are left alone."""
        for rec in self.tasks.values():
            if rec.state not in ACTIVE_STATES or rec.lender not in (None, lender.name):
                continue
            rec.lender = lender.name
            if rec.task_id in agent_task_ids:
                if rec.state == "agent_offline":  # (else the agent already said where the task stands)
                    rec.state = rec.state_before_offline or "running"
            else:
                rec.state = "failed"
                rec.guessed = True  # until the agent says how the task really ended (see task_state)
                rec.report = rec.report or ("貸す人の codex-bridge が起動し直したため、この依頼は中断されました。"
                                             "続けるなら、この依頼の続きとして送ってください。")
            rec.state_before_offline = None
            self._save(rec)

    def log_visibility_for(self, name: str) -> str:
        return self.settings["lender_log_visibility"].get(name, self.settings["log_visibility"])

    def drop_lender_settings(self, name: str) -> None:
        if name in self.settings["lender_log_visibility"]:
            del self.settings["lender_log_visibility"][name]
            self._save_settings()
        self._changed()

    def _settings_msg(self, lender: str) -> dict:
        # thresholds stay in the message (empty) so that a v0.2 agent, which reads the key, keeps working
        return protocol.msg("settings", thresholds={}, log_visibility=self.log_visibility_for(lender))

    async def _send_settings(self, lender: Lender) -> None:
        if not lender.connected or lender.send is None:
            return
        try:
            await lender.send(self._settings_msg(lender.name))
        except Exception:
            pass  # a broken link must not fail the change; hello sends the settings again

    # ---- usage ---------------------------------------------------------
    def _usage_of(self, lender: Lender | None) -> dict:
        out = dict(lender.usage) if lender else {"five_hour": None, "weekly": None, "ts": None}
        for k in ("five_hour", "weekly"):
            if out.get(k):
                out[k] = {**out[k], "resets_at_local": iso_local(out[k].get("resets_at"))}
        out["blocked_until"] = iso_local(blocked_until(lender, NO_THRESHOLDS)) if lender else None
        return out

    def usage(self) -> dict:
        """Top level: the latest connection (the v0.1 shape). "lenders": the same for each lender by name."""
        out = self._usage_of(self._primary())
        out["lenders"] = {l.name: self._usage_of(l) for l in self._lenders.values()}
        return out

    async def set_log_visibility(self, value: str, lender: str) -> None:
        if value not in LOG_VISIBILITY:
            raise ToolError(f"log_visibility は {LOG_VISIBILITY} のどれかです")
        self.settings["lender_log_visibility"][lender] = value
        self._save_settings()
        if (l := self._lenders.get(lender)) is not None:
            await self._send_settings(l)
        self._changed()

    # ---- tasks ---------------------------------------------------------
    def active_task_ids(self, lender: str | None = None) -> list[str]:
        return [r.task_id for r in self.tasks.values()
                if r.state in ACTIVE_STATES and (lender is None or r.lender == lender)]

    def current_task_id(self, lender: str | None = None) -> str | None:
        """Compatibility view: the first active task."""
        return next(iter(self.active_task_ids(lender)), None)

    def task_for(self, lender: str, thread_id: str | None) -> str | None:
        """Match only this lender's active tasks; the single-task fallback supports old Codex versions."""
        active = [self.tasks[t] for t in self.active_task_ids(lender)]
        matched = [r for r in active if thread_id and r.thread_id == thread_id]
        if len(matched) == 1:
            return matched[0].task_id
        if thread_id and any(r.lender == lender and r.thread_id == thread_id and r.state not in ACTIVE_STATES
                             for r in self.tasks.values()):
            return None
        return (active[0].task_id if len(active) == 1 and
                (not thread_id or not active[0].thread_id) else None)

    def is_active(self, task_id: str) -> bool:
        rec = self.tasks.get(task_id)
        return rec is not None and rec.state in ACTIVE_STATES

    def _get(self, task_id: str) -> TaskRecord:
        rec = self.tasks.get(task_id)
        if rec is None:
            raise ToolError(f"タスクが見つかりません: {task_id}")
        return rec

    def _require_free(self, lender: Lender, now: float, advice: str = "") -> None:
        if lender.busy_elsewhere or len(lender.active_task_ids) >= lender.max_parallel:
            raise ToolError(BUSY.format(lender.max_parallel) + advice)
        blocked = blocked_until(lender, NO_THRESHOLDS, now)
        if blocked:
            raise ToolError(QUOTA.format(iso_local(blocked)))

    def _pick(self, now: float, among: list[Lender] | None = None) -> Lender:
        """Automatic routing: fewest active tasks, then most usage headroom."""
        online = among if among is not None else self._connected()
        lender = pick_lender(online, NO_THRESHOLDS, now)
        if lender is not None:
            return lender
        # nobody is free: everyone idle is over a limit (say when the first comes back), all are working,
        # or the only ones left have just connected and not said hello yet
        ready = [l for l in online if l.ready]
        waits = [t for l in ready
                 if len(l.active_task_ids) < l.max_parallel and not l.busy_elsewhere
                 and (t := blocked_until(l, NO_THRESHOLDS, now))]
        if waits:
            raise ToolError(QUOTA.format(iso_local(min(waits))))
        busy = [l for l in ready if len(l.active_task_ids) >= l.max_parallel or l.busy_elsewhere]
        if busy:
            raise ToolError("、".join(f"「{l.name}」: {BUSY.format(l.max_parallel)}" for l in busy))
        raise ToolError(NOT_READY.format("、".join(l.name for l in online)))

    def _continue_target(self, prev: TaskRecord, lender: str | None, now: float, *,
                         model: str | None = None, effort: str | None = None) -> Lender:
        """The session lives in one lender's Codex, so a continuation can only go back there."""
        if prev.lender is None:  # a v0.1 record: there was only one lender then
            target = self._resolve(lender)
        else:
            if lender is not None and lender != prev.lender:
                raise ToolError(f"このタスクの会話は「{prev.lender}」の Codex にあるため、"
                                f"「{lender}」には続きを送れません。{RETRY_FRESH}")
            target = self._lenders.get(prev.lender)
            if target is None or not target.connected or not self._invited(prev.lender):
                raise ToolError(f"この依頼の会話を持つ貸す人「{prev.lender}」が接続されていません。{RETRY_FRESH}")
        self._ready(target)
        reason = lender_accepts(target, model, effort)
        if reason:
            raise ToolError(reason)
        if not target.capabilities.get("resumable", True):
            raise ToolError(f"「{target.name}」は使い捨てのセッションで動いているため、会話が残っていません。{RETRY_FRESH}")
        if not prev.thread_id:
            raise ToolError(f"このタスクには続けられるセッションがありません: {prev.task_id}")
        if any(r.state in ACTIVE_STATES and r.lender == target.name and
               (r.task_id == prev.task_id or r.thread_id == prev.thread_id) for r in self.tasks.values()):
            raise ToolError("この会話は作業中です。追加の指示を送るか、終わるのを待ってください。")
        self._require_free(target, now, advice=RETRY_FRESH)
        return target

    async def start(self, prompt: str, continue_task_id: str | None = None, lender: str | None = None, *,
                    model: str | None = None, effort: str | None = None,
                    origin: str | None = None, session: str | None = None) -> str:
        if self.check_start:
            self.check_start()
        if not self._connected():
            raise ToolError(NOT_CONNECTED)
        now = time.time()
        thread_id = None
        if continue_task_id:
            prev = self._get(continue_task_id)
            if model is None and effort is None:
                model, effort = prev.model, prev.effort
            target = self._continue_target(prev, lender, now, model=model, effort=effort)
            thread_id = prev.thread_id
        elif lender is not None:
            target = self._ready(self._resolve(lender))
            reason = lender_accepts(target, model, effort)
            if reason:
                raise ToolError(reason)
            self._require_free(target, now)
        elif model is not None or effort is not None:
            online = self._connected()
            among = [l for l in online if lender_accepts(l, model, effort) is None]
            if not among:
                reasons = [lender_accepts(l, model, effort) for l in online]
                raise ToolError("指定のモデル（" + (model or "既定") + "）と推論の強さ（" + (effort or "既定")
                                + "）を使わせている人が、接続中の中にいません。" + " ".join(reasons))
            target = self._pick(now, among)
        else:
            target = self._pick(now)
        task_id = time.strftime("t%Y%m%d-%H%M%S") + f"-{next(self._task_ids)}"
        rec = TaskRecord(task_id=task_id, prompt=prompt, created=now, continue_task_id=continue_task_id,
                         lender=target.name, origin=origin, session=session, model=model, effort=effort, thread_id=thread_id)
        self.tasks[task_id] = rec
        self._events[task_id] = []
        target.last_assigned = now
        self._save(rec)  # also marks the lender as working
        spec = {k: v for k, v in (("model", model), ("effort", effort)) if v is not None}
        await target.send(protocol.msg("task_start", task_id=task_id, prompt=prompt, continue_thread_id=thread_id, **spec))
        self._say("info", f"{task_id} を {target.name} に送りました")
        return task_id

    async def _send_for(self, rec: TaskRecord, m: dict) -> None:
        await self._resolve(rec.lender).send(m)

    async def followup(self, task_id: str, text: str) -> None:
        rec = self._get(task_id)
        if rec.state not in ACTIVE_STATES:
            raise ToolError(f"この依頼は作業中ではありません（{rec.state}）。続けるなら、この依頼の続きとして送ってください")
        if rec.state == "awaiting_lender":
            raise ToolError(AWAITING.format(task_id))
        await self._send_for(rec, protocol.msg("task_followup", task_id=task_id, text=text))

    async def cancel(self, task_id: str) -> None:
        rec = self._get(task_id)
        if rec.state not in ACTIVE_STATES:
            raise ToolError(f"このタスクは作業中ではありません（{rec.state}）")
        await self._send_for(rec, protocol.msg("task_cancel", task_id=task_id))

    def events(self, task_id: str) -> list[dict]:
        return list(self._events.get(task_id, []))

    def progress(self, task_id: str) -> str | None:
        """The message Codex is writing now (None when it is not writing one)."""
        return self._progress.get(task_id)

    def status(self, task_id: str | None = None) -> dict:
        if task_id is None:
            task_id = self.current_task_id() or (max(self.tasks, key=lambda k: self.tasks[k].created) if self.tasks else None)
            if task_id is None:
                return {"state": "idle", "agent": self.agent_info()}
        rec = self._get(task_id)
        return {
            "task_id": rec.task_id, "state": rec.state, "lender": rec.lender, "thread_id": rec.thread_id,
            "origin": rec.origin, "model": rec.model, "effort": rec.effort,
            "actual_model": rec.actual_model, "actual_effort": rec.actual_effort, "rerouted": rec.rerouted,
            "resume_at": iso_local(rec.resume_at), "recent_events": self.events(task_id)[-15:],
            # the agent of this task's own lender; None for a v0.1 record, which names no lender
            "agent": self.agent_info(rec.lender) if rec.lender is not None else None,
        }

    def result(self, task_id: str) -> dict:
        rec = self._get(task_id)
        return {
            "task_id": rec.task_id, "state": rec.state, "lender": rec.lender, "prompt": rec.prompt,
            "origin": rec.origin, "model": rec.model, "effort": rec.effort,
            "actual_model": rec.actual_model, "actual_effort": rec.actual_effort, "rerouted": rec.rerouted,
            "report": rec.report, "thread_id": rec.thread_id, "resume_at": iso_local(rec.resume_at),
            "created": rec.created, "finished": rec.finished,
            "changed_files": self.touched_fn(task_id), "event_count": len(self._events.get(task_id, [])),
        }

    def list_tasks(self) -> list[dict]:
        return [{"task_id": r.task_id, "state": r.state, "lender": r.lender, "prompt": r.prompt[:200],
                 "origin": r.origin, "model": r.model, "effort": r.effort,
                 "actual_model": r.actual_model, "actual_effort": r.actual_effort, "rerouted": r.rerouted,
                 "created": r.created}
                for r in sorted(self.tasks.values(), key=lambda r: r.created, reverse=True)]

    # ---- sessions (round trips to one lender's agent) ------------------
    async def _request(self, lender: str | None, type_: str, **fields):
        target = self._resolve(lender)
        req_id = f"r{next(self._req_ids)}"
        fut = asyncio.get_running_loop().create_future()
        self._pending[req_id] = (target.name, fut)
        try:
            await target.send(protocol.msg(type_, req_id=req_id, **fields))
            return await asyncio.wait_for(fut, REQUEST_TIMEOUT)
        except asyncio.TimeoutError:
            raise ToolError("貸す人の PC から返事がありませんでした") from None
        finally:
            self._pending.pop(req_id, None)

    async def sessions_list(self, lender: str | None = None):
        return await self._request(lender, "session_list")

    async def session_read(self, thread_id: str, lender: str | None = None):
        return await self._request(lender, "session_read", thread_id=thread_id)

    async def session_delete(self, thread_id: str, lender: str | None = None):
        return await self._request(lender, "session_delete", thread_id=thread_id)
