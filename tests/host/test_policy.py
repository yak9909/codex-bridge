from pathlib import Path

from codex_bridge.host.policy import PolicyStore

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
