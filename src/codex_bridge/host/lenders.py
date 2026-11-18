"""Per-lender state (one lender = one invite name) and the choice of who gets a new task."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, Iterable

from codex_bridge.common.protocol import EFFORT_ORDER

SendFn = Callable[[dict], Awaitable[None]]
WINDOWS = ("five_hour", "weekly")
# A v0.1 agent sends no "protocol"/"capabilities" in hello: sessions can be continued, no pre-approval.
V1_CAPABILITIES = {"resumable": True, "pre_approval": False, "model_select": False}


def _no_usage() -> dict:
    return {"five_hour": None, "weekly": None, "ts": None}


@dataclass
class Lender:
    name: str
    send: SendFn | None = None
    connected: bool = False
    ready: bool = False  # hello arrived on this connection; until then what the lender can do is unknown
    busy_elsewhere: bool = False
    agent_version: str | None = None
    codex_version: str | None = None
    auth_mode: str | None = None
    protocol: int = 1
    capabilities: dict = field(default_factory=lambda: dict(V1_CAPABILITIES))
    models: list[dict] | None = None  # unknown until an agent sends its model offer
    max_effort: str | None = None
    limits: dict | None = None  # the lender's own limits: {five_hour, weekly, force}, via normalize_limits
    usage: dict = field(default_factory=_no_usage)
    current_task_id: str | None = None
    last_assigned: float = 0.0
    since: float | None = None  # when the current (or last) connection was made


def number(v) -> float | None:
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
    out: dict = {k: v if (v := number(raw.get(k))) is not None and 0 < v <= 100 else None for k in WINDOWS}
    out["force"] = raw.get("force") is True
    return out


def as_text(v) -> str | None:
    """A version or login name as an agent sent it: text, or None for anything else."""
    return v if isinstance(v, str) else None


def as_protocol(v) -> int:
    """The protocol number of a hello; 1 (v0.1) when it is missing or not a positive whole number."""
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 1


def normalize_capabilities(raw) -> dict:
    """What a lender's agent said it can do: the known flags only, each a real true/false.
    Anything else (a v0.1 agent sends none at all) counts as v0.1."""
    raw = raw if isinstance(raw, dict) else {}
    return {k: raw[k] if isinstance(raw.get(k), bool) else default for k, default in V1_CAPABILITIES.items()}


def normalize_models(raw) -> list[dict] | None:
    """A lender's model offer, keeping valid entries from at most 200 items; None when not a list."""
    if not isinstance(raw, list):
        return None
    out: list[dict] = []
    for entry in raw[:200]:
        if not isinstance(entry, dict) or not (model := as_text(entry.get("model"))):
            continue
        efforts = entry.get("efforts")
        efforts = efforts if isinstance(efforts, list) else []
        out.append({"model": model, "name": as_text(entry.get("name")) or model,
                    "efforts": list(dict.fromkeys(v for v in efforts if isinstance(v, str))),
                    "default_effort": as_text(entry.get("default_effort")),
                    "is_default": entry.get("is_default") is True})
    return out


def normalize_effort(v) -> str | None:
    """A known reasoning effort, or None for anything else."""
    return v if isinstance(v, str) and v in EFFORT_ORDER else None


def describe_models(models: list[dict] | None) -> str:
    """The models and efforts a lender offers, for a refusal reason."""
    return "、".join(f"{m['model']}（{'・'.join(m['efforts'])}）" for m in models) if models else "なし"


def lender_accepts(lender: Lender, model: str | None, effort: str | None) -> str | None:
    """None when the requested choice is offered; otherwise the reason it cannot be used."""
    if model is None and effort is None:
        return None
    if not lender.capabilities.get("model_select"):
        return f"「{lender.name}」の exe は v0.2 以前のため、モデルと推論の強さを指定できません。"
    models = lender.models
    if models is None:
        return (f"「{lender.name}」のモデルの一覧がまだ届いていません（その人の Codex が起動すると届きます）。"
                "少し待つか、モデルと推論の強さを指定せずに依頼してください。")
    if model is not None:
        target = next((m for m in models if m["model"] == model), None)
    else:
        target = next((m for m in models if m.get("is_default") is True), models[0] if models else None)
    if target is None or (effort is not None and effort not in target["efforts"]):
        return (f"「{lender.name}」は、指定のモデルと推論の強さを使わせていません。"
                f"この人が使えるモデルと推論の強さ: {describe_models(models)}")
    return None


def moment(v) -> float | None:
    """Seconds since the epoch, if a clock can show that moment; else None."""
    try:
        datetime.fromtimestamp(number(v), timezone.utc).astimezone()
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return v


def normalize_window(raw) -> dict | None:
    """One usage window as an agent reported it, or None when its numbers cannot be used.
    Only the two known values are kept; the percent must be one (0..100), so that a made-up
    value cannot make a lender look like the one with the most room."""
    if not isinstance(raw, dict) or (used := number(raw.get("used_percent"))) is None or not 0 <= used <= 100:
        return None
    return {"used_percent": used, "resets_at": moment(raw.get("resets_at"))}


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
    free = [l for l in lenders if l.connected and l.ready and not l.busy_elsewhere and l.current_task_id is None
            and blocked_until(l, thresholds, now) is None]

    def order(l: Lender) -> tuple[float, float]:
        room = headroom(l, thresholds)
        return (-(float("-inf") if room is None else room), l.last_assigned)
    return min(free, key=order, default=None)
