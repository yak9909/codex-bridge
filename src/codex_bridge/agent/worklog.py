"""The lender's record of what Codex did, per task: <dir>/<task_id>.jsonl (design 4.10).

One JSON line per record: {ts, kind, data}. `kind` is an event kind of the host protocol (plan, agent_message,
tool_finished, ...) or one the host already has by other means (task_prompt, state). The half-written message
(`progress`) is never recorded. Records older than the retention period are removed by `prune()`, or all of
them by `clear()`.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Callable

# The task id comes from the host and becomes a file name on this PC: only plain names, never a path.
_SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,100}")
DAY = 86400


def _read(path: Path) -> list[dict]:
    try:
        # errors="replace": a stray bad byte costs that one line, not the whole file (which would then be
        # neither shown nor pruned)
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue  # cut off by a crash, or not ours
        if isinstance(rec, dict) and isinstance(rec.get("ts"), (int, float)):
            out.append(rec)
    return out


class WorkLog:
    def __init__(self, dir: Path, retention_days: float = 30, clock: Callable[[], float] = time.time) -> None:
        self.dir = Path(dir)
        self.retention_days = retention_days
        self.clock = clock

    def _path(self, task_id: str) -> Path:
        if not isinstance(task_id, str) or not _SAFE_ID.fullmatch(task_id):
            raise ValueError(f"not a task id: {str(task_id)[:40]!r}")
        return self.dir / f"{task_id}.jsonl"

    def add(self, task_id: str, kind: str, data, *, connection_id=None, host_name=None) -> None:
        path = self._path(task_id)
        self.dir.mkdir(parents=True, exist_ok=True)
        fields = {"connection_id": connection_id, "host_name": host_name} if connection_id else {}
        line = json.dumps({"ts": self.clock(), "kind": kind, "data": data, **fields}, ensure_ascii=False) + "\n"
        with open(path, "a+b") as f:
            size = f.seek(0, os.SEEK_END)
            if size:
                f.seek(size - 1)
                if f.read(1) != b"\n":  # cut off by a crash: this record must not join that line
                    line = "\n" + line
            f.write(line.encode("utf-8"))

    def task_events(self, task_id: str) -> list[dict]:
        return _read(self._path(task_id))

    def tasks(self) -> list[dict]:
        """One row per task with records, the most recently written first."""
        rows = []
        for path in self.dir.glob("*.jsonl") if self.dir.exists() else ():
            recs = _read(path)
            if recs and _SAFE_ID.fullmatch(path.stem):
                rows.append({"task_id": path.stem, "started": recs[0]["ts"], "updated": recs[-1]["ts"],
                             "count": len(recs)})
        return sorted(rows, key=lambda r: r["updated"], reverse=True)

    def prune(self) -> int:
        """Remove the tasks last written longer ago than the retention period; how many were removed.
        It runs at every task start, so it goes by the file's modification time and reads nothing."""
        limit = self.clock() - self.retention_days * DAY
        gone = 0
        for path in list(self.dir.glob("*.jsonl")) if self.dir.exists() else ():
            try:
                old = path.stat().st_mtime < limit
            except OSError:
                continue
            if old and _SAFE_ID.fullmatch(path.stem) and self._remove(path.stem):
                gone += 1
        return gone

    def clear(self) -> int:
        """The "delete the records" button: every task's record is removed; how many were."""
        names = [p.stem for p in self.dir.glob("*.jsonl")] if self.dir.exists() else []
        return sum(1 for name in names if _SAFE_ID.fullmatch(name) and self._remove(name))

    def _remove(self, task_id: str) -> bool:
        try:
            self._path(task_id).unlink()
            return True
        except OSError:
            return False
