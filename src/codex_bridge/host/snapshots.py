"""Keep originals before a task changes them, so a whole task can be undone."""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
from pathlib import Path

NO_TASK = "_no_task"
SAFE_ID = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,80}")


class Snapshots:
    def __init__(self, data_dir: Path) -> None:
        self.base = Path(data_dir) / "snapshots"
        self._lock = threading.Lock()

    def _dir(self, task_id: str | None) -> Path:
        """Snapshot folder of a task. Rejects anything that is not a plain id (never leaves self.base)."""
        name = NO_TASK if task_id is None else task_id
        if not SAFE_ID.fullmatch(name):
            raise ValueError(f"不正なタスク ID です: {task_id!r}")
        d = self.base / name
        if d.resolve().parent != self.base.resolve():
            raise ValueError(f"不正なタスク ID です: {task_id!r}")
        return d

    def _manifest(self, d: Path) -> dict:
        m = d / "manifest.json"
        return json.loads(m.read_text(encoding="utf-8")) if m.exists() else {}

    def _save_manifest(self, d: Path, manifest: dict) -> None:
        (d / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")

    def before_change(self, task_id: str | None, rel: str, abs_path: Path) -> None:
        """Save the original of rel the first time this task touches it."""
        with self._lock:
            d = self._dir(task_id)
            d.mkdir(parents=True, exist_ok=True)
            manifest = self._manifest(d)
            key = rel.lower()
            if key in manifest:
                return
            entry = {"rel": rel, "abs": str(abs_path), "existed": abs_path.exists(), "kind": None, "copy": None}
            if abs_path.exists():
                copy = d / f"{len(manifest):06d}"
                if abs_path.is_dir():
                    shutil.copytree(abs_path, copy)
                    entry["kind"] = "dir"
                else:
                    shutil.copy2(abs_path, copy)
                    entry["kind"] = "file"
                entry["copy"] = copy.name
            manifest[key] = entry
            self._save_manifest(d, manifest)

    def undo(self, task_id: str | None) -> list[str]:
        with self._lock:
            d = self._dir(task_id)
            manifest = self._manifest(d)
            restored = []
            # Restore in reverse order so parents created later are removed first.
            for entry in reversed(list(manifest.values())):
                target = Path(entry["abs"])
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
                if entry["existed"]:
                    src = d / entry["copy"]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if entry["kind"] == "dir":
                        shutil.copytree(src, target)
                    else:
                        shutil.copy2(src, target)
                restored.append(entry["rel"])
            if d.exists():
                shutil.rmtree(d)
            return restored

    def touched(self, task_id: str | None) -> list[str]:
        return [e["rel"] for e in self._manifest(self._dir(task_id)).values()]


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".cbtmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
