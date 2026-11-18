"""Legacy diagnostic log storage. The local screen and API now live in ui.py.

Kept for existing log.jsonl / visibility.txt data; WorkLog supplies the screen.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

VISIBLE = {
    "summary": {"task_prompt", "final_report"},
    "files": {"task_prompt", "final_report", "tool_call"},
    "all": {"task_prompt", "final_report", "tool_call", "agent_message", "status", "state", "error",
            "started", "turn_completed"},
}
LOGGED_ELSEWHERE = {"tool_started", "plan", "reasoning_summary", "token_usage", "progress"}
MAX_ENTRIES = 5000


def _normalize(m: dict) -> dict | None:
    """Turn a runner log message into {ts, task_id, kind, text}."""
    t = m.get("type")
    base = {"ts": time.time(), "task_id": m.get("task_id")}
    if t == "task_prompt":
        return {**base, "kind": "task_prompt", "text": m.get("prompt", "")}
    if t == "final_report":
        return {**base, "kind": "final_report", "text": m.get("text") or ""}
    if t == "event":
        d = m.get("data") or {}
        kind = m.get("kind")
        if kind in ("tool_call", "tool_finished"):  # v0.2 sends tool_finished where v0.1 sent tool_call
            args = d.get("arguments", "")
            args = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
            return {**base, "kind": "tool_call", "text": f"{d.get('tool')} {args}"}
        if kind in LOGGED_ELSEWHERE:  # in the work log (agent/worklog.py), not in this list
            return None
        return {**base, "kind": m.get("kind"), "text": d.get("text") or d.get("status") or ""}
    if t == "task_state":
        return {**base, "kind": "state", "text": m.get("state", "")}
    if t == "error":
        return {**base, "kind": "error", "text": m.get("text", "")}
    return None


class LogStore:
    def __init__(self, directory: Path) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "log.jsonl"
        self.state_path = self.dir / "visibility.txt"
        self.visibility = self.state_path.read_text(encoding="utf-8").strip() if self.state_path.exists() else "summary"
        self.entries: list[dict] = []
        if self.path.exists():
            self.entries = [json.loads(l) for l in self.path.read_text(encoding="utf-8").splitlines()][-MAX_ENTRIES:]
        self._lock = threading.Lock()

    def set_visibility(self, value: str) -> None:
        if value in VISIBLE:
            self.visibility = value
            self.state_path.write_text(value, encoding="utf-8")

    def add(self, m: dict) -> None:
        if m.get("type") == "settings":
            self.set_visibility(m.get("log_visibility", self.visibility))
            return
        e = _normalize(m)
        if e is None:
            return
        with self._lock:
            self.entries.append(e)
            del self.entries[:-MAX_ENTRIES]
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

    def visible(self) -> list[dict]:
        allowed = VISIBLE[self.visibility]
        return [e for e in self.entries if e["kind"] in allowed]
