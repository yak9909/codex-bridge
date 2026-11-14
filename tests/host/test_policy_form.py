import yaml

from codex_bridge.host.policy import PolicyStore, parse_policy
from codex_bridge.host.policy_form import form_from_raw, set_folder_level, yaml_from_form

FORM = {
    "default": "read",
    "folders": [{"path": "analysis", "level": "write"}, {"path": "private", "level": "hidden"},
                {"path": "analysis/locked", "level": "read"}],
    "exec": {"mode": "allowlist", "allow": ["python scripts/**/*.py *", "  ", "git status"],
             "confirm_modified_scripts": True},
    "delete_needs_approval": True,
    "paused": False,
}


def test_yaml_from_form_is_valid_and_commented():
    text = yaml_from_form(FORM)
    assert "# " in text and "見せない" in text  # Japanese explanations for people who open the file
    p = parse_policy(yaml.safe_load(text))
    assert p.level_for("analysis/x.csv") == "write"
    assert p.level_for("analysis/locked/y") == "read"      # deeper folder rule wins
    assert p.level_for("private/z") == "hidden"
    assert p.allow_patterns == ["python scripts/**/*.py *", "git status"]  # blank lines dropped
    assert p.confirm_modified_scripts is True


def test_form_roundtrip():
    raw = yaml.safe_load(yaml_from_form(FORM))
    form = form_from_raw(raw)
    assert form["default"] == "read" and form["exec"]["mode"] == "allowlist"
    assert {(f["path"], f["level"]) for f in form["folders"]} == {
        ("analysis", "write"), ("private", "hidden"), ("analysis/locked", "read")}


def test_form_from_raw_keeps_glob_rules_as_advanced():
    form = form_from_raw({"rules": [{"path": "raw/**", "level": "write"}, {"path": "docs", "level": "read"}]})
    assert form["folders"] == [{"path": "docs", "level": "read"}]
    assert form["advanced_rules"] == [{"path": "raw/**", "level": "write"}]
    # advanced rules survive a save from the form
    p = parse_policy(yaml.safe_load(yaml_from_form(form)))
    assert p.level_for("raw/a/b") == "write"


def test_set_folder_level(tmp_path):
    store = PolicyStore(tmp_path / "policy.yaml")
    store.save_text(yaml_from_form(FORM))
    set_folder_level(store, "analysis", "hidden")
    set_folder_level(store, "new\\dir", "write")
    set_folder_level(store, "analysis/locked", "inherit")   # removes the rule
    p = store.current()
    assert p.level_for("analysis/a") == "hidden" and p.level_for("new/dir/x") == "write"
    assert all(f["path"] != "analysis/locked" for f in form_from_raw(store.as_dict())["folders"])
