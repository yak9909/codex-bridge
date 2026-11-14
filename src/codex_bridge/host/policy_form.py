"""A simple form view of policy.yaml (used by the admin page and Claude Code).

form = {
  "default": "read",                                   # places no rule mentions
  "folders": [{"path": "analysis", "level": "write"}],  # one rule per folder (path and everything below)
  "advanced_rules": [{"path": "raw/**/*.csv", "level": "read"}],  # wildcard rules (kept, edited as YAML)
  "exec": {"mode": "allowlist", "allow": [...], "confirm_modified_scripts": False},
  "delete_needs_approval": True,
  "paused": False,
}
"""

from __future__ import annotations

import json

from .policy import EXEC_MODES, LEVELS, PolicyStore, normalize_rel

INHERIT = "inherit"


def _is_glob(path: str) -> bool:
    return any(c in path for c in "*?[")


def form_from_raw(raw: dict | None) -> dict:
    raw = raw or {}
    ex = raw.get("exec") or {}
    mode = ex.get("mode", "allowlist")
    folders, advanced = [], []
    for r in raw.get("rules") or []:
        entry = {"path": normalize_rel(str(r.get("path", ""))) if not _is_glob(str(r.get("path", ""))) else r["path"],
                 "level": r.get("level")}
        (advanced if _is_glob(str(r.get("path", ""))) else folders).append(entry)
    return {
        "default": raw.get("default", "read"),
        "folders": folders,
        "advanced_rules": advanced,
        "exec": {"mode": "off" if mode is False else mode, "allow": list(ex.get("allow") or []),
                 "confirm_modified_scripts": bool(ex.get("confirm_modified_scripts", False))},
        "delete_needs_approval": bool(raw.get("delete_needs_approval", True)),
        "paused": bool(raw.get("paused", False)),
    }


def _q(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def yaml_from_form(form: dict) -> str:
    """Build a commented policy.yaml from the form (raises ValueError on bad values)."""
    default = form.get("default", "read")
    if default not in LEVELS:
        raise ValueError(f"default は {LEVELS} のどれかです")
    folders = []
    for f in form.get("folders") or []:
        path, level = normalize_rel(str(f.get("path", ""))), f.get("level")
        if not path or level == INHERIT:
            continue
        if level not in LEVELS:
            raise ValueError(f"{path}: level は {LEVELS} のどれかです")
        folders.append((path, level))
    folders.sort(key=lambda pl: (pl[0].count("/"), pl[0].lower()))  # deeper folders later = they win
    ex = form.get("exec") or {}
    mode = ex.get("mode", "allowlist")
    if mode not in EXEC_MODES:
        raise ValueError(f"exec.mode は {EXEC_MODES} のどれかです")
    allow = [a.strip() for a in ex.get("allow") or [] if a and a.strip()]

    lines = [
        "# codex-bridge 権限の規則（管理画面の「権限」で編集できます）",
        "#   hidden = 見せない（Codex からは存在しないように見える。中のフォルダもすべて）",
        "#   read   = 読むだけ",
        "#   write  = 読み書きできる",
        "",
        "# 規則に当たらない場所の扱い",
        f"default: {default}",
        "",
        "# フォルダごとの規則（そのフォルダと中身すべて。深いフォルダの規則が優先。hidden は常に優先）",
        "rules:",
    ]
    for r in form.get("advanced_rules") or []:
        if r.get("level") not in LEVELS:
            raise ValueError(f"{r.get('path')}: level は {LEVELS} のどれかです")
        lines.append(f"  - {{path: {_q(str(r['path']))}, level: {r['level']}}}")
    for path, level in folders:
        lines.append(f"  - {{path: {_q(path)}, level: {level}}}")
    if lines[-1] == "rules:":
        lines[-1] = "rules: []"
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
        "",
        "# 削除するときは承認待ちにする",
        f"delete_needs_approval: {str(bool(form.get('delete_needs_approval', True))).lower()}",
        "",
        "# 全体の一時停止（true の間、Codex の道具はすべて止まる）",
        f"paused: {str(bool(form.get('paused', False))).lower()}",
        "",
    ]
    return "\n".join(lines)


def save_form(store: PolicyStore, form: dict) -> None:
    store.save_text(yaml_from_form(form))


def set_folder_level(store: PolicyStore, path: str, level: str) -> dict:
    """Set (or with level='inherit', remove) the rule for one folder; returns the new form."""
    path = normalize_rel(path)
    if not path:
        raise ValueError("共有フォルダ全体は default で設定してください")
    if level not in (*LEVELS, INHERIT):
        raise ValueError(f"level は hidden / read / write / inherit のどれかです")
    form = form_from_raw(store.as_dict())
    form["folders"] = [f for f in form["folders"] if f["path"].lower() != path.lower()]
    if level != INHERIT:
        form["folders"].append({"path": path, "level": level})
    save_form(store, form)
    return form
