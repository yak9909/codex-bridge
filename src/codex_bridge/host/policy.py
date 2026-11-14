"""Permission rules (policy.yaml) with live reload."""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml

Level = Literal["hidden", "read", "write"]
ExecMode = Literal["off", "allowlist", "ask", "all"]
LEVELS = ("hidden", "read", "write")
EXEC_MODES = ("off", "allowlist", "ask", "all")


@dataclass(frozen=True)
class Limits:
    exec_timeout_sec: int = 1800
    output_max_bytes: int = 1024 * 1024
    sync_wait_sec: int = 60
    approval_timeout_sec: int = 1800
    read_max_bytes: int = 256 * 1024
    grep_max_matches: int = 500


@dataclass(frozen=True)
class Rule:
    path: str
    level: Level


def normalize_rel(rel: str) -> str:
    """Normalize a share-relative path to 'a/b/c' form ('' for the root)."""
    rel = rel.replace("\\", "/").strip()
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    return "/".join(parts)


@lru_cache(maxsize=1024)
def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Compile a '/'-separated glob. A pattern without wildcards means the path and everything below it."""
    pat = normalize_rel(pattern)
    if not any(c in pat for c in "*?["):
        return re.compile(re.escape(pat) + r"(?:/.*)?", re.IGNORECASE)
    out = []
    i = 0
    while i < len(pat):
        if pat.startswith("**/", i):
            out.append(r"(?:.*/)?")
            i += 3
        elif pat.startswith("/**", i) and i + 3 == len(pat):
            out.append(r"(?:/.*)?")
            i += 3
        elif pat.startswith("**", i):
            out.append(r".*")
            i += 2
        elif pat[i] == "*":
            out.append(r"[^/]*")
            i += 1
        elif pat[i] == "?":
            out.append(r"[^/]")
            i += 1
        else:
            out.append(re.escape(pat[i]))
            i += 1
    return re.compile("".join(out), re.IGNORECASE)


def glob_match(pattern: str, rel: str) -> bool:
    return glob_to_regex(pattern).fullmatch(normalize_rel(rel)) is not None


@dataclass(frozen=True)
class Policy:
    default: Level = "read"
    rules: tuple[Rule, ...] = ()
    exec_mode: ExecMode = "allowlist"
    allow_patterns: list[str] = field(default_factory=list)
    paused: bool = False
    confirm_modified_scripts: bool = False
    delete_needs_approval: bool = True
    limits: Limits = Limits()

    def _rule_level(self, rel: str) -> Level | None:
        found: Level | None = None
        for rule in self.rules:
            if glob_match(rule.path, rel):
                found = rule.level
        return found

    def level_for(self, rel: str) -> Level:
        rel = normalize_rel(rel)
        if rel == "":
            return self.default if self.default != "hidden" else "read"
        parts = rel.split("/")
        # An explicitly hidden ancestor hides everything below it.
        for i in range(1, len(parts)):
            if self._rule_level("/".join(parts[:i])) == "hidden":
                return "hidden"
        level = self._rule_level(rel)
        return level if level is not None else self.default


def parse_policy(data: dict | None) -> Policy:
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError("policy must be a mapping")
    default = data.get("default", "read")
    if default not in LEVELS:
        raise ValueError(f"default must be one of {LEVELS}")
    rules = []
    for r in data.get("rules") or []:
        if not isinstance(r, dict) or "path" not in r or r.get("level") not in LEVELS:
            raise ValueError(f"bad rule: {r!r}")
        rules.append(Rule(str(r["path"]), r["level"]))
    ex = data.get("exec") or {}
    mode = ex.get("mode", "allowlist")
    if mode is False:  # YAML 1.1 reads a bare `off` as false
        mode = "off"
    if mode not in EXEC_MODES:
        raise ValueError(f"exec.mode must be one of {EXEC_MODES}")
    allow = ex.get("allow") or []
    if not isinstance(allow, list) or not all(isinstance(a, str) for a in allow):
        raise ValueError("exec.allow must be a list of strings")
    lim = data.get("limits") or {}
    try:
        limits = Limits(**{k: int(v) for k, v in lim.items()})
    except TypeError as e:
        raise ValueError(f"bad limits: {e}") from e
    return Policy(
        default=default,
        rules=tuple(rules),
        exec_mode=mode,
        allow_patterns=list(allow),
        paused=bool(data.get("paused", False)),
        confirm_modified_scripts=bool(ex.get("confirm_modified_scripts", False)),
        delete_needs_approval=bool(data.get("delete_needs_approval", True)),
        limits=limits,
    )


class PolicyStore:
    """Loads policy.yaml and re-reads it whenever the file changes."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._stamp: tuple[int, int] | None = None
        self._policy = Policy(exec_mode="off")
        self._raw: dict = {"default": "read", "exec": {"mode": "off"}}
        self.last_error: str | None = None

    def current(self) -> Policy:
        with self._lock:
            try:
                st = os.stat(self.path)
            except FileNotFoundError:
                return self._policy
            stamp = (st.st_mtime_ns, st.st_size)
            if stamp != self._stamp:
                self._stamp = stamp
                try:
                    raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
                    self._policy = parse_policy(raw)
                    self._raw = raw
                    self.last_error = None
                except (yaml.YAMLError, ValueError, OSError) as e:
                    self.last_error = str(e)
            return self._policy

    def as_dict(self) -> dict:
        self.current()
        return self._raw

    def save(self, policy_dict: dict) -> None:
        parse_policy(policy_dict)  # validate first; raises ValueError
        self.save_text(yaml.safe_dump(policy_dict, allow_unicode=True, sort_keys=False))

    def save_text(self, text: str) -> None:
        try:
            raw = yaml.safe_load(text) or {}
        except yaml.YAMLError as e:
            raise ValueError(f"YAML の書式が正しくありません: {e}") from e
        parse_policy(raw)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.path)
        self.current()

    def update(self, **changes) -> None:
        """Change top-level keys (e.g. paused) or exec keys via exec_<name>."""
        raw = dict(self.as_dict())
        for k, v in changes.items():
            if k.startswith("exec_"):
                raw.setdefault("exec", {})
                raw["exec"] = {**raw["exec"], k[5:]: v}
            else:
                raw[k] = v
        self.save(raw)
