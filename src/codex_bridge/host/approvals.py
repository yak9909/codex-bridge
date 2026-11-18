"""Pending approvals decided by a person in the admin web UI."""

from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import dataclass, field
from typing import Callable, Literal

from .policy import PolicyStore

Status = Literal["pending", "approved", "denied", "expired"]


@dataclass
class Approval:
    id: str
    kind: str
    summary: str
    task_id: str | None
    created_at: float
    key: str | None = None
    cwd: str | None = None
    decided: Status = "pending"
    consumed: bool = False
    _timeout: float = 1800.0
    _event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    @property
    def status(self) -> Status:
        if self.decided == "pending" and time.monotonic() - self.created_at >= self._timeout:
            return "expired"
        return self.decided

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "summary": self.summary, "task_id": self.task_id,
                "cwd": self.cwd, "status": self.status, "age_sec": int(time.monotonic() - self.created_at)}


class Approvals:
    def __init__(self, policy: PolicyStore) -> None:
        self.policy = policy
        self._items: dict[str, Approval] = {}
        self._ids = itertools.count(1)
        self.on_change: list[Callable[[], None]] = []

    def _changed(self) -> None:
        for cb in list(self.on_change):
            try:
                cb()
            except Exception:
                pass

    def request(self, kind: str, summary: str, task_id: str | None, key: str | None = None,
                cwd: str | None = None) -> Approval:
        if key is not None:
            for a in self._items.values():
                if a.key == key and not a.consumed and a.status != "expired":
                    return a
        a = Approval(id=f"a{next(self._ids)}", kind=kind, summary=summary, task_id=task_id,
                     created_at=time.monotonic(), key=key, cwd=cwd,
                     _timeout=self.policy.current().limits.approval_timeout_sec)
        self._items[a.id] = a
        self._changed()
        return a

    def get(self, approval_id: str) -> Approval:
        return self._items[approval_id]

    def decide(self, approval_id: str, approve: bool) -> None:
        a = self._items.get(approval_id)
        if a is None or a.status != "pending":
            raise KeyError(approval_id)
        a.decided = "approved" if approve else "denied"
        a._event.set()
        self._changed()

    def consume(self, approval_id: str) -> None:
        self._items[approval_id].consumed = True

    async def wait(self, approval_id: str, timeout: float) -> Status:
        a = self._items[approval_id]
        remaining = a._timeout - (time.monotonic() - a.created_at)
        try:
            await asyncio.wait_for(a._event.wait(), timeout=max(0.0, min(timeout, remaining)))
        except asyncio.TimeoutError:
            pass
        if a.status == "expired":
            self._changed()
        return a.status

    def pending(self) -> list[Approval]:
        return [a for a in self._items.values() if a.status == "pending"]


def make_approve_fn(approvals: Approvals, task_id_fn: Callable[[], str | None], wait_sec: float):
    """approve_fn for WriteTools: same action re-uses the same request until it is used."""

    async def approve(kind: str, summary: str) -> str:
        task_id = task_id_fn()
        a = approvals.request(kind, summary, task_id, key=f"{kind}|{task_id}|{summary}")
        status = await approvals.wait(a.id, timeout=wait_sec)
        if status == "pending":
            return "pending"
        approvals.consume(a.id)
        return "approved" if status == "approved" else "denied"

    return approve
