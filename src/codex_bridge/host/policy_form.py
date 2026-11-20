"""A simple form view of policy.yaml v2 (used by the admin page and the agents' tools).

form = {
  "main": "D:/work/project",                                    # relative paths start here
  "folders": [{"path": "D:/work/project", "level": "write"},    # absolute folders (and folders inside them)
              {"path": "D:/tools/lib", "level": "read"},
              {"path": "D:/work/project/secret", "level": "hidden"}],
  "advanced_rules": [{"path": "**/*.lock", "level": "hidden"}],  # wildcard rules, relative to main
  "exec": {"mode": "allowlist", "allow": [...], "confirm_modified_scripts": False},
  "lender_exec_modes": {"friend": "ask"},                      # only the mode varies per lender
  "delete_needs_approval": True,
  "lender_delete_approval": {"friend": False},                 # per-lender override of the line above
  "paused": False,
}
"""

from __future__ import annotations

import json
import os
from pathlib import Path, PureWindowsPath

from .policy import EXEC_MODES, LEVELS, PolicyStore, normalize_rel, parse_policy

INHERIT = "inherit"


def _abs(path: str) -> str:
    """Normalize an absolute path to 'D:/a/b' form; ValueError for relative paths."""
    raw = str(path or "").strip().strip('"')
    if not raw or not PureWindowsPath(raw).drive:
        raise ValueError(f"フォルダはフルパスで指定してください（例: D:/work/project）: {path}")
    return Path(os.path.abspath(raw)).as_posix()


def _key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def form_from_raw(raw: dict | None) -> dict:
    raw = raw or {}
    ex = raw.get("exec") or {}
    mode = ex.get("mode", "allowlist")
    return {
        "main": raw.get("main") or next((f["path"] for f in raw.get("folders") or []
                                         if f.get("level") in ("read", "write")), None),
        "folders": [{"path": Path(str(f["path"])).as_posix(), "level": f.get("level")}
                    for f in raw.get("folders") or []],
        "advanced_rules": list(raw.get("advanced_rules") or []),
        "exec": {"mode": "off" if mode is False else mode, "allow": list(ex.get("allow") or []),
                 "confirm_modified_scripts": bool(ex.get("confirm_modified_scripts", False))},
        "lender_exec_modes": {name: ("off" if v is False else v)
                              for name, v in (raw.get("lender_exec_modes") or {}).items()},
        "delete_needs_approval": bool(raw.get("delete_needs_approval", True)),
        "lender_delete_approval": {name: bool(v) for name, v in (raw.get("lender_delete_approval") or {}).items()},
        "paused": bool(raw.get("paused", False)),
    }


def _q(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def yaml_from_form(form: dict) -> str:
    """Build a commented policy.yaml from the form (raises ValueError on bad values)."""
    folders: list[tuple[str, str]] = []
    seen: set[str] = set()
    for f in form.get("folders") or []:
        level = f.get("level")
        if level == INHERIT:
            continue
        if level not in LEVELS:
            raise ValueError(f"{f.get('path')}: level は {LEVELS} のどれかです")
        path = _abs(f.get("path"))
        if _key(path) in seen:
            continue
        seen.add(_key(path))
        folders.append((path, level))  # keep the owner's order (the deepest rule wins regardless of order)
    main = form.get("main")
    if main:
        main = _abs(main)
    ex = form.get("exec") or {}
    mode = ex.get("mode", "allowlist")
    if mode not in EXEC_MODES:
        raise ValueError(f"exec.mode は {EXEC_MODES} のどれかです")
    lender_modes = parse_policy({"lender_exec_modes": form.get("lender_exec_modes")}).lender_exec_modes
    delete_approvals = parse_policy({"lender_delete_approval": form.get("lender_delete_approval")}).lender_delete_approval
    allow = [a.strip() for a in ex.get("allow") or [] if a and a.strip()]

    lines = [
        "# codex-bridge 権限の規則（管理画面の「共有フォルダと権限」で編集できます）",
        "#   hidden = 見せない（Codex からは存在しないように見える。中のフォルダもすべて）",
        "#   read   = 読むだけ",
        "#   write  = 読み書きできる",
        "# 一覧にない場所は、Codex からは見えません。",
        "",
        "# 相対パスで指定されたときの起点（メインのフォルダ）",
        f"main: {_q(main)}" if main else "main: null",
        "",
        "# 共有フォルダ（フルパス）。フォルダの中に別の規則を書くと、深いほうが優先。hidden は常に優先",
        "folders:" if folders else "folders: []",
    ]
    lines += [f"  - {{path: {_q(p)}, level: {lv}}}" for p, lv in folders]
    adv = form.get("advanced_rules") or []
    if adv:
        lines += ["", "# ワイルドカードを使った細かい規則（メインのフォルダからの相対）", "advanced_rules:"]
        for r in adv:
            if r.get("level") not in LEVELS:
                raise ValueError(f"{r.get('path')}: level は {LEVELS} のどれかです")
            lines.append(f"  - {{path: {_q(str(r['path']))}, level: {r['level']}}}")
    lines += [
        "",
        "# コマンド実行",
        "#   off       = 実行しない",
        "#   allowlist = allow に合うコマンドは自動で実行、それ以外は承認待ち",
        "#   ask       = 毎回承認待ち",
        "#   all       = すべて自動で実行（おすすめしません）",
        "exec:",
        f"  mode: {_q(mode)}",
        "  # 自動で実行してよいコマンドの型（最後の * は「任意の引数」、** は「下のフォルダすべて」）",
        "  allow:" if allow else "  allow: []",
    ]
    lines += [f"    - {_q(a)}" for a in allow]
    lines += [
        "  # true にすると、作業中に書き換えたスクリプトの実行は承認待ちになる",
        f"  confirm_modified_scripts: {str(bool(ex.get('confirm_modified_scripts'))).lower()}",
    ]
    if lender_modes:
        lines += ["", "# Codex を貸している人ごとのコマンド実行のモード（書いていない人は、上の exec.mode と同じ）",
                  "lender_exec_modes:"]
        lines += [f"  {_q(name)}: {_q(mode)}" for name, mode in lender_modes.items()]
    lines += [
        "",
        "# 削除するときは承認待ちにする",
        f"delete_needs_approval: {str(bool(form.get('delete_needs_approval', True))).lower()}",
    ]
    if delete_approvals:
        lines += ["# Codex を貸している人ごとの削除の承認（書いていない人は、上の delete_needs_approval と同じ）",
                  "lender_delete_approval:"]
        lines += [f"  {_q(name)}: {str(value).lower()}" for name, value in delete_approvals.items()]
    lines += [
        "",
        "# 全体の一時停止（true の間、Codex の道具はすべて止まる）",
        f"paused: {str(bool(form.get('paused', False))).lower()}",
        "",
    ]
    return "\n".join(lines)


def save_form(store: PolicyStore, form: dict) -> None:
    current = form_from_raw(store.as_dict())
    for key in ("lender_exec_modes", "lender_delete_approval"):
        if key not in form:
            form = {**form, key: current[key]}
    store.save_text(yaml_from_form(form))


def _form(store: PolicyStore) -> dict:
    return form_from_raw(store.as_dict())


def set_folder_level(store: PolicyStore, path: str, level: str) -> dict:
    """Set (or with level='inherit', remove) the rule for one folder. Relative paths start at main."""
    if level not in (*LEVELS, INHERIT):
        raise ValueError("level は hidden / read / write / inherit のどれかです")
    form = _form(store)
    if PureWindowsPath(str(path)).drive:
        target = _abs(path)
    else:
        rel = normalize_rel(path)
        if not rel or not form["main"]:
            raise ValueError("フォルダを指定してください")
        target = (Path(form["main"]) / rel).as_posix()
    form["folders"] = [f for f in form["folders"] if _key(f["path"]) != _key(target)]
    if level != INHERIT:
        form["folders"].append({"path": target, "level": level})
    save_form(store, form)
    return form


def add_folder(store: PolicyStore, path: str, level: str) -> dict:
    target = _abs(path)
    if not Path(target).is_dir():
        raise ValueError(f"フォルダが見つかりません: {target}")
    if level not in ("read", "write"):
        raise ValueError("追加するフォルダは read（読むだけ）か write（書ける）です")
    return set_folder_level(store, target, level)


def folder_removal_form(store: PolicyStore, path: str) -> tuple[dict, bool]:
    """Return the updated form and whether removal takes the current main out of the share."""
    target = _abs(path)
    form = _form(store)
    remaining = [f for f in form["folders"] if _key(f["path"]) != _key(target)]
    if len(remaining) == len(form["folders"]):
        raise ValueError(f"登録されていないフォルダです: {target}")
    form["folders"] = remaining
    loses_main = False
    if form["main"]:
        before = store.current()
        after = parse_policy(form)
        readable = ("read", "write")
        # A surviving alias cannot replace access through the resolved main path.
        loses_main = any(
            before.level_for_abs(main) in readable and after.level_for_abs(main) not in readable
            for main in (before.main, Path(form["main"])) if main is not None
        )
        if loses_main:
            form["main"] = next((f["path"] for f in form["folders"] if f["level"] in ("read", "write")), None)
    return form, loses_main


def remove_folder(store: PolicyStore, path: str) -> dict:
    form, _ = folder_removal_form(store, path)
    save_form(store, form)
    return form


def set_main_folder(store: PolicyStore, path: str) -> dict:
    target = _abs(path)
    level = store.current().level_for_abs(Path(target))
    if level not in ("read", "write"):
        raise ValueError(f"メインにできるのは、読める共有フォルダだけです: {target}")
    form = _form(store)
    form["main"] = target
    save_form(store, form)
    return form
