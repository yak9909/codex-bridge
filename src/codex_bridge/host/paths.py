"""Safe conversion between the paths Codex gives and real paths (several shared folders)."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path, PureWindowsPath
from typing import Literal

from .config import configured_folder
from .policy import OUTPUTS_TOP, Level, Policy, PolicyStore, glob_to_regex, normalize_rel


class ToolError(Exception):
    """An error whose message is safe to show to Codex."""


PAUSED_MSG = "停止中です（管理者が一時停止しています）"
UNSET_SHARE_MSG = "共有フォルダがまだ設定されていません。「使う」タブで選んでください。"


def not_found(rel: str) -> ToolError:
    return ToolError(f"not found: {rel}")


def _nc(p: Path | str) -> str:
    return os.path.normcase(os.path.abspath(str(p)))


def _prefix(key: str) -> str:
    return key.rstrip(os.sep) + os.sep


def _within(key: str, base: str) -> bool:
    return key == base or key.startswith(_prefix(base))


class _WalkPolicy:
    """One policy snapshot: ordinary children inherit their registered folder's level."""

    def __init__(self, policy: Policy, fallback: Path | None) -> None:
        self.policy = policy
        main = policy.main if policy.v2 else fallback
        self.main = _nc(main) if main else ""
        self.main_prefix = _prefix(self.main)
        self.folders: dict[str, Level] = {}
        if policy.v2:
            for folder in policy.folders:
                key = _nc(folder.path)
                # Policy.level_for_abs keeps the first tie; any hidden tie wins.
                if key not in self.folders or folder.level == "hidden":
                    self.folders[key] = folder.level
        self.rules = [(glob_to_regex(r.path), r.level) for r in policy.rules]

    def folder_level(self, key: str) -> Level | None:
        if not self.policy.v2:
            return self.policy.default if _within(key, self.main) else None
        hits = [(base, level) for base, level in self.folders.items() if _within(key, base)]
        if any(level == "hidden" for _, level in hits):
            return "hidden"
        return max(hits, key=lambda hit: len(hit[0]))[1] if hits else None

    def level(self, key: str, folder: Level | None) -> Level | None:
        if folder is None or (folder == "hidden" and self.policy.v2):
            return folder
        if key == self.main:
            if not self.policy.v2:
                return "read" if folder == "hidden" else folder
            rel = ""
        elif not key.startswith(self.main_prefix):
            return folder
        else:
            rel = key[len(self.main_prefix):].replace(os.sep, "/")
        if rel.split("/", 1)[0] == OUTPUTS_TOP:
            return "read"
        level = folder
        for pattern, rule_level in self.rules:
            if pattern.fullmatch(rel):
                if rule_level == "hidden":
                    return "hidden"
                level = rule_level
        return level

    def initial(self, key: str) -> tuple[Level | None, Level | None]:
        folder = self.folder_level(key)
        # v1 permits enumerating the root even when the default is hidden.
        if not self.policy.v2 and key == self.main and folder == "hidden":
            return "read", folder
        if key.startswith(self.main_prefix) and self.rules:
            rel = key[len(self.main_prefix):].replace(os.sep, "/")
            parts = rel.split("/")
            for i in range(1, len(parts)):
                if any(level == "hidden" and pattern.fullmatch("/".join(parts[:i]))
                       for pattern, level in self.rules):
                    return "hidden", folder
        return self.level(key, folder), folder


class SafePaths:
    """Paths are absolute (any registered folder) or relative to the main folder."""

    def __init__(self, root: Path | None, policy: PolicyStore, *, install_dir: Path | None = None,
                 protected: tuple[Path, ...] = ()) -> None:
        self.policy = policy
        self.install_dir = Path(install_dir).resolve() if install_dir else None
        self._protected = tuple(Path(p).resolve() for p in (policy.path.parent, *protected))
        if self.install_dir:
            self._protected += tuple(self.install_dir / d for d in ("vendor", "src", ".venv", "scripts"))
        self._protected_keys = tuple(_nc(p) for p in self._protected)
        self._protected_prefixes = tuple(_prefix(key) for key in self._protected_keys)
        self._install_key = _nc(self.install_dir) if self.install_dir else None
        self._install_prefix = _prefix(self._install_key) if self._install_key else None
        self.set_root(root)

    def set_root(self, root: Path | None) -> None:
        """Main folder used when the policy has none of its own (v1 policies)."""
        self._fallback_root = configured_folder(Path(root).resolve()) if root is not None else None

    @property
    def root(self) -> Path | None:
        pol = self.policy.current()
        return configured_folder(pol.main if pol.v2 else self._fallback_root)

    # ---- levels ----------------------------------------------------------
    def _under(self, p: Path, base: Path) -> str | None:
        np, nb = _nc(p), _nc(base)
        if np == nb:
            return ""
        prefix = nb.rstrip(os.sep) + os.sep
        return np[len(prefix):].replace(os.sep, "/") if np.startswith(prefix) else None

    def level_of(self, p: Path) -> Level | None:
        """Level of an absolute path; None if it is in no shared folder."""
        # These exclusions outrank every policy, including a manually edited broad folder rule.
        for candidate in (Path(p), Path(p).resolve(strict=False)):
            if self._blocked(_nc(candidate), candidate.is_dir() if self._install_key else False):
                return None
        return self._policy_level(self.policy.current(), p)

    def _policy_level(self, pol: Policy, p: Path) -> Level | None:
        if pol.v2:
            return pol.level_for_abs(p)
        if self._fallback_root is None:
            return None
        rel = self._under(p, self._fallback_root)
        return None if rel is None else pol.level_for(rel)

    def _blocked(self, key: str, is_dir: bool) -> bool:
        return (key in self._protected_keys or key.startswith(self._protected_prefixes)
                or (not is_dir and self._install_prefix is not None
                    and key.startswith(self._install_prefix)
                    and os.sep not in key[len(self._install_prefix):]))

    @staticmethod
    def _outputs(key: str) -> bool:
        return OUTPUTS_TOP in key.lower().split(os.sep)

    def check_configured(self) -> None:
        if self.root is None:
            raise ToolError(UNSET_SHARE_MSG)

    def check_running(self) -> None:
        self.check_configured()
        if self.policy.current().paused:
            raise ToolError(PAUSED_MSG)

    # ---- display ---------------------------------------------------------
    def display(self, p: Path) -> str:
        """Relative inside the main folder, absolute ('/' separated) elsewhere."""
        main = self.root
        rel = self._under(p, main) if main else None
        return rel if rel is not None else Path(os.path.abspath(p)).as_posix()

    to_rel = display  # older name used throughout the tools

    def display_formatter(self) -> Callable[[str], str]:
        """Snapshot the main folder once when formatting absolute scandir paths."""
        root = self.root
        if root is None:
            return lambda absolute: absolute.replace(os.sep, "/")
        main = _nc(root)
        prefix = _prefix(main)

        def display(absolute: str) -> str:
            key = os.path.normcase(absolute)
            if key == main:
                return ""
            if key.startswith(prefix):
                return key[len(prefix):].replace(os.sep, "/")
            return absolute.replace(os.sep, "/")

        return display

    def roots(self) -> list[dict]:
        """Registered folders Codex may see (hidden ones are left out)."""
        pol = self.policy.current()
        if not pol.v2:
            root = self.root
            return [{"path": root.as_posix(), "level": pol.level_for(""), "main": True}] if root else []
        out, main = [], _nc(pol.main) if pol.main else None
        for f in pol.folders:
            if not configured_folder(f.path) or f.level == "hidden" or self.level_of(f.path) in (None, "hidden"):
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
            requested_base = self.root  # read once: the folder may vanish after check_running
            if requested_base is None:
                raise ToolError(UNSET_SHARE_MSG)
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
        rules = _WalkPolicy(self.policy.current(), self._fallback_root)
        key, real_key = _nc(real), _nc(real.resolve(strict=False))
        message = f"{action}できません: フォルダの中に、書き込みが許可されていない場所があります"

        def scan(directory, key, real_key, folder, real_folder):
            prefix, real_prefix = _prefix(key), _prefix(real_key)
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        name = os.path.normcase(entry.name)
                        child_key, child_real = prefix + name, real_prefix + name
                        child_folder = rules.folders.get(child_key, folder)
                        child_real_folder = rules.folders.get(child_real, real_folder)
                        level = rules.level(child_key, child_folder)
                        linked = entry.is_symlink() or entry.is_junction()
                        is_dir = entry.is_dir()
                        if linked:
                            child_real = _nc(Path(entry.path).resolve(strict=False))
                        if level != "write" or self._blocked(child_key, is_dir) or self._blocked(child_real, is_dir):
                            raise ToolError(message)
                        # Removing a link touches the link, not its destination.
                        # C1 exclusions still apply to the destination of every link.
                        if not linked:
                            if rules.level(child_real, child_real_folder) != "write":
                                raise ToolError(message)
                            if is_dir:
                                scan(entry.path, child_key, child_real, child_folder, child_real_folder)
            except OSError:
                raise ToolError(message) from None

        scan(real, key, real_key, rules.folder_level(key), rules.folder_level(real_key))

    def visible(self, p: Path) -> bool:
        """True if p (an entry found while walking) may be shown to Codex."""
        try:
            real = Path(p).resolve(strict=False)
        except OSError:
            return False
        return self._visible_real(p, real)

    def _visible_real(self, p: Path, real: Path) -> bool:
        pol = self.policy.current()
        for candidate in (Path(p), real):
            if self._blocked(_nc(candidate), candidate.is_dir() if self._install_key else False):
                return False
            if self._policy_level(pol, candidate) in (None, "hidden"):
                return False
        return True

    def discoverable(self, p: Path) -> bool:
        """Internal outputs are readable by their returned path, but cannot be enumerated/searched."""
        try:
            real = Path(p).resolve(strict=False)
        except OSError:
            return False
        return not any(self._outputs(_nc(candidate)) for candidate in (p, real)) and self._visible_real(p, real)

    def search_exclusions(self, cwd: Path) -> list[str]:
        """Case-insensitive rg globs; no enumeration of the shared file tree.

        Match-time discoverable checks remain the final guard for links, policy
        changes, and advanced rules whose wildcard prefix lies above cwd.
        """
        key = _nc(cwd)
        excludes = [f"!{OUTPUTS_TOP}", f"!{OUTPUTS_TOP}/**"]

        def literal(text: str) -> str:
            return "".join("\\" + c if c in "*?[]{}!" else c for c in text)

        def exclude_path(base: str) -> None:
            if _within(base, key):
                rel = base[len(_prefix(key)):].replace(os.sep, "/")
                if rel:
                    excludes.extend(["!/" + literal(rel), "!/" + literal(rel) + "/**"])

        for base in self._protected_keys:
            exclude_path(base)
        if self.install_dir and _within(self._install_key, key):
            # Only the install directory's immediate files are private. Its
            # other subfolders may be shared, so do not prune the entire install.
            try:
                with os.scandir(self.install_dir) as entries:
                    for entry in entries:
                        if not entry.is_dir():
                            exclude_path(_nc(entry.path))
            except OSError:
                pass  # the result guard still rejects installation files
        pol = self.policy.current()
        for folder in pol.folders:
            if folder.level == "hidden":
                exclude_path(_nc(folder.path))
        root = self.root  # read once: None when the main folder just vanished
        main = _nc(root) if root is not None else None
        for rule in pol.rules:
            if rule.level != "hidden" or main is None:
                continue
            pattern = normalize_rel(rule.path)
            if _within(main, key):
                prefix = main[len(_prefix(key)):].replace(os.sep, "/") if main != key else ""
                pattern = (literal(prefix) + "/" if prefix else "") + "".join(
                    "\\" + c if c in "[]{}!" else c for c in pattern)
            elif _within(key, main):
                prefix = key[len(_prefix(main)):].replace(os.sep, "/") + "/"
                if not pattern.lower().startswith(prefix.replace(os.sep, "/").lower()):
                    continue
                pattern = "".join("\\" + c if c in "[]{}!" else c for c in pattern[len(prefix):])
            else:
                continue
            if pattern.endswith("/**"):
                pattern = pattern[:-3]
            excludes.extend(["!/" + pattern, "!/" + pattern + "/**"])
        return excludes

    def walk(self, root: Path, need: Literal["read", "write"], *,
             policy: Policy | None = None) -> Iterator[tuple[Path, os.DirEntry]]:
        """Yield (file path, cached directory entry), in find_files order.

        Keep both the requested and real namespace. Only roots and reparse links
        need resolution; ordinary children extend the already checked real parent.
        Directory symlinks are not followed (as with os.walk); junctions are, with
        an ancestor guard to stop cycles. Internal outputs are never enumerated.
        """
        rules = _WalkPolicy(policy if policy is not None else self.policy.current(), self._fallback_root)
        root = Path(os.path.abspath(root))
        try:
            real = root.resolve(strict=False)
        except OSError:
            return
        key, real_key = _nc(root), _nc(real)
        level, folder = rules.initial(key)
        real_level, real_folder = rules.initial(real_key)

        def allowed(key, level, is_dir):
            return level not in (None, "hidden") and not self._outputs(key) and not self._blocked(key, is_dir)

        if not allowed(key, level, True) or not allowed(real_key, real_level, True):
            return

        def scan(directory, key, real_key, folder, real_folder, ancestors):
            try:
                with os.scandir(directory) as entries:
                    children = sorted(entries, key=lambda entry: entry.name)
            except OSError:
                return
            directories = []
            prefix, real_prefix = _prefix(key), _prefix(real_key)
            for entry in children:
                name = os.path.normcase(entry.name)
                if name.casefold() == OUTPUTS_TOP:
                    continue
                child_key = prefix + name
                child_real = real_prefix + name
                child_folder = rules.folders.get(child_key, folder)
                child_real_folder = rules.folders.get(child_real, real_folder)
                child_level = rules.level(child_key, child_folder)
                try:
                    symlink = entry.is_symlink()
                    linked = symlink or entry.is_junction()
                    is_dir = entry.is_dir()
                    if linked:
                        child_real = _nc(Path(entry.path).resolve(strict=False))
                        real_level, child_real_folder = rules.initial(child_real)
                    else:
                        real_level = (child_level if child_real == child_key else
                                      rules.level(child_real, child_real_folder))
                    if child_level in (None, "hidden") or self._blocked(child_key, is_dir):
                        continue
                    if child_real != child_key and not allowed(child_real, real_level, is_dir):
                        continue
                    if is_dir:
                        if not symlink and child_real not in ancestors:
                            directories.append((entry.path, child_key, child_real, child_folder, child_real_folder))
                    elif entry.is_file():
                        if need == "read" or child_level == real_level == "write":
                            yield Path(entry.path), entry
                except OSError:
                    continue
            for child in directories:
                yield from scan(*child, ancestors | {child[2]})

        yield from scan(root, key, real_key, folder, real_folder, {real_key})
