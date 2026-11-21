"""Permission rules (policy.yaml) with live reload."""

from __future__ import annotations

import os
import re
import shlex
import threading
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml

from .command_rules import lex

Level = Literal["hidden", "read", "write"]
ExecMode = Literal["off", "allowlist", "ask", "all"]
LEVELS = ("hidden", "read", "write")
EXEC_MODES = ("off", "allowlist", "ask", "all")
EXEC_MODE_STRICTNESS = ("all", "allowlist", "ask", "off")
OUTPUTS_TOP = ".codex-bridge"
DATA_SHARE_MSG = "codex-bridge のデータフォルダ（記録や合言葉の置き場）を含むフォルダは共有できません"


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


def _norm(p: Path | str) -> str:
    return os.path.normcase(os.path.abspath(str(p)))


def _rel_under(np: str, base: str) -> str | None:
    """Posix relative path of normalized np under normalized base, or None if outside."""
    if np == base:
        return ""
    prefix = base.rstrip(os.sep) + os.sep
    if not np.startswith(prefix):
        return None
    return np[len(prefix):].replace(os.sep, "/")


@dataclass(frozen=True)
class Folder:
    path: Path
    level: Level

    @property
    def key(self) -> str:
        return _norm(self.path)


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
    rules: tuple[Rule, ...] = ()  # v1: relative rules; v2: advanced (wildcard) rules relative to main
    exec_mode: ExecMode = "allowlist"
    lender_exec_modes: dict[str, str] = field(default_factory=dict)
    allow_patterns: list[str] = field(default_factory=list)
    paused: bool = False
    confirm_modified_scripts: bool = False
    delete_needs_approval: bool = True
    lender_delete_approval: dict[str, bool] = field(default_factory=dict)
    limits: Limits = Limits()
    folders: tuple[Folder, ...] = ()  # v2: absolute folders
    main: Path | None = None          # v2: relative paths are resolved from here
    v2: bool = False
    deny_patterns: list[str] = field(default_factory=list)
    ask_patterns: list[str] = field(default_factory=list)

    def exec_mode_for(self, lender: str | None) -> str:
        return self.lender_exec_modes.get(lender, self.exec_mode) if lender is not None else self.exec_mode

    def effective_exec_mode(self, lender: str | None, task_mode: str | None) -> str:
        mode = self.exec_mode_for(lender)
        return max(mode, task_mode, key=EXEC_MODE_STRICTNESS.index) if task_mode is not None else mode

    def delete_approval_for(self, lender: str | None) -> bool:
        """Whether a delete by this lender waits for approval (the global value when it has none of its own)."""
        return self.lender_delete_approval.get(lender, self.delete_needs_approval) if lender is not None \
            else self.delete_needs_approval

    def level_for_abs(self, p: Path) -> Level | None:
        """v2: the level of an absolute path, or None if no registered folder contains it."""
        np = _norm(p)
        hits = [f for f in self.folders if np == f.key or np.startswith(f.key.rstrip(os.sep) + os.sep)]
        if any(f.level == "hidden" for f in hits):
            return "hidden"
        rel = _rel_under(np, _norm(self.main)) if self.main else None
        if rel is not None and rel.split("/")[0].lower() == OUTPUTS_TOP:
            return "read"  # command outputs are always readable, never writable
        if not hits:
            return None
        level: Level = max(hits, key=lambda f: len(f.key)).level
        if rel is not None and self.rules:
            parts = rel.split("/") if rel else []
            for i in range(1, len(parts) + 1):
                if any(r.level == "hidden" and glob_match(r.path, "/".join(parts[:i])) for r in self.rules):
                    return "hidden"
            found = self._rule_level(rel)
            if found is not None:
                level = found
        return level

    def _rule_level(self, rel: str) -> Level | None:
        found: Level | None = None
        for rule in self.rules:
            if glob_match(rule.path, rel):
                found = rule.level
        return found

    def level_for(self, rel: str) -> Level:
        """Level of a path relative to the main folder (v1 semantics when no folders are registered)."""
        rel = normalize_rel(rel)
        if self.v2:
            if self.main is None:
                return "hidden"
            level = self.level_for_abs(self.main / rel if rel else self.main)
            return "hidden" if level is None else level
        if rel == "":
            return self.default if self.default != "hidden" else "read"
        parts = rel.split("/")
        if parts[0].lower() == OUTPUTS_TOP:
            return "read"  # command outputs are always readable, never writable
        # A hidden rule always wins: it hides the path and everything below it, whatever rules follow.
        for i in range(1, len(parts) + 1):
            prefix = "/".join(parts[:i])
            if any(r.level == "hidden" and glob_match(r.path, prefix) for r in self.rules):
                return "hidden"
        level = self._rule_level(rel)
        return level if level is not None else self.default


def command_patterns(value, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{label} はコマンドの型の並びで指定してください")
    for item in value:
        if lex(item, "powershell", pattern=True)[1]:
            raise ValueError(f"{label} の型の引用符が閉じていません: {item}")
    return list(value)


def parse_policy(data: dict | None) -> Policy:
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError("policy must be a mapping")
    is_v2 = "folders" in data
    default = data.get("default", "read")
    if default not in LEVELS:
        raise ValueError(f"default must be one of {LEVELS}")
    rules = []
    for r in (data.get("advanced_rules") if is_v2 else data.get("rules")) or []:
        if not isinstance(r, dict) or "path" not in r or r.get("level") not in LEVELS:
            raise ValueError(f"bad rule: {r!r}")
        rules.append(Rule(str(r["path"]), r["level"]))
    folders = []
    for f in data.get("folders") or []:
        if not isinstance(f, dict) or not f.get("path") or f.get("level") not in LEVELS:
            raise ValueError(f"bad folder: {f!r}")
        if not Path(str(f["path"])).is_absolute():
            raise ValueError(f"フォルダはフルパスで書いてください: {f['path']}")
        folders.append(Folder(Path(str(f["path"])), f["level"]))
    main = None
    if is_v2:
        if data.get("main"):
            main = Path(str(data["main"])).resolve()
        else:
            first = next((f for f in folders if f.level in ("read", "write")), None)
            main = first.path.resolve() if first else None
    ex = data.get("exec") or {}
    mode = ex.get("mode", "allowlist")
    if mode is False:  # YAML 1.1 reads a bare `off` as false
        mode = "off"
    if mode not in EXEC_MODES:
        raise ValueError(f"exec.mode must be one of {EXEC_MODES}")
    lender_modes = data.get("lender_exec_modes")
    if lender_modes is None:
        lender_modes = {}
    if not isinstance(lender_modes, dict):
        raise ValueError("lender_exec_modes は 名前: モード の形で書いてください")
    lender_exec_modes = {}
    for name, value in lender_modes.items():
        value = "off" if value is False else value
        if not isinstance(name, str) or value not in EXEC_MODES:
            raise ValueError(f"lender_exec_modes の値は {EXEC_MODES} のどれかです: {name}")
        lender_exec_modes[name] = value
    approvals = data.get("lender_delete_approval")
    if approvals is None:
        approvals = {}
    if not isinstance(approvals, dict) or not all(isinstance(n, str) and isinstance(v, bool)
                                                  for n, v in approvals.items()):
        raise ValueError("lender_delete_approval は 名前: true/false の形で書いてください")
    allow = ex.get("allow") or []
    if not isinstance(allow, list) or not all(isinstance(a, str) for a in allow):
        raise ValueError("exec.allow must be a list of strings")
    for pattern in allow:
        try:
            shlex.split(pattern, posix=False)
        except ValueError as e:
            raise ValueError(f"exec.allow の型の引用符が閉じていません: {pattern}") from e
    lim = data.get("limits") or {}
    try:
        limits = Limits(**{k: int(v) for k, v in lim.items()})
    except TypeError as e:
        raise ValueError(f"bad limits: {e}") from e
    return Policy(
        default=default,
        rules=tuple(rules),
        exec_mode=mode,
        lender_exec_modes=lender_exec_modes,
        allow_patterns=list(allow),
        deny_patterns=command_patterns(ex.get("deny"), "exec.deny"),
        ask_patterns=command_patterns(ex.get("ask"), "exec.ask"),
        paused=bool(data.get("paused", False)),
        confirm_modified_scripts=bool(ex.get("confirm_modified_scripts", False)),
        delete_needs_approval=bool(data.get("delete_needs_approval", True)),
        lender_delete_approval=dict(approvals),
        limits=limits,
        folders=tuple(folders),
        main=main,
        v2=is_v2,
    )


class PolicyStore:
    """Loads policy.yaml and re-reads it whenever the file changes."""

    def __init__(self, path: Path, share_root: Path | None = None) -> None:
        self.path = Path(path)
        self.share_root = Path(share_root) if share_root else None  # lets a v1 file be converted to v2
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
                    if self.share_root and isinstance(raw, dict) and "folders" not in raw:
                        raw = self._convert_v1(raw)
                    self._policy = parse_policy(raw)
                    self._raw = raw
                    self.last_error = None
                except (yaml.YAMLError, ValueError, OSError) as e:
                    self.last_error = str(e)
            return self._policy

    def _convert_v1(self, raw: dict) -> dict:
        """Rewrite a v0.1 policy file in the v0.2 form (absolute folders). Called with the lock held."""
        from .policy_migrate import migrate_v1
        new = migrate_v1(raw, self.share_root)
        parse_policy(new)
        backup = self.path.with_name("policy.v1.yaml")
        try:
            with backup.open("xb") as f:
                f.write(self.path.read_bytes())
        except FileExistsError:
            pass  # keep the first original, byte for byte (including comments and line endings)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(yaml.safe_dump(new, allow_unicode=True, sort_keys=False), encoding="utf-8")
        os.replace(tmp, self.path)
        st = os.stat(self.path)
        self._stamp = (st.st_mtime_ns, st.st_size)
        return new

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
        parsed = parse_policy(raw)
        if not parsed.v2 and self.share_root:
            from .policy_migrate import migrate_v1
            parsed = parse_policy(migrate_v1(raw, self.share_root))
        previous = {}
        for folder in self.current().folders:
            previous[folder.key] = max(previous.get(folder.key, "hidden"), folder.level, key=LEVELS.index)
        for folder in parsed.folders:
            # Existing broad rules must not trap the owner: removing, hiding, lowering,
            # or keeping a rule is safe. SafePaths still protects the data on every use.
            old_level = previous.get(folder.key, "hidden")
            if LEVELS.index(folder.level) > LEVELS.index(old_level):
                self.guard_shared_folder(folder.path)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.path)
        self.current()

    def guard_shared_folder(self, path: Path) -> None:
        if self.path.parent.resolve().is_relative_to(Path(path).resolve()):
            raise ValueError(DATA_SHARE_MSG)

    def problems(self) -> list[dict]:
        data_dir = self.path.parent.resolve()
        return [{"folder": folder.path.as_posix(), "reason": "contains_data_dir"}
                for folder in self.current().folders
                if folder.level in ("read", "write") and data_dir.is_relative_to(folder.path.resolve())]

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

    def set_lender_exec_mode(self, name: str, mode: str) -> None:
        if mode not in (*EXEC_MODES, "inherit"):
            raise ValueError(f"mode は {', '.join((*EXEC_MODES, 'inherit'))} のどれかです")
        if mode == "inherit":
            self.drop_lender(name)
            return
        self.update(lender_exec_modes={**self.current().lender_exec_modes, name: mode})

    def set_lender_delete_approval(self, name: str, value: bool) -> None:
        self.update(lender_delete_approval={**self.current().lender_delete_approval, name: bool(value)})

    def drop_lender(self, name: str) -> None:
        current = self.current()
        changes = {}
        if name in current.lender_exec_modes:
            changes["lender_exec_modes"] = {k: v for k, v in current.lender_exec_modes.items() if k != name}
        if name in current.lender_delete_approval:
            changes["lender_delete_approval"] = {k: v for k, v in current.lender_delete_approval.items()
                                                 if k != name}
        if changes:
            self.update(**changes)
