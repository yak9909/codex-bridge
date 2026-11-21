"""Usage windows and the threshold guard that tells Codex to stop now and report."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

STOP_TEXT = ("使用量の上限に達したため、今から中断処理に入ります。今の作業はここで止めてください。"
             "新しい手順やファイルの変更には進まず、終えたこと・残っていること・次にやるべきことだけを短く報告して、"
             "すぐに終了してください。")
FIVE_HOUR_MINS = 300
WEEKLY_MINS = 10080


@dataclass(frozen=True)
class Window:
    used_percent: float
    resets_at: int


@dataclass(frozen=True)
class UsageSnapshot:
    five_hour: Window | None
    weekly: Window | None

    def to_message(self) -> dict:
        w = lambda x: None if x is None else {"used_percent": x.used_percent, "resets_at": x.resets_at}
        return {"five_hour": w(self.five_hour), "weekly": w(self.weekly), "ts": int(time.time())}


def snapshot_from_rate_limits(resp: dict) -> UsageSnapshot:
    """Map primary/secondary to 5-hour/weekly by window length, never by position."""
    bucket = (resp.get("rateLimitsByLimitId") or {}).get("codex") or resp.get("rateLimits") or {}
    five = weekly = None
    for key in ("primary", "secondary"):
        w = bucket.get(key)
        if not w:
            continue
        win = Window(float(w.get("usedPercent", 0)), int(w.get("resetsAt") or 0))
        mins = w.get("windowDurationMins") or 0
        if abs(mins - FIVE_HOUR_MINS) <= abs(mins - WEEKLY_MINS):
            five = win
        else:
            weekly = win
    return UsageSnapshot(five, weekly)


@dataclass(frozen=True)
class Steer:
    text: str


@dataclass(frozen=True)
class Interrupt:
    pass


@dataclass(frozen=True)
class Suspend:
    resume_at: int


@dataclass(frozen=True)
class BlockNew:
    resume_at: int


@dataclass(frozen=True)
class Unblock:
    pass


Action = Steer | Interrupt | Suspend | BlockNew | Unblock


class QuotaGuard:
    def __init__(self, thresholds: dict, grace_sec: float = 600, clock: Callable[[], float] = time.monotonic) -> None:
        self.thresholds = dict(thresholds)
        self.grace_sec = grace_sec
        self.clock = clock
        self._enabled = True
        self.active_turn: str | None = None
        self.stopping = False
        self.steer_at = 0.0
        self.blocked_until: int | None = None
        self._retry_steer = False
        # Limits set by the friend (the Codex owner) on their own PC; the lower of host/friend applies.
        self.local: dict = {"five_hour": None, "weekly": None}
        self.force = False  # friend's choice: interrupt at once instead of asking Codex to wrap up

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        if not self._enabled:
            self.stopping = False
            self.blocked_until = None
            self._retry_steer = False

    def set_thresholds(self, thresholds: dict) -> None:
        self.thresholds.update({k: v for k, v in thresholds.items() if v is not None})

    def set_local_limits(self, limits: dict, force: bool) -> None:
        self.local = {k: limits.get(k) for k in ("five_hour", "weekly")}
        self.force = bool(force)

    def effective_limits(self) -> dict:
        out = {}
        for k in ("five_hour", "weekly"):
            vals = [v for v in (self.thresholds.get(k), self.local.get(k)) if v is not None]
            out[k] = min(vals) if vals else None
        return out

    def _start_stopping(self) -> list[Action]:
        self.stopping = True
        self.steer_at = self.clock()
        self._retry_steer = False
        return [Interrupt()] if self.force else [Steer(STOP_TEXT)]

    def on_usage(self, s: UsageSnapshot) -> list[Action]:
        if not self.enabled:
            return []
        limits = self.effective_limits()
        over = [w for k, w in (("five_hour", s.five_hour), ("weekly", s.weekly))
                if w is not None and limits.get(k) is not None and w.used_percent >= limits[k]]
        actions: list[Action] = []
        if over:
            resume_at = max(w.resets_at for w in over)
            if self.active_turn and not self.stopping:
                actions += self._start_stopping()
            if self.blocked_until != resume_at:
                self.blocked_until = resume_at
                actions.append(BlockNew(resume_at))
        elif self.blocked_until is not None:
            self.blocked_until = None
            self._retry_steer = False  # the window came back: nothing left to stop
            actions.append(Unblock())
        return actions

    def on_turn_started(self, turn_id: str) -> list[Action]:
        self.active_turn = turn_id
        # Only ask Codex to stop while usage is actually over a threshold.
        if self.enabled and self.blocked_until is not None and (self._retry_steer or not self.stopping):
            return self._start_stopping()
        return []

    def on_steer_failed(self) -> None:
        self.stopping = False
        self._retry_steer = True

    def on_turn_completed(self) -> list[Action]:
        self.active_turn = None
        if self.stopping:
            self.stopping = False
            if self.blocked_until is not None:
                return [Suspend(self.blocked_until)]
        return []

    def tick(self) -> list[Action]:
        if self.stopping and self.clock() - self.steer_at > self.grace_sec:
            self.stopping = False
            if self.blocked_until is not None:
                return [Interrupt(), Suspend(self.blocked_until)]
        return []
