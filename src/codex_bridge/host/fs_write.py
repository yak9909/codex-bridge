"""Write tools: write_file, edit_file, move_path, delete_path."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import shutil
import stat
import threading
from pathlib import Path
from typing import Awaitable, Callable

from ..common.audit import AuditLog
from .fs_read import sniff_file
from .paths import SafePaths, ToolError, not_found
from .policy import PolicyStore, normalize_rel
from .snapshots import Snapshots, atomic_write_bytes

# approve_fn(kind, summary) -> "approved" | "denied" | "pending"
ApproveFn = Callable[[str, str], Awaitable[str]]


def read_text_preserving(abs_path: Path) -> tuple[str, str, str]:
    """Return (text with '\\n' newlines, encoding, newline style)."""
    enc = sniff_file(abs_path)
    if enc is None:
        raise ToolError(f"テキストとして扱えません（バイナリまたは不明な文字コード）: {abs_path.name}")
    raw = abs_path.read_bytes().decode(enc)
    crlf = raw.count("\r\n")
    lf = raw.count("\n") - crlf
    newline = "\r\n" if crlf > lf else "\n"
    return raw.replace("\r\n", "\n"), enc, newline


def write_text_preserving(abs_path: Path, text: str, encoding: str, newline: str) -> None:
    text = text.replace("\r\n", "\n")
    if newline != "\n":
        text = text.replace("\n", newline)
    try:
        data = text.encode(encoding)
    except UnicodeEncodeError as e:
        raise ToolError(f"この文字は {encoding} で保存できません: {e.object[e.start:e.end]!r}") from e
    atomic_write_bytes(abs_path, data)


class WriteTools:
    def __init__(self, paths: SafePaths, policy: PolicyStore, snapshots: Snapshots, audit: AuditLog,
                 task_id_fn: Callable[[], str | None], approve_fn: ApproveFn,
                 lender_fn: Callable[[], str | None] = lambda: None) -> None:
        self.lender_fn = lender_fn  # invite name of the lender making the current call (None: not known)
        self.paths = paths
        self.policy = policy
        self.snapshots = snapshots
        self.audit = audit
        self.task_id_fn = task_id_fn
        self.approve_fn = approve_fn
        self._change_lock = threading.Lock()

    @staticmethod
    def _same_contents(left: Path, right: Path) -> bool:
        if left.is_dir() and right.is_dir():
            names = {p.name for p in left.iterdir()}
            return names == {p.name for p in right.iterdir()} and all(
                WriteTools._same_contents(left / name, right / name) for name in names)
        if not left.is_file() or not right.is_file() or left.stat().st_size != right.stat().st_size:
            return False
        with left.open("rb") as original, right.open("rb") as saved:
            while chunk := original.read(64 * 1024):
                if chunk != saved.read(len(chunk)):
                    return False
            return not saved.read(1)

    @contextmanager
    def _changing(self, *paths: tuple[str, Path]):
        """Keep originals before changing them, but discard new records for a failed no-op.

        Serialize writers so cleanup cannot remove another call's snapshot. If a move or
        recursive delete partly succeeded, keep its originals so it can still be undone.
        """
        with self._change_lock:
            task_id = self.task_id_fn()
            snapshots = self.snapshots
            directory = snapshots._dir(task_id)
            previous = snapshots._manifest(directory)
            try:
                for rel, real in paths:
                    snapshots.before_change(task_id, rel, real)
                yield
            except (OSError, ToolError):
                try:
                    with snapshots._lock:
                        manifest = snapshots._manifest(directory)
                        added = {key: entry for key, entry in manifest.items() if key not in previous}
                        unchanged = all(
                            self._same_contents(Path(entry["abs"]), directory / entry["copy"])
                            if entry["existed"] else not Path(entry["abs"]).exists()
                            for entry in added.values())
                        if added and unchanged:
                            snapshots._save_manifest(directory, previous)
                            for entry in added.values():
                                if entry["copy"]:
                                    copy = directory / entry["copy"]
                                    if copy.is_dir():
                                        shutil.rmtree(copy, onexc=self._remove_readonly_copy)
                                    else:
                                        copy.chmod(copy.stat().st_mode | stat.S_IWRITE)
                                        copy.unlink()
                except OSError:
                    pass  # Preserve the original failure and keep any originals we cannot verify.
                raise

    @staticmethod
    def _remove_readonly_copy(function, path, error) -> None:
        if not isinstance(error, PermissionError):
            raise error
        saved = Path(path)
        saved.chmod(saved.stat().st_mode | stat.S_IWRITE)
        function(path)

    def _log(self, op: str, rel: str, **extra) -> None:
        self.audit.write("file_change", op=op, path=rel, task_id=self.task_id_fn(), **extra)

    def write_file(self, path: str, content: str) -> dict:
        real = self.paths.resolve(path, "write")
        rel = normalize_rel(path)
        if real.is_dir():
            raise ToolError(f"フォルダには書き込めません: {rel}")
        if real.exists():
            _, enc, newline = read_text_preserving(real)
        else:
            enc, newline = "utf-8", "\r\n" if "\r\n" in content else "\n"
        with self._changing((rel, real)):
            write_text_preserving(real, content, enc, newline)
        self._log("write_file", rel, bytes=len(content))
        return {"status": "done", "path": rel, "encoding": "utf-8" if enc == "utf-8-sig" else enc}

    def edit_file(self, path: str, old: str, new: str, replace_all: bool = False) -> dict:
        real = self.paths.resolve(path, "write")
        rel = normalize_rel(path)
        if not real.is_file():
            raise not_found(path)
        text, enc, newline = read_text_preserving(real)
        old_n = old.replace("\r\n", "\n")
        if not old_n:
            raise ToolError("置き換え前の文字列が空です")
        count = text.count(old_n)
        if count == 0:
            raise ToolError(f"一致する箇所がありません: {rel}")
        if count > 1 and not replace_all:
            raise ToolError(f"{count} か所で一致しました。前後を含めて一意にするか、replace_all を指定してください: {rel}")
        with self._changing((rel, real)):
            write_text_preserving(real, text.replace(old_n, new.replace("\r\n", "\n")), enc, newline)
        self._log("edit_file", rel, replacements=count)
        return {"status": "done", "path": rel, "replacements": count}

    def move_path(self, src: str, dst: str) -> dict:
        real_src = self.paths.resolve(src, "write")
        real_dst = self.paths.resolve(dst, "write")
        rs, rd = normalize_rel(src), normalize_rel(dst)
        if not real_src.exists():
            raise not_found(src)
        if real_dst.exists():
            raise ToolError(f"移動先が既にあります: {rd}")
        self.paths.check_subtree_writable(real_src, "移動")
        with self._changing((rs, real_src), (rd, real_dst)):
            real_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(real_src), str(real_dst))
        self._log("move_path", rs, to=rd)
        return {"status": "done", "from": rs, "to": rd}

    async def delete_path(self, path: str) -> dict:
        real = self.paths.resolve(path, "write")
        rel = normalize_rel(path)
        if not real.exists():
            raise not_found(path)
        await asyncio.to_thread(self.paths.check_subtree_writable, real, "削除")
        if self.policy.current().delete_approval_for(self.lender_fn()):
            decision = await self.approve_fn("delete", f"削除: {rel}")
            if decision == "pending":
                return {"status": "waiting_approval", "path": rel,
                        "message": "削除は管理者の承認待ちです。しばらくしてから同じ delete_path を再度呼んでください。"}
            if decision != "approved":
                raise ToolError(f"削除は拒否されました: {rel}")
        # Re-check after a possibly long wait (the policy may have changed); file work off the event loop.
        await asyncio.to_thread(self.delete_now, path)
        return {"status": "done", "path": rel}

    def delete_now(self, path: str) -> None:
        """Delete without asking (callers handle approval)."""
        real = self.paths.resolve(path, "write")
        rel = normalize_rel(path)
        if not real.exists():
            raise not_found(path)
        self.paths.check_subtree_writable(real, "削除")
        with self._changing((rel, real)):
            if real.is_dir():
                shutil.rmtree(real)
            else:
                real.unlink()
        self._log("delete_path", rel)
