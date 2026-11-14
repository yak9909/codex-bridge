"""grep tool backed by ripgrep, filtered by the policy."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .fs_read import sniff_file
from .paths import SafePaths, ToolError, not_found
from .policy import PolicyStore

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class SearchTools:
    def __init__(self, paths: SafePaths, policy: PolicyStore, rg_path: Path) -> None:
        self.paths = paths
        self.policy = policy
        self.rg_path = Path(rg_path)

    def grep(self, pattern: str, path: str | None = None, glob: str | None = None,
             ignore_case: bool = False, context: int = 0, limit: int = 500) -> dict:
        """Without a path: every shared folder."""
        if path:
            base = self.paths.resolve(path, "read")
            if not base.exists():
                raise not_found(path)
            targets = [(base, ".")] if base.is_dir() else [(base.parent, base.name)]
        else:
            self.paths.check_running()
            targets = [(r, ".") for r in self.paths.root_paths()]
        limit = max(1, min(int(limit), self.policy.current().limits.grep_max_matches))
        matches: list[dict] = []
        seen: set[tuple[str, int]] = set()
        truncated = False
        for cwd, target in targets:
            truncated = self._run(pattern, cwd, target, glob, ignore_case, context, limit, None, matches, seen)
            if not truncated and not pattern.isascii():
                # Shift_JIS files only match when ripgrep decodes them as such.
                truncated = self._run(pattern, cwd, target, glob, ignore_case, context, limit, "shift_jis",
                                      matches, seen)
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

    def _rel_if_visible(self, cwd: Path, rg_path: str, cache: dict, encoding: str | None) -> str | None:
        key = (str(cwd), rg_path)
        if key in cache:
            return cache[key]
        p = (cwd / rg_path)
        rel: str | None = None
        if self.paths.visible(p):
            if encoding is None or sniff_file(p.resolve()) == "cp932":
                rel = self.paths.to_rel(p)
        cache[key] = rel
        return rel
