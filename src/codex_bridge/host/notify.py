"""Event-driven approval and final notices for calling agents' long waits."""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

from ..common.protocol import FINAL_STATES
from .approvals import Approvals
from .tasks import TaskManager, TaskRecord, iso_local

WAIT_DEFAULT = 3300
WAIT_MAX = 3600
CODEX_MAX_AGE = 86400
REPORT_MAX = 2000
DISCONNECT_CHECK = 15.0


class Notifier:
    def __init__(self, mgr: TaskManager, approvals: Approvals,
                 clock: Callable[[], float] = time.time):
        self.mgr = mgr
        self.approvals = approvals
        self.clock = clock
        self._event = asyncio.Event()
        self._closed = False
        self._waiting: dict[str, int] = {}
        self._notified_approvals: set[str] = set()
        mgr.on_change.append(self.changed)
        approvals.on_change.append(self.changed)

    def changed(self) -> None:
        self._event.set()
        self._event = asyncio.Event()

    def close(self) -> None:
        self._closed = True
        self.changed()

    def waiting(self, task_id: str) -> int:
        return self._waiting.get(task_id, 0)

    def _codex_targets(self, session: str | None) -> list[str]:
        pending = {a.task_id for a in self.approvals.pending() if a.id not in self._notified_approvals}
        now = self.clock()
        return [r.task_id for r in self.mgr.tasks.values()
                if r.origin == "codex" and now - r.created <= CODEX_MAX_AGE
                and (session is None or r.session == session)
                and (r.state in ("queued", "running") or r.task_id in pending
                     or (r.state in FINAL_STATES and not r.notified_final))]

    @staticmethod
    def _identity(rec: TaskRecord) -> dict:
        return {"task_id": rec.task_id, "lender": rec.lender,
                "model": rec.actual_model or rec.model, "effort": rec.actual_effort or rec.effort}

    def _take(self, targets: list[str]) -> list[dict]:
        # No await: concurrent waiters cannot take the same notice.
        notices = []
        pending = self.approvals.pending()
        for task_id in targets:
            rec = self.mgr.tasks.get(task_id)
            if rec is None:
                continue
            for a in pending:
                if a.task_id == task_id and a.id not in self._notified_approvals:
                    notices.append({"kind": "approval", **self._identity(rec),
                                    "approval": {"id": a.id, "kind": a.kind, "summary": a.summary, "cwd": a.cwd}})
                    self._notified_approvals.add(a.id)
            if rec.state in FINAL_STATES and not rec.notified_final:
                notices.append({"kind": "final", **self._identity(rec), "state": rec.state,
                                "report": rec.report[:REPORT_MAX] if rec.report is not None else None,
                                "report_truncated": rec.report is not None and len(rec.report) > REPORT_MAX,
                                "resume_at": iso_local(rec.resume_at)})
                self.mgr.mark_notified_final(task_id)
        return notices

    async def wait(self, *, task_id: str | None = None, origin: str | None = None,
                   any_unwatched: bool = False, session: str | None = None,
                   timeout: float = WAIT_DEFAULT,
                   disconnected: Callable[[], Awaitable[bool]] | None = None) -> dict:
        if self._closed:
            return {"status": "closing", "notices": []}
        codex = task_id is None and origin == "codex"
        if task_id is not None:
            targets = [task_id] if task_id in self.mgr.tasks else []
        elif origin == "claude" and any_unwatched:
            targets = [r.task_id for r in self.mgr.tasks.values()
                       if r.origin in ("claude", None) and not self.waiting(r.task_id)
                       and (r.state not in FINAL_STATES or not r.notified_final)]
        else:
            targets = []
        counted = list(targets)
        for target in counted:
            self._waiting[target] = self.waiting(target) + 1
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        event = None
        try:
            while True:
                if self._closed:
                    return {"status": "closing", "notices": []}
                if disconnected is not None:
                    gone = await disconnected()
                    if self._closed:
                        return {"status": "closing", "notices": []}
                    if gone:
                        return {"status": "gone", "notices": []}
                # Timer ticks only check disconnects; task selection and taking run on changes.
                if event is None or event.is_set():
                    # Capture before taking so a change during the take cannot be lost.
                    event = self._event
                    if codex:
                        targets = self._codex_targets(session)
                    if not targets:
                        return {"status": "none", "notices": []}
                    notices = self._take(targets)
                    if notices:
                        return {"status": "notice", "notices": notices}
                    if task_id is not None:
                        rec = self.mgr.tasks[task_id]
                        if rec.state in FINAL_STATES and rec.notified_final:
                            return {"status": "none", "notices": []}
                    if codex and not self._codex_targets(session):
                        return {"status": "none", "notices": []}
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return {"status": "timeout", "notices": []}
                try:
                    await asyncio.wait_for(event.wait(), min(remaining, DISCONNECT_CHECK))
                except asyncio.TimeoutError:
                    pass
                if loop.time() >= deadline:
                    return {"status": "closing" if self._closed else "timeout", "notices": []}
        finally:
            for target in counted:
                remaining_waiters = self.waiting(target) - 1
                if remaining_waiters:
                    self._waiting[target] = remaining_waiters
                else:
                    self._waiting.pop(target, None)
