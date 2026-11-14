"""Write tools: write_file, edit_file, move_path, delete_path."""

from __future__ import annotations

import shutil
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
                 task_id_fn: Callable[[], str | None], approve_fn: ApproveFn) -> None:
        self.paths = paths
        self.policy = policy
        self.snapshots = snapshots
        self.audit = audit
        self.task_id_fn = task_id_fn
        self.approve_fn = approve_fn

    def _before(self, rel: str, real: Path) -> None:
        self.snapshots.before_change(self.task_id_fn(), rel, real)

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
        self._before(rel, real)
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
        self._before(rel, real)
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
        self._before(rs, real_src)
        self._before(rd, real_dst)
        real_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(real_src), str(real_dst))
        self._log("move_path", rs, to=rd)
        return {"status": "done", "from": rs, "to": rd}

    async def delete_path(self, path: str) -> dict:
        real = self.paths.resolve(path, "write")
        rel = normalize_rel(path)
        if not real.exists():
            raise not_found(path)
        if self.policy.current().delete_needs_approval:
            decision = await self.approve_fn("delete", f"削除: {rel}")
            if decision == "pending":
                return {"status": "waiting_approval", "path": rel,
                        "message": "削除は管理者の承認待ちです。しばらくしてから同じ delete_path を再度呼んでください。"}
            if decision != "approved":
                raise ToolError(f"削除は拒否されました: {rel}")
        # Re-check after a possibly long wait: the policy may have changed.
        real = self.paths.resolve(path, "write")
        self._before(rel, real)
        if real.is_dir():
            shutil.rmtree(real)
        else:
            real.unlink()
        self._log("delete_path", rel)
        return {"status": "done", "path": rel}
