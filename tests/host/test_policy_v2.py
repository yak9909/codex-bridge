from pathlib import Path

import yaml

from codex_bridge.host.policy import PolicyStore, parse_policy
from codex_bridge.host.policy_migrate import migrate_v1


def v2(tmp_path: Path) -> dict:
    work, tools = tmp_path / "work", tmp_path / "tools"
    for d in (work / "vendor", work / "secret", tools, tmp_path / "workspace"):
        d.mkdir(parents=True, exist_ok=True)
    return {"folders": [
        {"path": work.as_posix(), "level": "write"},
        {"path": tools.as_posix(), "level": "read"},
        {"path": (work / "secret").as_posix(), "level": "hidden"},
        {"path": (work / "vendor").as_posix(), "level": "read"},
    ]}


def test_deepest_folder_wins_and_hidden_always_wins(tmp_path):
    p = parse_policy(v2(tmp_path))
    assert p.level_for_abs(tmp_path / "work" / "a.txt") == "write"
    assert p.level_for_abs(tmp_path / "work" / "vendor" / "x.py") == "read"
    assert p.level_for_abs(tmp_path / "work" / "secret" / "deep" / "x") == "hidden"
    assert p.level_for_abs(tmp_path / "tools" / "lib.py") == "read"
    assert p.level_for_abs(tmp_path / "WORK" / "A.TXT") == "write"  # case-insensitive on Windows


def test_prefix_is_not_a_parent(tmp_path):
    p = parse_policy(v2(tmp_path))
    assert p.level_for_abs(tmp_path / "workspace" / "x") is None


def test_outside_all_folders_is_none(tmp_path):
    assert parse_policy(v2(tmp_path)).level_for_abs(tmp_path / "elsewhere" / "x") is None


def test_main_is_first_visible_folder_and_relative_uses_it(tmp_path):
    p = parse_policy(v2(tmp_path))
    assert p.main == (tmp_path / "work").resolve()
    assert p.level_for("vendor/x.py") == "read" and p.level_for("a.txt") == "write"
    raw = v2(tmp_path)
    raw["main"] = (tmp_path / "tools").as_posix()
    assert parse_policy(raw).main == (tmp_path / "tools").resolve()


def test_outputs_dir_of_main_is_always_read(tmp_path):
    p = parse_policy(v2(tmp_path))
    assert p.level_for_abs(tmp_path / "work" / ".codex-bridge" / "outputs" / "j1.txt") == "read"


def test_advanced_rules_are_relative_to_main(tmp_path):
    raw = v2(tmp_path)
    raw["advanced_rules"] = [{"path": "**/*.lock", "level": "hidden"}]
    p = parse_policy(raw)
    assert p.level_for_abs(tmp_path / "work" / "sub" / "a.lock") == "hidden"
    assert p.level_for_abs(tmp_path / "work" / "sub" / "a.txt") == "write"


def test_migrate_v1(tmp_path):
    share = tmp_path / "share"
    v1 = {"default": "read", "rules": [{"path": "raw", "level": "write"}, {"path": "private", "level": "hidden"},
                                       {"path": "raw/**/*.tmp", "level": "hidden"}],
          "exec": {"mode": "ask"}, "paused": False}
    out = migrate_v1(v1, share)
    assert out["main"] == share.as_posix()
    assert out["folders"] == [{"path": share.as_posix(), "level": "read"},
                              {"path": (share / "raw").as_posix(), "level": "write"},
                              {"path": (share / "private").as_posix(), "level": "hidden"}]
    assert out["advanced_rules"] == [{"path": "raw/**/*.tmp", "level": "hidden"}]
    assert out["exec"] == {"mode": "ask"} and "rules" not in out and "default" not in out


def test_migrate_v1_with_hidden_default_registers_only_rule_folders(tmp_path):
    share = tmp_path / "share"
    out = migrate_v1({"default": "hidden", "rules": [{"path": "pub", "level": "read"}]}, share)
    assert out["folders"] == [{"path": (share / "pub").as_posix(), "level": "read"}]
    assert out["main"] == share.as_posix()


def test_store_converts_v1_file_on_load(tmp_path):
    share = tmp_path / "share"
    (share / "raw").mkdir(parents=True)
    f = tmp_path / "policy.yaml"
    f.write_text('default: read\nrules:\n  - {path: "raw", level: write}\n', encoding="utf-8")
    store = PolicyStore(f, share_root=share)
    p = store.current()
    assert p.level_for_abs(share / "raw" / "x") == "write" and p.level_for("raw/x") == "write"
    assert "folders" in yaml.safe_load(f.read_text(encoding="utf-8"))


def test_store_without_share_root_keeps_v1_behaviour(tmp_path):
    f = tmp_path / "policy.yaml"
    f.write_text('default: read\nrules:\n  - {path: "raw", level: write}\n', encoding="utf-8")
    p = PolicyStore(f).current()
    assert p.level_for("raw/x") == "write" and p.level_for("docs/x") == "read"
