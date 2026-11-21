from pathlib import Path

import pytest
import yaml

from codex_bridge.host.policy import EXEC_MODES, PolicyStore, parse_policy

POLICY = """
default: read
rules:
  - {path: "raw/**", level: write}
  - {path: "raw/secret/**", level: hidden}
  - {path: "out", level: write}
exec: {mode: allowlist, allow: ["python scripts/**/*.py *"]}
"""


def write_policy(tmp_path: Path, text: str) -> PolicyStore:
    (tmp_path / "policy.yaml").write_text(text, encoding="utf-8")
    return PolicyStore(tmp_path / "policy.yaml")


def rewrite(tmp_path: Path, text: str) -> None:
    (tmp_path / "policy.yaml").write_text(text, encoding="utf-8")


def test_later_more_specific_rule_wins(tmp_path):
    p = write_policy(tmp_path, POLICY).current()
    assert p.level_for("raw/a.txt") == "write"
    assert p.level_for("raw/secret/x.bin") == "hidden"
    assert p.level_for("RAW/Secret/x.bin") == "hidden"
    assert p.level_for("docs/readme.md") == "read"
    assert p.level_for("out/sub/f.csv") == "write"
    assert p.level_for("out") == "write"


def test_hidden_parent_hides_children_even_if_child_rule_says_write(tmp_path):
    store = write_policy(tmp_path, POLICY.replace(
        'exec:', '  - {path: "raw/secret/ok.txt", level: write}\nexec:'))
    assert store.current().level_for("raw/secret/ok.txt") == "hidden"


def test_hidden_directory_itself_is_hidden(tmp_path):
    store = write_policy(tmp_path, POLICY)
    # "raw/secret/**" matches the children; the folder itself must also be hidden
    assert store.current().level_for("raw/secret") == "hidden"


def test_reload_on_change(tmp_path):
    store = write_policy(tmp_path, POLICY)
    assert store.current().exec_mode == "allowlist"
    rewrite(tmp_path, POLICY.replace("allowlist", "off"))
    assert store.current().exec_mode == "off"


def test_broken_yaml_keeps_last_good_policy(tmp_path):
    store = write_policy(tmp_path, POLICY)
    store.current()
    rewrite(tmp_path, "rules: [")
    assert store.current().exec_mode == "allowlist"


def test_defaults(tmp_path):
    p = write_policy(tmp_path, "default: read\n").current()
    assert p.exec_mode == "allowlist" and p.allow_patterns == []
    assert p.paused is False and p.confirm_modified_scripts is False
    assert p.delete_needs_approval is True
    assert p.limits.exec_timeout_sec == 1800
    assert p.limits.output_max_bytes == 1024 * 1024
    assert p.limits.sync_wait_sec == 60
    assert p.limits.approval_timeout_sec == 1800
    assert p.limits.read_max_bytes == 256 * 1024
    assert p.limits.grep_max_matches == 500


def test_save_validates_and_writes(tmp_path):
    store = write_policy(tmp_path, POLICY)
    store.save({"default": "read", "rules": [], "exec": {"mode": "off"}})
    assert store.current().exec_mode == "off"


def test_save_rejects_bad_level(tmp_path):
    import pytest
    store = write_policy(tmp_path, POLICY)
    with pytest.raises(ValueError):
        store.save({"default": "everything"})
    assert store.current().exec_mode == "allowlist"


def test_missing_policy_file_means_default_read_with_exec_off(tmp_path):
    store = PolicyStore(tmp_path / "nope.yaml")
    p = store.current()
    assert p.level_for("anything") == "read" and p.exec_mode == "off"


def test_hidden_wins_over_any_later_rule(tmp_path):
    # Review I9: a hidden folder stays hidden even if a later broad rule matches.
    store = write_policy(tmp_path, 'default: read\nrules:\n  - {path: "private", level: hidden}\n'
                                   '  - {path: "**", level: write}\n')
    p = store.current()
    assert p.level_for("private/x.txt") == "hidden" and p.level_for("private") == "hidden"
    assert p.level_for("other.txt") == "write"


def test_lender_exec_modes_parse_and_off_as_false():
    p = parse_policy(yaml.safe_load('exec: {mode: ask}\nlender_exec_modes: {a: off, b: all}\n'))
    assert p.lender_exec_modes == {"a": "off", "b": "all"}
    assert p.exec_mode_for("a") == "off" and p.exec_mode_for("b") == "all"
    assert p.exec_mode_for("c") == "ask" and p.exec_mode_for(None) == "ask"
    for raw in ({}, {"lender_exec_modes": None}):
        assert parse_policy(raw).lender_exec_modes == {}


@pytest.mark.parametrize("modes", [{"a": "bad"}, {"a": "inherit"}, {"a": None}, {"a": True},
                                   {"a": []}, {1: "all"}, [], "all", False, 0])
def test_lender_exec_modes_bad_value_rejected(modes):
    with pytest.raises(ValueError) as exc:
        parse_policy({"lender_exec_modes": modes})
    expected = (f"lender_exec_modes の値は {EXEC_MODES} のどれかです: {next(iter(modes))}"
                if isinstance(modes, dict) else "lender_exec_modes は 名前: モード の形で書いてください")
    assert str(exc.value) == expected


def test_set_and_inherit_lender_mode(tmp_path):
    store = write_policy(tmp_path, POLICY)
    store.set_lender_exec_mode("b", "ask")
    for mode in EXEC_MODES:
        store.set_lender_exec_mode("a", mode)
        assert PolicyStore(store.path).current().exec_mode_for("a") == mode
        assert store.current().exec_mode == "allowlist"
        assert store.current().allow_patterns == ["python scripts/**/*.py *"]
    before = store.path.read_bytes()
    for invalid in ("bad", None, False, [], {}):
        with pytest.raises(ValueError):
            store.set_lender_exec_mode("a", invalid)
        assert store.path.read_bytes() == before
    store.set_lender_exec_mode("a", "inherit")
    assert store.current().lender_exec_modes == {"b": "ask"}
    assert store.current().exec_mode_for("a") == "allowlist"
    before = store.path.read_bytes()
    store.drop_lender("missing")
    assert store.path.read_bytes() == before
    store.drop_lender("b")
    assert PolicyStore(store.path).current().lender_exec_modes == {}
