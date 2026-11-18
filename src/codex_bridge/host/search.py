"""grep tool backed by ripgrep, filtered by the policy."""

from __future__ import annotations

import fnmatch
import json
import re
import subprocess
from pathlib import Path

from .fs_read import detect_encoding, sniff_file
from .paths import SafePaths, ToolError, not_found
from .policy import PolicyStore

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _glob_matches(pattern: str, relative: str) -> bool:
    """Match path segments; ** also matches zero directories (unlike fnmatch)."""
    if "/" not in pattern:
        return fnmatch.fnmatchcase(relative.rsplit("/", 1)[-1], pattern)

    def match(patterns, parts):
        if not patterns:
            return not parts
        if patterns[0] == "**":
            return match(patterns[1:], parts) or bool(parts) and match(patterns, parts[1:])
        return bool(parts) and fnmatch.fnmatchcase(parts[0], patterns[0]) and match(patterns[1:], parts[1:])

    return match(pattern.lstrip("/").split("/"), relative.split("/"))


class SearchTools:
    def __init__(self, paths: SafePaths, policy: PolicyStore, rg_path: Path) -> None:
        self.paths = paths
        self.policy = policy
        self.rg_path = Path(rg_path)

    def grep(self, pattern: str, path: str | None = None, glob: str | None = None,
             ignore_case: bool = False, context: int = 0, limit: int = 500) -> dict:
        """Without a path: every shared folder."""
        if path and path.strip():
            base = self.paths.resolve(path, "read")
            if not base.exists():
                raise not_found(path)
            if not self.paths.discoverable(base):
                return {"matches": [], "truncated": False}
            targets = [(base, ".")] if base.is_dir() else [(base.parent, base.name)]
        else:
            self.paths.check_running()
            targets = [(r, ".") for r in self.paths.root_paths()]
        limit = max(1, min(int(limit), self.policy.current().limits.grep_max_matches))
        matches: list[dict] = []
        seen: set[tuple[str, int]] = set()
        truncated = False
        for cwd, target in targets:
            if not self.paths.discoverable(cwd / target):
                continue
            try:
                truncated = self._run(pattern, cwd, target, glob, ignore_case, context, limit, None, matches, seen)
                if not truncated and not pattern.isascii():
                    # Shift_JIS files only match when ripgrep decodes them as such.
                    truncated = self._run(pattern, cwd, target, glob, ignore_case, context, limit, "shift_jis",
                                          matches, seen)
            except OSError:
                truncated = self._fallback(pattern, cwd, target, glob, ignore_case, context, limit, matches, seen)
            if truncated:
                break
        return {"matches": matches, "truncated": truncated}

    def _run(self, pattern, cwd: Path, target, glob, ignore_case, context, limit, encoding,
             matches: list[dict], seen: set) -> bool:
        cmd = [str(self.rg_path), "--json", "--no-config", "--hidden", "-e", pattern]
        if ignore_case:
            cmd.append("-i")
        if context:
            cmd += ["-C", str(int(context))]
        if glob:
            cmd += ["--glob", glob]
        for exclusion in self.paths.search_exclusions(cwd):
            cmd += ["--iglob", exclusion]
        if encoding:
            cmd += ["--encoding", encoding]
        # "--" ends option parsing: a path such as "--pre=cmd.exe" must never become an rg option.
        cmd += ["--", target]
        proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW)
        visible_cache: dict[str, str | None] = {}
        truncated = False
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                msg = json.loads(raw)
                if msg.get("type") not in ("match", "context"):
                    continue
                data = msg["data"]
                rel = self._rel_if_visible(cwd, data["path"].get("text", ""), visible_cache, encoding)
                if rel is None:
                    continue
                line = data.get("line_number")
                if (rel, line) in seen:
                    continue
                if msg["type"] == "match" and sum(1 for m in matches if "context" not in m) >= limit:
                    truncated = True
                    break
                seen.add((rel, line))
                text = data["lines"].get("text", "").rstrip("\r\n")
                entry = {"path": rel, "line": line, "text": text}
                if msg["type"] == "context":
                    entry["context"] = True
                matches.append(entry)
        finally:
            if truncated:
                proc.kill()
            _, err = proc.communicate()
        if proc.returncode == 2 and b"regex parse error" in err:
            raise ToolError("正規表現が正しくありません")
        return truncated

    def _fallback(self, pattern, cwd, target, glob, ignore_case, context, limit, matches, seen):
        """Portable search when rg cannot be launched; enumerate with the same safe walk."""
        try:
            regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error:
            raise ToolError("正規表現が正しくありません") from None
        base = cwd / target
        files = (p for p, _ in self.paths.walk(base, "read")) if base.is_dir() else iter([base])
        display = self.paths.display_formatter()
        count = sum("context" not in match for match in matches)
        context = max(0, int(context))
        for path in files:
            relative = path.relative_to(cwd).as_posix()
            if glob:
                negative = glob.startswith("!")
                pattern_glob = glob[1:] if negative else glob
                hit = _glob_matches(pattern_glob, relative)
                if negative:
                    parts = relative.split("/")
                    hit = hit or any(_glob_matches(pattern_glob, "/".join(parts[:i]))
                                     for i in range(1, len(parts)))
                if hit == negative:
                    continue
            try:
                data = path.read_bytes()
                encoding = detect_encoding(data[:65536])
                if encoding is None:
                    continue
                lines = data.decode(encoding, errors="replace").splitlines()
            except OSError:
                continue
            rel = display(str(path))
            hits = {i for i, line in enumerate(lines) if regex.search(line)}
            selected = set()
            for i in hits:
                selected.update(range(max(0, i - context), min(len(lines), i + context + 1)))
            for i in sorted(selected):
                if (rel, i + 1) in seen:
                    continue
                if i in hits:
                    if count >= limit:
                        return True
                    count += 1
                entry = {"path": rel, "line": i + 1, "text": lines[i]}
                if i not in hits:
                    entry["context"] = True
                matches.append(entry)
                seen.add((rel, i + 1))
        return False

    def _rel_if_visible(self, cwd: Path, rg_path: str, cache: dict, encoding: str | None) -> str | None:
        key = (str(cwd), rg_path)
        if key in cache:
            return cache[key]
        p = (cwd / rg_path)
        rel: str | None = None
        if self.paths.discoverable(p):
            if encoding is None or sniff_file(p.resolve()) == "cp932":
                rel = self.paths.to_rel(p)
        cache[key] = rel
        return rel
