"""Pre-approval by the lender (design 4.5): a new task and a follow-up wait for a yes before Codex sees them.

One request per task may wait at a time. The screen reads `AgentRunner.pending_approvals` and answers with
`AgentRunner.decide_pending`; every decision is applied to its own task.

What the borrower is told: `awaiting_lender` while it waits. A refused new task is `rejected_by_lender` (final,
the reason in `report`). A refused follow-up cannot end the task Codex is working on, so the task goes on:
`running` again, and a `status` event with the reason.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .runner import AgentRunner

START, FOLLOWUP = "start", "followup"
REFUSED = "Codex を貸している人が拒否しました"
TIMED_OUT = "Codex を貸している人が {} 分のあいだ承認しなかったため、拒否しました"
EXITED = "Codex を貸している人の PC のアプリが終了したため、拒否しました"
CANCELLED = "承認される前に、依頼主が取り消しました"
WAITING = "承認待ちの指示があるため、この追加の指示は受け付けませんでした。承認か拒否のあとにもう一度送ってください"


@dataclass
class Pending:
    msg: dict        # the host's message, kept as it came
    kind: str        # START or FOLLOWUP
    created: float   # wall-clock time, for the screen
    deadline: float | None  # on the runner's clock; None means no expiry
    timeout_min: int | None  # minutes when held, independent of later settings changes

    @property
    def task_id(self) -> str:
        return self.msg["task_id"]

    @property
    def prompt(self) -> str:
        return self.msg["prompt"] if self.kind == START else self.msg["text"]

    def view(self) -> dict:
        return {"task_id": self.task_id, "prompt": self.prompt, "kind": self.kind, "created": self.created,
                "timeout_min": self.timeout_min, "model": self.msg.get("model"), "effort": self.msg.get("effort")}


def _why(base: str, reason: str) -> str:
    reason = (reason or "").strip()
    return f"{base}。理由: {reason}" if reason else base


class PreApproval:
    def __init__(self, runner: AgentRunner) -> None:
        self.runner = runner
        self.pendings: dict[str, Pending] = {}

    @property
    def pending(self) -> Pending | None:
        """Compatibility view for a single pending decision."""
        return next(iter(self.pendings.values()), None)

    @pending.setter
    def pending(self, pending: Pending | None) -> None:
        self.pendings = {pending.task_id: pending} if pending else {}

    def applies(self, kind: str) -> bool:
        """Whether the lender wants to say yes first to this kind of request (START or FOLLOWUP)."""
        return bool(self.runner.settings["pre_approval"]["new_task" if kind == START else "followup"])

    def holds(self, task_id: str) -> bool:
        return task_id in self.pendings

    async def hold(self, msg: dict, kind: str) -> None:
        r = self.runner
        minutes = r.settings["pre_approval"]["timeout_min"]
        deadline = None if minutes is None else r.clock() + minutes * 60
        p = Pending(msg, kind, time.time(), deadline, timeout_min=minutes)
        self.pendings[p.task_id] = p
        r.log_prompt(p.task_id, p.prompt)  # the lender can read it in the log from now on
        await self._announce(p)

    async def _announce(self, p: Pending) -> None:
        cur = self.runner.currents.get(p.task_id)
        await self.runner._state("awaiting_lender", task_id=p.task_id,
                                 thread_id=cur.thread_id if p.kind == FOLLOWUP and cur else None)

    async def resend(self) -> None:
        """After a reconnect and its hello: say again that a request waits (a number of its own, so that the host
        takes it even if it had already seen the first), so that the host never loses the task."""
        async with self.runner._lock:
            for p in list(self.pendings.values()):
                if self.pendings.get(p.task_id) is p:
                    await self._announce(p)

    def drop(self, task_id: str) -> Pending | None:
        """The request goes without an answer (the task ended or was cancelled); returns it."""
        return self.pendings.pop(task_id, None)

    async def decide(self, approve: bool, reason: str = "", task_id: str | None = None,
                     created: float | None = None) -> bool:
        async with self.runner._lock:
            if task_id is None:
                task_id = next(iter(self.pendings), None) if len(self.pendings) == 1 else None
            p = self.pendings.get(task_id)
            if p is None or (created is not None and created != p.created):
                return False
            self.pendings.pop(task_id)
            if approve:
                self.runner._record(p.task_id, "lender_approved", {"kind": p.kind})
                await self._approve(p)
            else:
                await self._reject(p, _why(REFUSED, reason))
            return True

    async def expire(self) -> None:
        async with self.runner._lock:
            for p in list(self.pendings.values()):
                if (self.pendings.get(p.task_id) is p and p.deadline is not None
                        and self.runner.clock() >= p.deadline):
                    self.pendings.pop(p.task_id)
                    await self._reject(p, TIMED_OUT.format(p.timeout_min))

    async def shutdown(self) -> None:
        async with self.runner._lock:
            for p in list(self.pendings.values()):
                if self.pendings.get(p.task_id) is p:
                    self.pendings.pop(p.task_id)
                    await self._reject(p, EXITED)

    async def _approve(self, p: Pending) -> None:
        r = self.runner
        if p.kind == START:
            await r._start_locked(p.msg, shown=True)
            return
        cur = r.currents.get(p.task_id)
        await r._deliver_followup(p.msg, shown=True)
        if cur is not None and r.currents.get(p.task_id) is cur:  # still working (not failed on the way): the borrower sees it again
            await r._state("running", cur=cur)

    async def _reject(self, p: Pending, report: str) -> None:
        r = self.runner
        if p.kind == START:
            await r._state("rejected_by_lender", report=report, task_id=p.task_id)
            await r._release_slot()
        elif (cur := r.currents.get(p.task_id)) is not None:
            await r._event("status", cur=cur, text=f"追加の指示は渡しませんでした（{report}）")
            await r._state("running", cur=cur)
