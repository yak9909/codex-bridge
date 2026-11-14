"""apply_patch in Codex's '*** Begin Patch' format, applied through WriteTools."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from .fs_write import WriteTools, read_text_preserving
from .paths import ToolError, not_found
from .policy import normalize_rel


@dataclass
class Hunk:
    header: str = ""
    old: list[str] = field(default_factory=list)
    new: list[str] = field(default_factory=list)


@dataclass
class AddFile:
    path: str
    content: str


@dataclass
class DeleteFile:
    path: str


@dataclass
class UpdateFile:
    path: str
    move_to: str | None
    hunks: list[Hunk]


PatchOp = AddFile | DeleteFile | UpdateFile


def parse_patch(text: str) -> list[PatchOp]:
    lines = text.replace("\r\n", "\n").split("\n")
    while lines and lines[-1] == "":
        lines.pop()
    if not lines or lines[0].strip() != "*** Begin Patch":
        raise ToolError("パッチは '*** Begin Patch' で始めてください")
    if lines[-1].strip() != "*** End Patch":
        raise ToolError("パッチは '*** End Patch' で終えてください")
    ops: list[PatchOp] = []
    body = lines[1:-1]
    i = 0
    while i < len(body):
        line = body[i]
        if line.startswith("*** Add File: "):
            path = line[len("*** Add File: "):].strip()
            i += 1
            content = []
            while i < len(body) and not body[i].startswith("*** "):
                if not body[i].startswith("+"):
                    raise ToolError(f"Add File の行は '+' で始めてください: {path}")
                content.append(body[i][1:])
                i += 1
            ops.append(AddFile(path, "\n".join(content) + "\n" if content else ""))
        elif line.startswith("*** Delete File: "):
            ops.append(DeleteFile(line[len("*** Delete File: "):].strip()))
            i += 1
        elif line.startswith("*** Update File: "):
            path = line[len("*** Update File: "):].strip()
            i += 1
            move_to = None
            if i < len(body) and body[i].startswith("*** Move to: "):
                move_to = body[i][len("*** Move to: "):].strip()
                i += 1
            hunks: list[Hunk] = []
            cur: Hunk | None = None
            while i < len(body) and not (body[i].startswith("*** ") and body[i] != "*** End of File"):
                ln = body[i]
                if ln == "*** End of File":
                    pass
                elif ln.startswith("@@"):
                    cur = Hunk(header=ln[2:].strip())
                    hunks.append(cur)
                else:
                    if cur is None:
                        cur = Hunk()
                        hunks.append(cur)
                    if ln.startswith("-"):
                        cur.old.append(ln[1:])
                    elif ln.startswith("+"):
                        cur.new.append(ln[1:])
                    else:
                        ctx = ln[1:] if ln.startswith(" ") else ln
                        cur.old.append(ctx)
                        cur.new.append(ctx)
                i += 1
            ops.append(UpdateFile(path, move_to, hunks))
        else:
            raise ToolError(f"パッチの書式が正しくありません: {line[:80]}")
    if not ops:
        raise ToolError("パッチに変更がありません")
    return ops


def _find(lines: list[str], block: list[str], start: int) -> int:
    n = len(block)
    for norm in (lambda s: s, lambda s: s.rstrip()):
        want = [norm(b) for b in block]
        for pos in range(start, len(lines) - n + 1):
            if [norm(x) for x in lines[pos:pos + n]] == want:
                return pos
    return -1


def apply_hunks(text: str, hunks: list[Hunk], rel: str) -> str:
    ends_nl = text.endswith("\n")
    lines = text.split("\n")
    if ends_nl:
        lines.pop()
    cursor = 0
    for idx, h in enumerate(hunks, start=1):
        if h.header:
            hpos = _find(lines, [h.header], cursor)
            if hpos < 0:
                hpos = next((k for k in range(cursor, len(lines)) if lines[k].strip() == h.header.strip()), -1)
            if hpos < 0:
                raise ToolError(f"{rel}: 一致する箇所がありません（{idx} 番目の変更の見出し: {h.header[:60]}）")
            cursor = hpos
        if not h.old:
            insert_at = cursor + 1 if h.header else len(lines)
            lines[insert_at:insert_at] = h.new
            cursor = insert_at + len(h.new)
            continue
        pos = _find(lines, h.old, cursor)
        if pos < 0:
            raise ToolError(f"{rel}: 一致する箇所がありません（{idx} 番目の変更）")
        lines[pos:pos + len(h.old)] = h.new
        cursor = pos + len(h.new)
    out = "\n".join(lines)
    return out + "\n" if ends_nl or not text else out


def _validate(write: WriteTools, ops: list[PatchOp]) -> list[tuple]:
    """Phase 1: check everything (paths, permissions, hunks, encodings) before touching any file."""
    paths = write.paths
    planned: list[tuple] = []
    for op in ops:
        if isinstance(op, AddFile):
            real = paths.resolve(op.path, "write")
            if real.exists():
                raise ToolError(f"追加しようとしたファイルが既にあります: {normalize_rel(op.path)}")
            planned.append(("add", op.path, op.content))
        elif isinstance(op, DeleteFile):
            real = paths.resolve(op.path, "write")
            if not real.exists():
                raise not_found(op.path)
            paths.check_subtree_writable(real, "削除")
            planned.append(("delete", op.path))
        else:
            real = paths.resolve(op.path, "write")
            if not real.is_file():
                raise not_found(op.path)
            if op.move_to:
                dst = paths.resolve(op.move_to, "write")
                if dst.exists():
                    raise ToolError(f"移動先が既にあります: {normalize_rel(op.move_to)}")
            src_text, enc, _ = read_text_preserving(real)
            new_text = apply_hunks(src_text, op.hunks, normalize_rel(op.path))
            try:
                new_text.encode(enc)
            except UnicodeEncodeError as e:
                raise ToolError(f"{normalize_rel(op.path)}: この文字は {enc} で保存できません: "
                                f"{e.object[e.start:e.end]!r}") from e
            planned.append(("update", op.path, op.move_to, new_text))
    return planned


def _apply(write: WriteTools, planned: list[tuple]) -> list[str]:
    """Phase 2: write the validated changes."""
    changed: list[str] = []
    for p in planned:
        if p[0] == "add":
            write.write_file(p[1], p[2])
            changed.append(normalize_rel(p[1]))
        elif p[0] == "delete":
            write.delete_now(p[1])
            changed.append(normalize_rel(p[1]))
        else:
            _, path, move_to, new_text = p
            if move_to:
                write.move_path(path, move_to)
                write.write_file(move_to, new_text)
                changed += [normalize_rel(path), normalize_rel(move_to)]
            else:
                write.write_file(path, new_text)
                changed.append(normalize_rel(path))
    return changed


async def apply_patch(write: WriteTools, text: str) -> dict:
    ops = parse_patch(text)
    # File work runs in a thread so big patches do not stall the event loop.
    planned = await asyncio.to_thread(_validate, write, ops)
    deletes = [p[1] for p in planned if p[0] == "delete"]
    if deletes and write.policy.current().delete_needs_approval:
        decision = await write.approve_fn("delete", "削除（apply_patch）: " + ", ".join(normalize_rel(d) for d in deletes))
        if decision == "pending":
            return {"status": "waiting_approval", "changed": [],
                    "message": "削除を含むため管理者の承認待ちです。しばらくしてから同じパッチを再度適用してください。"}
        if decision != "approved":
            raise ToolError("削除を含むパッチは拒否されました")
    changed = await asyncio.to_thread(_apply, write, planned)
    return {"status": "done", "changed": changed}
