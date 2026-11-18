from pathlib import Path

import pytest
import yaml

from codex_bridge.host.policy import PolicyStore, parse_policy
from codex_bridge.host.policy_form import (add_folder, form_from_raw, remove_folder, set_folder_level,
                                           save_form, set_main_folder, yaml_from_form)


def make_form(tmp_path: Path) -> dict:
    work, tools = tmp_path / "work", tmp_path / "tools"
    for d in (work / "locked", work / "private", tools):
        d.mkdir(parents=True, exist_ok=True)
    return {
        "main": work.as_posix(),
        "folders": [{"path": work.as_posix(), "level": "write"},
                    {"path": (work / "private").as_posix(), "level": "hidden"},
                    {"path": (work / "locked").as_posix(), "level": "read"},
                    {"path": tools.as_posix(), "level": "read"}],
        "exec": {"mode": "allowlist", "allow": ["python scripts/**/*.py *", "  ", "git status"],
                 "confirm_modified_scripts": True},
        "delete_needs_approval": True,
        "paused": False,
    }


def store_with(tmp_path: Path) -> PolicyStore:
    store = PolicyStore(tmp_path / "policy.yaml")
    store.save_text(yaml_from_form(make_form(tmp_path)))
    return store


def test_yaml_from_form_is_valid_and_commented(tmp_path):
    text = yaml_from_form(make_form(tmp_path))
    assert "# " in text and "見せない" in text
    p = parse_policy(yaml.safe_load(text))
    assert p.level_for_abs(tmp_path / "work" / "a.csv") == "write"
    assert p.level_for_abs(tmp_path / "work" / "locked" / "y") == "read"
    assert p.level_for_abs(tmp_path / "work" / "private" / "z") == "hidden"
    assert p.level_for_abs(tmp_path / "tools" / "t.py") == "read"
    assert p.allow_patterns == ["python scripts/**/*.py *", "git status"]
    assert p.main == (tmp_path / "work").resolve()


def test_relative_folder_paths_are_rejected(tmp_path):
    form = make_form(tmp_path)
    form["folders"].append({"path": "relative/dir", "level": "read"})
    with pytest.raises(ValueError, match="フルパス"):
        yaml_from_form(form)


def test_form_roundtrip(tmp_path):
    form = form_from_raw(yaml.safe_load(yaml_from_form(make_form(tmp_path))))
    assert form["main"] == (tmp_path / "work").as_posix() and form["exec"]["mode"] == "allowlist"
    assert {(f["path"], f["level"]) for f in form["folders"]} == {
        ((tmp_path / "work").as_posix(), "write"), ((tmp_path / "work" / "private").as_posix(), "hidden"),
        ((tmp_path / "work" / "locked").as_posix(), "read"), ((tmp_path / "tools").as_posix(), "read")}


def test_advanced_rules_survive(tmp_path):
    form = make_form(tmp_path)
    form["advanced_rules"] = [{"path": "**/*.lock", "level": "hidden"}]
    p = parse_policy(yaml.safe_load(yaml_from_form(form)))
    assert p.level_for_abs(tmp_path / "work" / "a" / "x.lock") == "hidden"


def test_set_folder_level_absolute_relative_and_inherit(tmp_path):
    store = store_with(tmp_path)
    set_folder_level(store, (tmp_path / "tools").as_posix(), "hidden")
    set_folder_level(store, "new\\dir", "write")                  # relative → under main
    set_folder_level(store, (tmp_path / "work" / "locked").as_posix(), "inherit")
    p = store.current()
    assert p.level_for_abs(tmp_path / "tools" / "x") == "hidden"
    assert p.level_for_abs(tmp_path / "work" / "new" / "dir" / "x") == "write"
    assert p.level_for_abs(tmp_path / "work" / "locked" / "x") == "write"


def test_add_remove_and_main(tmp_path):
    store = store_with(tmp_path)
    extra = tmp_path / "extra"
    extra.mkdir()
    add_folder(store, extra.as_posix(), "write")
    assert store.current().level_for_abs(extra / "f") == "write"
    set_main_folder(store, extra.as_posix())
    assert store.current().main == extra.resolve()
    remove_folder(store, extra.as_posix())
    assert store.current().level_for_abs(extra / "f") is None
    assert store.current().main == (tmp_path / "work").resolve()   # main falls back to the first folder


def test_add_rejects_missing_and_relative(tmp_path):
    store = store_with(tmp_path)
    with pytest.raises(ValueError):
        add_folder(store, (tmp_path / "nope").as_posix(), "read")
    with pytest.raises(ValueError):
        add_folder(store, "rel/dir", "read")
    with pytest.raises(ValueError):
        set_main_folder(store, (tmp_path / "work" / "private").as_posix())  # hidden can't be main


def test_form_round_trip_keeps_lender_exec_modes(tmp_path):
    raw = make_form(tmp_path)
    raw["lender_exec_modes"] = {"a": False, '友人 "b"': "all"}
    form = form_from_raw(raw)
    modes = {"a": "off", '友人 "b"': "all"}
    assert form["lender_exec_modes"] == modes
    text = yaml_from_form(form)
    assert '# Codex を貸している人ごとのコマンド実行のモード（書いていない人は、上の exec.mode と同じ）' in text
    assert '  "a": "off"' in text
    assert text.index("lender_exec_modes:") < text.index("# 削除するときは承認待ちにする")
    assert parse_policy(yaml.safe_load(text)).lender_exec_modes == modes
    assert form_from_raw(yaml.safe_load(text))["lender_exec_modes"] == modes
    assert form_from_raw({"lender_exec_modes": None})["lender_exec_modes"] == {}


def test_save_form_without_key_keeps_modes(tmp_path):
    store = store_with(tmp_path)
    store.update(lender_exec_modes={"a": "off", "b": "ask"})
    form = form_from_raw(store.as_dict())
    form.pop("lender_exec_modes", None)
    form["exec"]["mode"] = "all"
    save_form(store, form)
    assert store.current().lender_exec_modes == {"a": "off", "b": "ask"}
    assert store.current().exec_mode == "all"
    assert store.current().confirm_modified_scripts is True
    assert "lender_exec_modes" not in form
    save_form(store, {**form, "lender_exec_modes": {}})
    assert store.current().lender_exec_modes == {}


@pytest.mark.parametrize("operation", ["level", "inherit", "add", "remove", "main"])
def test_set_folder_level_keeps_lender_modes(tmp_path, operation):
    store = store_with(tmp_path)
    store.update(lender_exec_modes={"a": "off", "b": "all"})
    tools = (tmp_path / "tools").as_posix()
    if operation in ("level", "inherit"):
        set_folder_level(store, tools, "hidden" if operation == "level" else "inherit")
    elif operation == "add":
        add_folder(store, (tmp_path / "work" / "locked").as_posix(), "read")
    elif operation == "remove":
        remove_folder(store, tools)
    else:
        set_main_folder(store, tools)
    assert PolicyStore(store.path).current().lender_exec_modes == {"a": "off", "b": "all"}


@pytest.mark.parametrize("modes", [{"a": "bad"}, {"a": "inherit"}, {1: "all"}, [], False])
def test_yaml_from_form_rejects_bad_lender_modes(modes):
    with pytest.raises(ValueError, match="lender_exec_modes"):
        yaml_from_form({"lender_exec_modes": modes})
