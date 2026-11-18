"""The lender's settings (design 4.3 - 4.8 and 6): usage limits, which Codex features are stopped, disposable
sessions, where the data and the work folder are, how long the work log is kept.

Only the lender changes these (on the agent's own page): nothing in a host message writes to the store.
`SettingsStore` keeps the JSON; `SettingsApplier` puts a change to work (limits at once, a restart of Codex when
its launch arguments or work folder changed and nothing is running); `move_data_root` moves the data.
"""

from __future__ import annotations

import asyncio
import copy
import filecmp
import json
import math
import os
import secrets
import shutil
import stat
import threading
from pathlib import Path
from typing import Awaitable, Callable

from ..common.protocol import EFFORT_ORDER, PROTOCOL
from .models import offered_models

FEATURES = ("web_search", "image_generation", "multi_agent", "goals")
# Shown on the settings page. Sub-agents cannot really be switched off in Codex 0.167 (`--disable multi_agent`
# and `agents.max_depth=0` still let a normal thread spawn them): the setting is a request, and says so.
FEATURE_INFO = {
    "web_search": {"label": "Web 検索・Web の取得", "reason": "使用量。使わせる側のアカウントで外部にアクセスする",
                   "best_effort": False, "note": ""},
    "image_generation": {"label": "画像生成", "reason": "使用量", "best_effort": False, "note": ""},
    "multi_agent": {"label": "サブエージェント（複数エージェント）", "reason": "使用量（並行して消費する）",
                    "best_effort": True,
                    "note": "ベストエフォート: 今の Codex にはサブエージェントを確実に止める設定がありません。"
                            "「使わないで」と頼む設定にとどまり、使われることがあります。"},
    "goals": {"label": "目標（goal）機能", "reason": "使用量（長時間の自走）", "best_effort": False, "note": ""},
}
MULTI_AGENT_NOTE = "サブエージェント（ほかのエージェントを起動する機能）は使わないでください。作業はあなた自身で行ってください。"
LOCATION_FILE = "location.txt"
WORKDIR_MEMO = "workdir.txt"
MAX_TIMEOUT_MIN = 24 * 60
MAX_RETENTION_DAYS = 3650
SHARED_KEYS = frozenset({"limits", "workdir", "log_retention_days"})
CONNECTION_KEYS = frozenset({"features", "pre_approval", "disposable", "log_visibility", "allowed_models", "max_effort"})


def defaults() -> dict:
    return {"limits": {"five_hour": 80, "weekly": 80, "force": False},
            "features": {f: False for f in FEATURES},  # False = stopped
            "pre_approval": {"new_task": False, "followup": False, "timeout_min": 30},
            "disposable": False, "workdir": None, "log_retention_days": 30,
            "allowed_models": None, "max_effort": "xhigh"}


# ---- validation: each returns the value to keep, or raises ValueError --------------------------------
def _percent(v):
    if v is None:
        return None  # no limit of its own: only the borrower's threshold applies
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 1 <= v <= 100:
        raise ValueError("上限は 1〜100 の数で指定してください")
    return int(v) if v == int(v) else v


def _flag(v):
    if not isinstance(v, bool):
        raise ValueError("オン・オフ（true / false）で指定してください")
    return v


def _whole(lo: int, hi: int, what: str):
    def check(v):
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            raise ValueError(f"{what}は {lo}〜{hi} の整数で指定してください")
        return v
    return check


def _timeout_min(v):
    if v is None:
        return None  # no expiry; keep this distinct from a finite number of minutes
    return _whole(1, MAX_TIMEOUT_MIN, "待ち時間（分）")(v)


def _folder(v):
    if v is None:
        return None
    if not isinstance(v, str) or not v.strip() or not Path(v.strip()).is_absolute():
        raise ValueError("作業フォルダは、ドライブ名から始まるフォルダのパスで指定してください")
    return v.strip()


def _models(v):
    if v is None:
        return None
    if (not isinstance(v, list) or len(v) > 200
            or any(not isinstance(model, str) or not model for model in v) or len(set(v)) != len(v)):
        raise ValueError("使わせるモデルは、モデルの名前の一覧で指定してください")
    return list(v)


def _effort(v):
    if not isinstance(v, str) or v not in EFFORT_ORDER:
        raise ValueError("推論の強さの上限は、" + "・".join(EFFORT_ORDER) + " のどれかです")
    return v


SECTIONS = {
    "limits": {"five_hour": _percent, "weekly": _percent, "force": _flag},
    "features": {f: _flag for f in FEATURES},
    "pre_approval": {"new_task": _flag, "followup": _flag, "timeout_min": _timeout_min},
}


def expand_legacy_pre_approval(value):
    """A pre_approval object with the v0.2 `enabled` flag turned into `new_task` and `followup` (an explicit new key
    wins). Anything that is not an object is returned as it is, for the caller to refuse or skip."""
    if not isinstance(value, dict) or "enabled" not in value:
        return value
    out = {k: v for k, v in value.items() if k != "enabled"}
    if not isinstance(value["enabled"], bool):
        out["new_task"] = out["followup"] = value["enabled"]  # not a flag: validated (and refused) as such below
        return out
    out.setdefault("new_task", value["enabled"])
    out.setdefault("followup", value["enabled"])
    return out
SCALARS = {"disposable": _flag, "workdir": _folder,
           "log_retention_days": _whole(1, MAX_RETENTION_DAYS, "記録の保管日数"),
           "allowed_models": _models, "max_effort": _effort}


def _merge(base: dict, patch, strict: bool) -> dict:
    """base with the patch applied. strict: any bad value or unknown key is a ValueError; else it is skipped."""
    out = copy.deepcopy(base)
    if not isinstance(patch, dict):
        if strict:
            raise ValueError("設定は JSON の object で指定してください")
        return out
    for key, value in patch.items():
        if key in SECTIONS:
            if not isinstance(value, dict):
                if strict:
                    raise ValueError(f"{key} は object で指定してください")
                continue
            if key == "pre_approval":
                value = expand_legacy_pre_approval(value)
            for name, v in value.items():
                _put(out[key], name, v, SECTIONS[key].get(name), f"{key}.{name}", strict)
        else:
            _put(out, key, value, SCALARS.get(key), key, strict)
    return out


def _put(target: dict, name: str, value, check, label: str, strict: bool) -> None:
    if check is None:
        if strict:
            raise ValueError(f"知らない設定です: {label}")
        return
    try:
        target[name] = check(value)
    except ValueError:
        if strict:
            raise


class SettingsStore:
    """settings.json: a missing or damaged file reads as the defaults, a bad field falls back by itself."""

    def __init__(self, path: Path, legacy_limits: Path | None = None, *, shared: bool = False) -> None:
        self.path = Path(path)
        self.legacy_limits = Path(legacy_limits) if legacy_limits else None  # v0.1's limits.json
        self._lock = threading.Lock()
        self.shared = shared

    def _load(self) -> dict:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raw = {}
            if self.legacy_limits is not None:  # until the first save, a v0.1 lender keeps the limits it chose
                try:
                    raw = {"limits": json.loads(self.legacy_limits.read_text(encoding="utf-8"))}
                except (OSError, ValueError):
                    pass
        except (OSError, ValueError):
            raw = {}
        loaded = _merge(defaults(), raw, strict=False)
        return {k: loaded[k] for k in SHARED_KEYS} if self.shared else loaded

    def get(self) -> dict:
        with self._lock:
            return self._load()

    def update(self, patch: dict) -> dict:
        """Validated as a whole before anything is written; returns all the settings."""
        with self._lock:
            if self.shared and (not isinstance(patch, dict) or set(patch) - SHARED_KEYS):
                raise ValueError("共通の設定は limits、workdir、log_retention_days だけです")
            new = _merge({**defaults(), **self._load()}, patch, strict=True)
            if self.shared:
                new = {k: new[k] for k in SHARED_KEYS}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(new, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
            return new


# ---- what the settings mean for Codex and for the host -----------------------------------------------
def feature_args(features: dict | None = None) -> list[str]:
    """app-server arguments that stop the features switched off. Names verified on codex 0.167.0; the
    bridge's own tools keep working with all of them stopped."""
    f = {**defaults()["features"], **(features or {})}
    args: list[str] = []
    if not f["web_search"]:
        args += ["-c", 'web_search="disabled"']
    if not f["image_generation"]:
        args += ["--disable", "image_generation"]
    if not f["goals"]:
        args += ["--disable", "goals"]
    if not f["multi_agent"]:
        args += ["-c", "agents.max_depth=0"]  # best effort: see FEATURE_INFO
    return args


def feature_instructions(features: dict | None = None) -> str:
    """Added to the developer instructions of a thread."""
    return "" if (features or {}).get("multi_agent") else MULTI_AGENT_NOTE


def lender_view(settings: dict, catalog: list[dict] | None) -> dict:
    """The lender's own settings as the borrower's screen shows them (read-only): what hello and lender_models carry."""
    allowed = settings["allowed_models"]
    return {"features": {f: bool(settings["features"][f]) for f in FEATURES},
            "pre_approval": {"new_task": settings["pre_approval"]["new_task"],
                             "followup": settings["pre_approval"]["followup"],
                             "timeout_min": settings["pre_approval"]["timeout_min"]},
            "resumable": not settings["disposable"],
            "over_limit": "stop" if settings["limits"]["force"] else "report",
            "max_effort": settings["max_effort"],
            "models_all": None if catalog is None else
            [{"model": m["model"], "allowed": allowed is None or m["model"] in allowed,
              "is_default": m["is_default"] is True} for m in catalog]}


def models_fields(settings: dict, catalog: list[dict] | None) -> dict:
    return {"models": offered_models(catalog, settings["allowed_models"], settings["max_effort"]),
            "max_effort": settings["max_effort"], "lender_view": lender_view(settings, catalog)}


def hello_fields(settings: dict, catalog: list[dict] | None = None) -> dict:
    """What `hello` says about this agent (design 6): the host reads capabilities before routing a task."""
    return {"protocol": PROTOCOL,
            "capabilities": {"resumable": not settings["disposable"],
                             "pre_approval": settings["pre_approval"]["new_task"] or settings["pre_approval"]["followup"],
                             "model_select": True},
            "limits": dict(settings["limits"]), **models_fields(settings, catalog)}


# ---- the work folder (design 4.8) ----------------------------------------------------------------------
def program_data_base() -> Path:
    return Path(os.environ.get("ProgramData") or r"C:\ProgramData") / "codex-bridge-agent"


def default_workdir(root: Path, base: Path | None = None) -> Path:
    """An empty folder for Codex to work in, outside the user's profile (a path with the user name would reach the
    borrower as part of Codex's context). One per installation, remembered in the data location; if the shared
    place cannot be used, `workdir` inside the data location (not remembered: it moves with the data)."""
    root, base = Path(root), Path(base) if base else program_data_base()
    try:
        known = Path(root.joinpath(WORKDIR_MEMO).read_text(encoding="utf-8").strip())
        if known.parent == base and known.is_dir():
            return known
    except (OSError, ValueError):
        pass
    for _ in range(5):
        wd = base / f"work-{secrets.token_hex(4)}"
        try:
            base.mkdir(parents=True, exist_ok=True)
            wd.mkdir()
        except FileExistsError:
            continue
        except OSError:
            break
        try:
            root.mkdir(parents=True, exist_ok=True)
            (root / WORKDIR_MEMO).write_text(str(wd), encoding="utf-8")
        except OSError:
            pass
        return wd
    wd = root / "workdir"
    wd.mkdir(parents=True, exist_ok=True)
    return wd


def resolve_workdir(settings: dict, root: Path, base: Path | None = None) -> Path:
    """The folder the lender chose, if it can be used; else the default one."""
    if settings.get("workdir"):
        p = Path(settings["workdir"])
        try:
            p.mkdir(parents=True, exist_ok=True)
            return p
        except OSError:
            pass
    return default_workdir(root, base)


# ---- where the data is (design 4.7) ------------------------------------------------------------------------
def default_home() -> Path:
    """Where location.txt lives, and the data too until it is moved."""
    base = os.environ.get("CODEX_BRIDGE_AGENT_HOME") or Path(os.environ["LOCALAPPDATA"]) / "codex-bridge-agent"
    return Path(base)


def read_location(home: Path) -> Path | None:
    try:
        text = (Path(home) / LOCATION_FILE).read_text(encoding="utf-8-sig").strip()
    except (OSError, ValueError):
        return None
    p = Path(text) if text else None
    return p if p is not None and p.is_absolute() else None


def _write_location(home: Path, new: Path) -> None:
    """All at once (temp file, then replace): location.txt is either the old one or the new one."""
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    tmp = home / (LOCATION_FILE + ".tmp")
    tmp.write_text(str(new), encoding="utf-8")
    os.replace(tmp, home / LOCATION_FILE)


def _norm(p: Path) -> Path:
    return Path(os.path.normcase(os.path.realpath(p)))


def _inside(child: Path, parent: Path) -> bool:
    return child != parent and parent in child.parents


def _copy_tree(src: Path, dst: Path, skip: frozenset) -> None:
    def ignore(d, names):
        ignored = [n for n in names if n in skip] if Path(d) == Path(src) else []
        for n in names:
            if n not in ignored and os.path.isjunction(Path(d) / n):
                # copytree(symlinks=True) still follows Windows junctions. Refuse instead of copying
                # unrelated data; the move rolls back and leaves the original link in place.
                raise ValueError("ジャンクションを含むため保存場所を移せません。リンクを外してからお試しください")
        return ignored

    shutil.copytree(src, dst, dirs_exist_ok=True, symlinks=True, ignore=ignore)


def _verify_tree(src: Path, dst: Path, skip: frozenset) -> list[str]:
    """What is not in dst exactly as in src (every folder, every file, byte for byte); empty when all is."""
    problems: list[str] = []
    filecmp.clear_cache()
    for dirpath, dirnames, filenames in os.walk(src):
        here = Path(dirpath)
        rel = here.relative_to(src)
        if rel == Path("."):
            dirnames[:] = [d for d in dirnames if d not in skip]
            filenames = [f for f in filenames if f not in skip]
        for d in dirnames:
            if not (dst / rel / d).is_dir():
                problems.append(f"フォルダがありません: {rel / d}")
        for f in filenames:
            copied = dst / rel / f
            if not copied.is_file():
                problems.append(f"ファイルがありません: {rel / f}")
            elif not filecmp.cmp(here / f, copied, shallow=False):
                problems.append(f"内容が違います: {rel / f}")
    return problems


def _remove_tree(p: Path, left: list[Path]) -> None:
    if os.path.isjunction(p):
        try:
            p.rmdir()  # unlink the junction itself; never chmod or descend into its target
        except OSError:
            left.append(p)
        return
    if p.is_dir() and not p.is_symlink():
        for child in list(p.iterdir()):
            _remove_tree(child, left)
        try:
            p.rmdir()
        except OSError:
            if not any(p in l.parents for l in left):
                left.append(p)
        return
    try:
        p.unlink()
    except OSError:
        try:
            os.chmod(p, stat.S_IWRITE)
            p.unlink()
        except OSError:
            left.append(p)  # in use (Windows keeps an open file): it stays and is reported


def _remove_original(src: Path, skip: frozenset) -> list[Path]:
    """Delete what was moved (not what `skip` names); what could not be deleted is returned."""
    left: list[Path] = []
    for child in list(Path(src).iterdir()):
        if child.name not in skip:
            _remove_tree(child, left)
    if not skip:
        try:
            Path(src).rmdir()
        except OSError:
            if not left:
                left.append(Path(src))
    return left


def _check_destination(new: Path, current: Path, home: Path) -> None:
    if not new.is_absolute():
        raise ValueError("保存場所は、ドライブ名から始まるフォルダのパスで指定してください")
    n, c, h = _norm(new), _norm(current), _norm(home)
    if n == c:
        raise ValueError("今の保存場所と同じです")
    if _inside(n, c):
        raise ValueError("今の保存場所の中には移せません")
    if _inside(c, n):
        raise ValueError("今の保存場所を含むフォルダには移せません（そのフォルダの中身まで消えてしまうため）")
    if c != h and _inside(h, c):
        raise ValueError("今の保存場所の中に location.txt の場所があるため、移せません")
    if new.exists():
        if not new.is_dir():
            raise ValueError("指定の場所はフォルダではありません")
        extra = {e.name for e in new.iterdir()} - ({LOCATION_FILE} if n == h else set())
        if extra:
            raise ValueError("空のフォルダを指定してください（中身のあるフォルダには移せません）")


def move_data_root(new: Path, *, current: Path | None = None, home: Path | None = None,
                   copy=_copy_tree, verify=_verify_tree, write_location=_write_location,
                   remove=_remove_original) -> list[Path]:
    """Move all the data to `new`: copy, check the copy byte for byte, switch location.txt, delete the original.

    Nothing is lost whatever fails: until location.txt is switched the original is the one in use and is not
    touched (a failure removes the partial copy again and raises). After the switch the new place is complete, so
    a file that cannot be deleted (codex.exe while Codex runs) is no failure: the leftovers are returned.
    The caller must be idle (nothing writing to the old place) and start the exe again afterwards, so that
    its stores open the new place. The keyword arguments are the steps, for tests to make fail."""
    home = Path(home) if home else default_home()
    current = Path(current) if current else (read_location(home) or home)
    new = Path(new)
    _check_destination(new, current, home)
    if not current.is_dir():
        raise ValueError("今の保存場所が見つかりません")
    skip = frozenset({LOCATION_FILE}) if _norm(current) == _norm(home) else frozenset()  # location.txt stays put
    top = new  # the first folder of the destination that does not exist yet: removed again on failure
    while top.parent != top and not top.exists() and not top.parent.exists():
        top = top.parent
    created = not new.exists()
    already = {e.name for e in new.iterdir()} if new.exists() else set()
    loc = home / LOCATION_FILE
    old_location = loc.read_bytes() if loc.exists() else None
    try:
        new.mkdir(parents=True, exist_ok=True)
        copy(current, new, skip)
        problems = verify(current, new, skip)
        if problems:
            raise OSError("コピーが元と一致しません: " + "; ".join(problems[:3]))
        write_location(home, new)
    except BaseException:
        _undo(new, top if created else None, already, loc, old_location)
        raise
    try:
        return list(remove(current, skip))
    except Exception:
        return [current]


def _undo(new: Path, created_top: Path | None, already: set, loc: Path, old_location: bytes | None) -> None:
    """Back to how it was before the move began (best effort: the original was never touched)."""
    try:
        if old_location is None:
            loc.unlink(missing_ok=True)
        elif not loc.exists() or loc.read_bytes() != old_location:
            loc.write_bytes(old_location)
    except OSError:
        pass
    if created_top is not None:
        shutil.rmtree(created_top, ignore_errors=True)
        return
    try:
        for e in new.iterdir():
            if e.name not in already:
                shutil.rmtree(e, ignore_errors=True) if e.is_dir() else e.unlink(missing_ok=True)
    except OSError:
        pass


# ---- putting a change to work --------------------------------------------------------------------------------
class SettingsApplier:
    """Limits, retention and disposable take effect at once. Codex is restarted when its launch arguments or work
    folder changed, but only when no task is running (otherwise `settle()`, called now and then, does it as soon as
    the task is over). What hello says changed: hello is sent again."""

    def __init__(self, store: SettingsStore, app, runner, root: Path, *,
                 refresh_hello: Callable[[], Awaitable[None]] | None = None, base: Path | None = None,
                 on_log: Callable[[dict], None] | None = None, move=move_data_root) -> None:
        self.store, self.app, self.runner, self.root, self.base = store, app, runner, Path(root), base
        self.refresh_hello = refresh_hello
        self.on_log = on_log or (lambda e: None)
        self._move = move
        self.moving = False  # while the data is being copied nobody may start Codex again
        self._announced = hello_fields(store.get())["capabilities"]

    def _error(self, text: str, e: Exception) -> None:
        self.on_log({"type": "error", "text": f"{text}: {type(e).__name__}: {e}"})

    async def apply(self, settings: dict) -> None:
        self.runner.apply_settings(settings)
        caps = hello_fields(settings)["capabilities"]
        if caps != self._announced and self.refresh_hello is not None:
            self._announced = caps
            try:
                await self.refresh_hello()
            except Exception as e:
                self._error("設定の変更を依頼主に知らせられませんでした", e)
        await self.settle()

    async def settle(self) -> None:
        """Restart Codex if it is not running with the saved features and work folder (and nothing is running)."""
        s = self.store.get()
        wd = resolve_workdir(s, self.root, self.base)
        if self.app.features == s["features"] and Path(self.app.workdir) == wd:
            return
        async with self.runner.exclusive() as idle:
            if not idle:
                return
            try:
                await self.app.restart(features=s["features"], workdir=wd)
            except Exception as e:  # saved anyway; the next settle() tries again
                self._error("Codex を再起動できませんでした", e)

    async def move_data(self, new: Path) -> list[Path]:
        """Move the data (design 4.7). Codex is stopped while it copies, because it holds bin\\codex.exe. On failure
        Codex is started again on the original and the error is raised; on success it stays stopped: the caller
        starts the exe again, which then reads the new place."""
        async with self.runner.exclusive() as idle:
            if not idle:
                raise ValueError("作業中は保存場所を変えられません。作業が終わってからもう一度お試しください")
            self.moving = True
            try:
                await self.app.stop()
                try:
                    left = await asyncio.to_thread(self._move, Path(new), current=self.root)  # from where the data is now
                except BaseException:
                    await self.app.start()  # back on the original, which was not touched
                    raise
            except BaseException:
                self.moving = False
                raise
            return left
