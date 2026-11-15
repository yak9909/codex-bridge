"""Threads created by this tool (the only ones it may list, read or delete)."""

from __future__ import annotations

import json
import time
from pathlib import Path


class SessionRegistry:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._items: dict[str, dict] = {}
        if self.path.exists():
            self._items = json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._items, ensure_ascii=False, indent=1), encoding="utf-8")

    def add(self, thread_id: str, task_id: str, ephemeral: bool = False) -> None:
        entry = self._items.setdefault(thread_id, {"created": time.time(), "task_ids": [], "ephemeral": ephemeral})
        if task_id not in entry["task_ids"]:
            entry["task_ids"].append(task_id)
        entry["updated"] = time.time()
        self._save()

    def owns(self, thread_id: str) -> bool:
        return thread_id in self._items

    def list(self) -> list[dict]:
        return [{"thread_id": k, **v} for k, v in sorted(self._items.items(), key=lambda kv: kv[1]["created"])]

    def remove(self, thread_id: str) -> None:
        self._items.pop(thread_id, None)
        self._save()

    def purge_ephemeral(self) -> None:
        """A disposable session lives only while its task runs: one still listed at start-up was cut off by a
        crash, and no longer exists."""
        gone = [k for k, v in self._items.items() if v.get("ephemeral")]
        for k in gone:
            del self._items[k]
        if gone:
            self._save()
