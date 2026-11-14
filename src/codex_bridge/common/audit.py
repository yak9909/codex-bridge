"""Append-only JSON Lines audit log."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, kind: str, **fields) -> None:
        record = {"ts": datetime.now(timezone.utc).isoformat(), "kind": kind, **fields}
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock, self.path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")
