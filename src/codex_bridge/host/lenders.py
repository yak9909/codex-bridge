"""Per-lender state (one lender = one invite name) and the choice of who gets a new task."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, Iterable

SendFn = Callable[[dict], Awaitable[None]]
WINDOWS = ("five_hour", "weekly")
# A v0.1 agent sends no "protocol"/"capabilities" in hello: sessions can be continued, no pre-approval.
V1_CAPABILITIES = {"resumable": True, "pre_approval": False}


def _no_usage() -> dict:
    return {"five_hour": None, "weekly": None, "ts": None}


@dataclass
class Lender:
    name: str
    send: SendFn | None = None
    connected: bool = False
    ready: bool = False  # hello arrived on this connection; until then what the lender can do is unknown
    agent_version: str | None = None
    codex_version: str | None = None
    auth_mode: str | None = None
    protocol: int = 1
    capabilities: dict = field(default_factory=lambda: dict(V1_CAPABILITIES))
    limits: dict | None = None  # the lender's own limits: {five_hour, weekly, force}, via normalize_limits
    usage: dict = field(default_factory=_no_usage)
    current_task_id: str | None = None
    last_assigned: float = 0.0
    since: float | None = None  # when the current (or last) connection was made


def _number(v) -> float | None:
    """A finite number, or None for anything else (text, bool, NaN, ...)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        return v if math.isfinite(v) else None
    except OverflowError:  # an integer too large for a float
        return None


def normalize_limits(raw) -> dict | None:
    """The limits a lender's agent sent, made safe to compute with: each window is a percent in
    (0, 100] or None (no limit of its own). None when `raw` is not an object at all."""
    if not isinstance(raw, dict):
        return None
    out: dict = {k: v if (v := _number(raw.get(k))) is not None and 0 < v <= 100 else None for k in WINDOWS}
    out["force"] = raw.get("force") is True
    return out


def _moment(v) -> float | None:
    """Seconds since the epoch, if a clock can show that moment; else None."""
    try:
        datetime.fromtimestamp(_number(v), timezone.utc).astimezone()
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return v


def normalize_window(raw) -> dict | None:
    """One usage window as an agent reported it, or None when its numbers cannot be used."""
    if not isinstance(raw, dict) or (used := _number(raw.get("used_percent"))) is None:
        return None
    return {**raw, "used_percent": used, "resets_at": _moment(raw.get("resets_at"))}


def effective_limits(lender: Lender, thresholds: dict) -> dict:
    """The limit that applies to this lender: the lower of the host's threshold and the lender's own."""
    out = {}
    for k in WINDOWS:
        vals = [v for v in (thresholds.get(k), (lender.limits or {}).get(k)) if v is not None]
        out[k] = min(vals) if vals else None
    return out


def blocked_until(lender: Lender, thresholds: dict, now: float | None = None) -> float | None:
    """When new tasks are accepted again, or None while the lender is under its limits."""
    now = time.time() if now is None else now
    limits = effective_limits(lender, thresholds)
    until = None
    for k in WINDOWS:
        w = lender.usage.get(k)
        if w and limits[k] is not None and w.get("used_percent", 0) >= limits[k] and (w.get("resets_at") or 0) > now:
            until = max(until or 0, w["resets_at"])
    return until


def headroom(lender: Lender, thresholds: dict) -> float | None:
    """Smaller of (limit - used) over the two windows; None while no usage has arrived."""
    limits = effective_limits(lender, thresholds)
    rooms = [limits[k] - w.get("used_percent", 0) for k in WINDOWS
             if (w := lender.usage.get(k)) and limits[k] is not None]
    return min(rooms) if rooms else None


def pick_lender(lenders: Iterable[Lender], thresholds: dict, now: float | None = None) -> Lender | None:
    """Among connected (hello received), idle, not blocked lenders: most headroom;
    ties go to the least recently used."""
    free = [l for l in lenders if l.connected and l.ready and l.current_task_id is None
            and blocked_until(l, thresholds, now) is None]

    def order(l: Lender) -> tuple[float, float]:
        room = headroom(l, thresholds)
        return (-(float("-inf") if room is None else room), l.last_assigned)
    return min(free, key=order, default=None)
