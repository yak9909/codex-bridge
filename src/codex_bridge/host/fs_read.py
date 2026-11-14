"""Read-only tools: list_dir, find_files, stat, read_file."""

from __future__ import annotations

import codecs
import os
from datetime import datetime, timezone
from pathlib import Path

from .paths import SafePaths, ToolError, not_found
from .policy import PolicyStore, glob_match, normalize_rel

SAMPLE_BYTES = 64 * 1024
DEFAULT_HEX_BYTES = 4096


def _mtime(st: os.stat_result) -> str:
    return datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()


def detect_encoding(sample: bytes) -> str | None:
    """Return 'utf-8', 'utf-8-sig' or 'cp932' for text, None for binary."""
    if b"\x00" in sample:
        return None
    if sample.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    for enc in ("utf-8", "cp932"):
        dec = codecs.getincrementaldecoder(enc)()
        try:
            dec.decode(sample, final=False)
            return enc
        except UnicodeDecodeError:
            continue
    return None


def sniff_file(path: Path) -> str | None:
    with open(path, "rb") as f:
        return detect_encoding(f.read(SAMPLE_BYTES))


class ReadTools:
    def __init__(self, paths: SafePaths, policy: PolicyStore) -> None:
        self.paths = paths
        self.policy = policy

    def _entry(self, p: Path) -> dict:
        st = p.stat()
        return {
            "name": p.name,
            "type": "dir" if p.is_dir() else "file",
            "size": st.st_size if p.is_file() else None,
            "mtime": _mtime(st),
        }

    def list_dir(self, path: str = ".") -> list[dict]:
        real = self.paths.resolve(path, "read")
        if not real.is_dir():
            raise not_found(path) if not real.exists() else ToolError(f"フォルダではありません: {normalize_rel(path)}")
        out = []
        for child in sorted(real.iterdir(), key=lambda c: c.name.lower()):
            if self.paths.visible(child):
                try:
                    out.append(self._entry(child))
                except OSError:
                    continue
        return out

    def find_files(self, pattern: str, path: str = ".", limit: int = 1000) -> list[str]:
        base = self.paths.resolve(path, "read")
        if not base.is_dir():
            raise not_found(path)
        found: list[str] = []
        for dirpath, dirnames, filenames in os.walk(base):
            d = Path(dirpath)
            dirnames[:] = sorted(n for n in dirnames if self.paths.visible(d / n))
            for name in sorted(filenames):
                fp = d / name
                rel_to_base = Path(os.path.relpath(fp, base)).as_posix()
                if glob_match(pattern, rel_to_base) and self.paths.visible(fp):
                    found.append(self.paths.to_rel(fp))
                    if len(found) >= limit:
                        return found
        return found

    def stat(self, path: str) -> dict:
        real = self.paths.resolve(path, "read")
        if not real.exists():
            raise not_found(path)
        e = self._entry(real)
        e["path"] = self.paths.to_rel(real) if real != self.paths.root else "."
        return e

    def read_file(self, path: str, offset_line: int = 1, max_lines: int = 2000,
                  byte_offset: int | None = None, max_bytes: int | None = None) -> dict:
        real = self.paths.resolve(path, "read")
        if not real.is_file():
            raise not_found(path)
        limit = self.policy.current().limits.read_max_bytes
        cap = min(max_bytes, limit) if max_bytes else limit
        rel = normalize_rel(path)
        enc = sniff_file(real)
        if enc is None or byte_offset is not None:
            return self._read_hex(real, rel, byte_offset or 0, min(max_bytes or DEFAULT_HEX_BYTES, limit))
        offset_line = max(1, int(offset_line))
        lines: list[str] = []
        used = 0
        truncated = False
        line_truncated = False
        next_line = None
        with open(real, "r", encoding=enc, errors="replace", newline="") as f:
            for lineno, line in enumerate(f, start=1):
                if lineno < offset_line:
                    continue
                size = len(line.encode("utf-8"))
                if not lines and size > cap:
                    # A single line longer than the cap: return its beginning.
                    lines.append(line.encode("utf-8")[:cap].decode("utf-8", errors="ignore"))
                    truncated = line_truncated = True
                    next_line = lineno + 1
                    break
                if len(lines) >= max_lines or used + size > cap:
                    truncated = True
                    next_line = lineno
                    break
                lines.append(line)
                used += size
        if line_truncated:
            return {
                "path": rel, "encoding": "utf-8" if enc == "utf-8-sig" else enc, "start_line": offset_line,
                "text": "".join(lines), "truncated": True, "line_truncated": True, "next_offset_line": next_line,
                "message": f"{offset_line} 行目が長すぎるため先頭だけを返しました。続きは byte_offset を指定して読んでください。",
            }
        return {
            "path": rel,
            "encoding": "utf-8" if enc == "utf-8-sig" else enc,
            "start_line": offset_line,
            "text": "".join(lines),
            "truncated": truncated,
            "next_offset_line": next_line,
        }

    def _read_hex(self, real: Path, rel: str, offset: int, n: int) -> dict:
        size = real.stat().st_size
        with open(real, "rb") as f:
            f.seek(offset)
            data = f.read(n)
        end = offset + len(data)
        return {
            "path": rel,
            "binary": True,
            "size": size,
            "byte_offset": offset,
            "hex": data.hex(),
            "truncated": end < size,
            "next_byte_offset": end if end < size else None,
        }
