"""Pre-approval by the lender (design 4.5): a new task and a follow-up wait for a yes before Codex sees them.

One request waits at a time (the host gives a lender one task at a time, and refuses a follow-up to a task that
waits). `PreApproval` holds it and puts the lender's answer to work through the runner it belongs to; the screen
(Task 11) reads `AgentRunner.pending_approval` and answers with `AgentRunner.decide_pending`.

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
        return {"task_id": self.task_id, "prompt": self.prompt, "kind": self.kind, "created": self.created}


def _why(base: str, reason: str) -> str:
    reason = (reason or "").strip()
    return f"{base}。理由: {reason}" if reason else base


class PreApproval:
    def __init__(self, runner: AgentRunner) -> None:
        self.runner = runner
        self.pending: Pending | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.runner.settings["pre_approval"]["enabled"])

    def holds(self, task_id: str) -> bool:
        return self.pending is not None and self.pending.task_id == task_id

    async def hold(self, msg: dict, kind: str) -> None:
        r = self.runner
        minutes = r.settings["pre_approval"]["timeout_min"]
        deadline = None if minutes is None else r.clock() + minutes * 60
        self.pending = p = Pending(msg, kind, time.time(), deadline, timeout_min=minutes)
        r.log_prompt(p.task_id, p.prompt)  # the lender can read it in the log from now on
        await self._announce(p)

    async def _announce(self, p: Pending) -> None:
        cur = self.runner.cur
        await self.runner._state("awaiting_lender", task_id=p.task_id,
                                 thread_id=cur.thread_id if p.kind == FOLLOWUP and cur else None)

    async def resend(self) -> None:
        """After a reconnect and its hello: say again that a request waits (a number of its own, so that the host
        takes it even if it had already seen the first), so that the host never loses the task."""
        if self.pending is not None:
            await self._announce(self.pending)

    def drop(self, task_id: str) -> Pending | None:
        """The request goes without an answer (the task ended or was cancelled); returns it."""
        if self.holds(task_id):
            p, self.pending = self.pending, None
            return p
        return None

    async def decide(self, approve: bool, reason: str = "", task_id: str | None = None) -> bool:
        if task_id is not None and (self.pending is None or self.pending.task_id != task_id):
            return False
        p, self.pending = self.pending, None  # first: nothing below may be decided twice
        if p is None:
            return False
        if approve:
            await self._approve(p)
        else:
            await self._reject(p, _why(REFUSED, reason))
        return True

    async def expire(self) -> None:
        p = self.pending
        if p is not None and p.deadline is not None and self.runner.clock() >= p.deadline:
            self.pending = None
            await self._reject(p, TIMED_OUT.format(p.timeout_min))

    async def shutdown(self) -> None:
        p, self.pending = self.pending, None
        if p is not None:
            await self._reject(p, EXITED)

    async def _approve(self, p: Pending) -> None:
        r = self.runner
        if p.kind == START:
            async with r._lock:
                await r._start_locked(p.msg, shown=True)
            return
        cur = r.cur
        await r._deliver_followup(p.msg, shown=True)
        if cur is not None and r.cur is cur:  # still working (not failed on the way): the borrower sees it again
            await r._state("running")

    async def _reject(self, p: Pending, report: str) -> None:
        r = self.runner
        if p.kind == START:
            await r._state("rejected_by_lender", report=report, task_id=p.task_id)
        elif r.cur is not None and r.cur.task_id == p.task_id:
            await r._event("status", text=f"追加の指示は渡しませんでした（{report}）")
            await r._state("running")
