"""Safe conversion between the paths Codex gives and real paths (several shared folders)."""

from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath
from typing import Literal

from .policy import Level, PolicyStore, normalize_rel


class ToolError(Exception):
    """An error whose message is safe to show to Codex."""


PAUSED_MSG = "停止中です（管理者が一時停止しています）"


def not_found(rel: str) -> ToolError:
    return ToolError(f"not found: {rel}")


def _nc(p: Path | str) -> str:
    return os.path.normcase(os.path.abspath(str(p)))


class SafePaths:
    """Paths are absolute (any registered folder) or relative to the main folder."""

    def __init__(self, root: Path, policy: PolicyStore) -> None:
        self.policy = policy
        self.set_root(root)

    def set_root(self, root: Path) -> None:
        """Main folder used when the policy has none of its own (v1 policies)."""
        self._fallback_root = Path(root).resolve()

    @property
    def root(self) -> Path:
        pol = self.policy.current()
        return pol.main if (pol.v2 and pol.main is not None) else self._fallback_root

    # ---- levels ----------------------------------------------------------
    def _under(self, p: Path, base: Path) -> str | None:
        np, nb = _nc(p), _nc(base)
        if np == nb:
            return ""
        prefix = nb.rstrip(os.sep) + os.sep
        return np[len(prefix):].replace(os.sep, "/") if np.startswith(prefix) else None

    def level_of(self, p: Path) -> Level | None:
        """Level of an absolute path; None if it is in no shared folder."""
        pol = self.policy.current()
        if pol.v2:
            return pol.level_for_abs(p)
        rel = self._under(p, self._fallback_root)
        return None if rel is None else pol.level_for(rel)

    def check_running(self) -> None:
        if self.policy.current().paused:
            raise ToolError(PAUSED_MSG)

    # ---- display ---------------------------------------------------------
    def display(self, p: Path) -> str:
        """Relative inside the main folder, absolute ('/' separated) elsewhere."""
        rel = self._under(p, self.root)
        return rel if rel is not None else Path(os.path.abspath(p)).as_posix()

    to_rel = display  # older name used throughout the tools

    def roots(self) -> list[dict]:
        """Registered folders Codex may see (hidden ones are left out)."""
        pol = self.policy.current()
        if not pol.v2:
            return [{"path": self._fallback_root.as_posix(), "level": pol.level_for(""), "main": True}]
        out, main = [], _nc(pol.main) if pol.main else None
        for f in pol.folders:
            if f.level == "hidden" or self.level_of(f.path) in (None, "hidden"):
                continue
            out.append({"path": Path(os.path.abspath(f.path)).as_posix(), "level": self.level_of(f.path),
                        "main": _nc(f.path) == main})
        return out

    def root_paths(self, need_write: bool = False) -> list[Path]:
        """Top-level folders to walk/search (nested registered folders are skipped)."""
        roots = [Path(r["path"]) for r in self.roots() if not need_write or r["level"] == "write"]
        keep: list[Path] = []
        for r in sorted(roots, key=lambda x: len(_nc(x))):
            if not any(self._under(r, k) is not None for k in keep):
                keep.append(r)
        return keep

    # ---- resolving --------------------------------------------------------
    def resolve(self, path: str, need: Literal["read", "write"]) -> Path:
        """Return the real path for an absolute or main-relative path, or raise ToolError."""
        self.check_running()
        shown = path
        raw = str(path).strip()
        pw = PureWindowsPath(raw)
        if pw.drive or pw.root:
            if not pw.drive:
                raise not_found(shown)  # "/x" or "\x": no drive
            parts = list(pw.parts[1:])
            requested_base: Path = Path(pw.anchor)
        else:
            parts = [p for p in normalize_rel(raw).split("/") if p]
            requested_base = self.root
        # ".." escapes; ":" would address NTFS alternate data streams (file.txt:x, ::$DATA).
        if any(part == ".." or ":" in part for part in parts):
            raise not_found(shown)
        requested = Path(os.path.abspath(requested_base.joinpath(*parts)))
        real = requested.resolve(strict=False)
        levels = {self.level_of(requested), self.level_of(real)}
        if None in levels or "hidden" in levels:
            raise not_found(shown)
        if need == "write" and levels != {"write"}:
            raise ToolError(f"書き込みは許可されていません: {self.display(requested)}")
        return real

    def check_subtree_writable(self, real: Path, action: str) -> None:
        """Refuse moving/deleting a folder if anything inside is hidden or not writable.

        The message never names the offending entry (it may be hidden)."""
        if not real.is_dir() or real.is_symlink() or os.path.isjunction(real):
            return
        for dirpath, dirnames, filenames in os.walk(real):
            d = Path(dirpath)
            for name in dirnames + filenames:
                p = d / name
                levels = {self.level_of(p)}
                if not (p.is_symlink() or os.path.isjunction(p)):
                    levels.add(self.level_of(p.resolve()))
                if any(lv != "write" for lv in levels):
                    raise ToolError(f"{action}できません: フォルダの中に、書き込みが許可されていない場所があります")
            dirnames[:] = [n for n in dirnames if not os.path.isjunction(d / n) and not (d / n).is_symlink()]

    def visible(self, p: Path) -> bool:
        """True if p (an entry found while walking) may be shown to Codex."""
        try:
            real = Path(p).resolve(strict=False)
        except OSError:
            return False
        return all(self.level_of(x) not in (None, "hidden") for x in (Path(p), real))
