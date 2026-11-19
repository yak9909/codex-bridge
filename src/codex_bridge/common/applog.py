"""One app log for both sides: JSON lines, newest last, kept short."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Literal

LEVELS = {"info": 0, "warn": 1, "error": 2}


class AppLog:
    def __init__(self, path: Path, keep: int = 5000) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._trim(keep)

    def _trim(self, keep: int) -> None:
        try:
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return
        if len(lines) > keep:
            self.path.write_text("".join(line + "\n" for line in lines[-keep:]), encoding="utf-8", newline="\n")

    def add(self, side: Literal["use", "lend"], level: Literal["info", "warn", "error"], text: str) -> None:
        line = json.dumps({"t": time.time(), "side": side, "level": level, "text": text}, ensure_ascii=False)
        with self._lock:
            try:
                with open(self.path, "a", encoding="utf-8", newline="\n") as f:
                    f.write(line + "\n")
            except OSError:
                pass  # Logging must not interrupt shutdown after the data folder has moved.

    def entries(self, side: str | None = None, min_level: str | None = None, q: str | None = None,
                limit: int = 1000) -> list[dict]:
        floor = LEVELS.get(min_level or "info", 0)
        needle = q.lower() if q else None
        with self._lock:
            try:
                lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                return []
        out: list[dict] = []
        for line in lines:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if not (isinstance(e, dict) and isinstance(e.get("text"), str) and isinstance(e.get("t"), (int, float))):
                continue
            if side is not None and e.get("side") != side:
                continue
            if LEVELS.get(e.get("level"), 0) < floor:
                continue
            if needle is not None and needle not in e["text"].lower():
                continue
            out.append(e)
        return out[-limit:] if limit > 0 else []
