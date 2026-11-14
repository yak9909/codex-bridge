"""Safe conversion between share-relative paths and real paths."""

from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath
from typing import Literal

from .policy import PolicyStore, normalize_rel


class ToolError(Exception):
    """An error whose message is safe to show to Codex."""


PAUSED_MSG = "停止中です（管理者が一時停止しています）"


def not_found(rel: str) -> ToolError:
    return ToolError(f"not found: {rel}")


class SafePaths:
    def __init__(self, root: Path, policy: PolicyStore) -> None:
        self.root = Path(root).resolve()
        self.policy = policy
        self._root_nc = os.path.normcase(str(self.root))

    def _inside(self, p: Path) -> bool:
        s = os.path.normcase(str(p))
        return s == self._root_nc or s.startswith(self._root_nc.rstrip("\\/") + os.sep)

    def check_running(self) -> None:
        if self.policy.current().paused:
            raise ToolError(PAUSED_MSG)

    def to_rel(self, p: Path) -> str:
        return Path(os.path.relpath(p, self.root)).as_posix() if Path(p) != self.root else ""

    def resolve(self, rel: str, need: Literal["read", "write"]) -> Path:
        """Return the real path for a share-relative path, or raise ToolError."""
        self.check_running()
        shown = rel
        pw = PureWindowsPath(rel)
        if pw.drive or pw.root or rel.startswith(("/", "\\")):
            raise not_found(shown)
        norm = normalize_rel(rel)
        if any(part == ".." for part in norm.split("/")):
            raise not_found(shown)
        real = (self.root / norm).resolve(strict=False)
        if not self._inside(real):
            raise not_found(shown)
        policy = self.policy.current()
        real_rel = self.to_rel(real)
        levels = {policy.level_for(norm), policy.level_for(real_rel)}
        if "hidden" in levels:
            raise not_found(shown)
        if need == "write" and levels != {"write"}:
            raise ToolError(f"書き込みは許可されていません: {norm}")
        return real

    def visible(self, p: Path) -> bool:
        """True if p (an entry found while walking) may be shown to Codex."""
        try:
            real = Path(p).resolve(strict=False)
        except OSError:
            return False
        if not self._inside(real):
            return False
        policy = self.policy.current()
        return (policy.level_for(self.to_rel(Path(p))) != "hidden"
                and policy.level_for(self.to_rel(real)) != "hidden")
