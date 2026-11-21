from pathlib import Path

import yaml
import pytest

from codex_bridge.host.policy import Folder, PolicyStore, parse_policy
from codex_bridge.host.policy_migrate import migrate_v1
from codex_bridge.host.policy_form import add_folder, set_folder_level


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


@pytest.mark.parametrize("existing", [False, True])
def test_final_review_migration_preserves_original_bytes(tmp_path, existing):
    path = tmp_path / "policy.yaml"
    original = b'# original comment\r\ndefault: write\r\nexec: {mode: ask}\r\n'
    path.write_bytes(original)
    backup = tmp_path / "policy.v1.yaml"
    if existing:
        backup.write_bytes(b"earlier backup")
    store = PolicyStore(path, share_root=tmp_path / "share")
    assert store.current().v2
    assert backup.read_bytes() == (b"earlier backup" if existing else original)


def stored_bad_folder(tmp_path, level="write"):
    data = tmp_path / "data"
    data.mkdir()
    path = data / "policy.yaml"
    path.write_text(yaml.safe_dump({"folders": [{"path": tmp_path.as_posix(), "level": level}],
                                    "exec": {"mode": "ask"}}), encoding="utf-8")
    return PolicyStore(path)


def test_pause_and_exec_mode_work_with_a_bad_folder_stored(tmp_path):
    store = stored_bad_folder(tmp_path)
    store.update(paused=True)
    assert store.current().paused
    store.update(exec_mode="off")
    assert store.current().exec_mode == "off"
    store.update(paused=False)
    assert not store.current().paused


@pytest.mark.parametrize("level", ["hidden", "inherit"])
def test_bad_folder_can_be_removed_or_hidden(tmp_path, level):
    store = stored_bad_folder(tmp_path)
    set_folder_level(store, tmp_path.as_posix(), level)
    assert store.current().folders == (() if level == "inherit" else (Folder(tmp_path, "hidden"),))
    assert store.problems() == []


@pytest.mark.parametrize("previous", [None, "hidden", "read"])
@pytest.mark.parametrize("level", ["read", "write"])
def test_adding_a_folder_that_contains_data_dir_is_still_refused(tmp_path, previous, level):
    store = stored_bad_folder(tmp_path, previous or "hidden")
    if previous is None:
        store.path.write_text("folders: []\n", encoding="utf-8")
    before = store.path.read_bytes()
    if previous == level:
        store.save({"folders": [{"path": tmp_path.as_posix(), "level": level}]})
        return  # unchanged stored rule does not prevent saving unrelated settings
    with pytest.raises(ValueError, match="データフォルダ"):
        store.save({"folders": [{"path": tmp_path.as_posix(), "level": level}]})
    assert store.path.read_bytes() == before


def test_changing_bad_folder_path_is_refused(tmp_path):
    store = stored_bad_folder(tmp_path)
    with pytest.raises(ValueError, match="データフォルダ"):
        store.save({"folders": [{"path": tmp_path.parent.as_posix(), "level": "write"}]})


def test_bad_folder_downgrade_and_normalized_path_are_allowed(tmp_path):
    store = stored_bad_folder(tmp_path)
    store.save({"folders": [{"path": str(tmp_path).upper(), "level": "read"}]})
    assert store.current().folders[0].level == "read"
    assert store.problems() == [{"folder": store.current().folders[0].path.as_posix(),
                                 "reason": "contains_data_dir"}]


def test_unchanged_duplicate_bad_folders_do_not_block_pause(tmp_path):
    store = stored_bad_folder(tmp_path)
    store.path.write_text(yaml.safe_dump({"folders": [
        {"path": tmp_path.as_posix(), "level": "read"},
        {"path": tmp_path.as_posix(), "level": "hidden"}]}), encoding="utf-8")
    store.update(paused=True)
    assert store.current().paused


def test_unrelated_changes_save_with_a_stored_rule_containing_data_dir(tmp_path):
    store = stored_bad_folder(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    add_folder(store, other.as_posix(), "read")
    set_folder_level(store, other.as_posix(), "hidden")
    set_folder_level(store, other.as_posix(), "inherit")
    store.update(paused=True)
    store.update(exec_mode="off")
    assert store.current().paused and store.current().exec_mode == "off"
    with pytest.raises(ValueError, match="データフォルダ"):
        add_folder(store, tmp_path.parent.as_posix(), "read")


def test_delete_approval_per_lender():
    p = parse_policy({"delete_needs_approval": True, "lender_delete_approval": {"yamada": False}})
    assert p.delete_approval_for("yamada") is False
    assert p.delete_approval_for("suzuki") is True
    assert p.delete_approval_for(None) is True
    assert parse_policy({"delete_needs_approval": False, "lender_delete_approval": {"a": True}}
                        ).delete_approval_for("b") is False
    with pytest.raises(ValueError):
        parse_policy({"lender_delete_approval": {"yamada": "no"}})


async def test_delete_through_write_tools_uses_the_calling_lenders_setting(share):
    share.policy.update(delete_needs_approval=True, lender_delete_approval={"yamada": False})
    share.deny_all()  # an asked approval is refused: a delete that is not asked goes through
    share.write.lender_fn = lambda: "yamada"
    assert (await share.write.delete_path("raw/b.txt"))["status"] == "done"
    share.write.lender_fn = lambda: "suzuki"
    with pytest.raises(Exception, match="拒否"):
        await share.write.delete_path("raw/a.txt")
    assert (share.root / "raw/a.txt").exists()


async def test_patch_delete_uses_the_calling_lenders_setting(share):
    from codex_bridge.host.patch import apply_patch
    share.policy.update(delete_needs_approval=True, lender_delete_approval={"yamada": False})
    share.deny_all()
    share.write.lender_fn = lambda: "yamada"
    r = await apply_patch(share.write, "*** Begin Patch\n*** Delete File: raw/b.txt\n*** End Patch\n")
    assert r["status"] == "done"
    share.write.lender_fn = lambda: "suzuki"
    with pytest.raises(Exception, match="拒否"):
        await apply_patch(share.write, "*** Begin Patch\n*** Delete File: raw/a.txt\n*** End Patch\n")


def test_drop_lender_drops_delete_approval(tmp_path):
    store = PolicyStore(tmp_path / "policy.yaml")
    store.update(lender_exec_modes={"yamada": "ask", "suzuki": "all"},
                 lender_delete_approval={"yamada": False, "suzuki": False})
    store.drop_lender("yamada")
    assert store.current().lender_exec_modes == {"suzuki": "all"}
    assert store.current().lender_delete_approval == {"suzuki": False}
    store.set_lender_delete_approval("yamada", True)
    assert store.current().lender_delete_approval == {"suzuki": False, "yamada": True}
    store.drop_lender("yamada")
    assert store.current().lender_delete_approval == {"suzuki": False}


def test_lender_delete_approval_round_trips_through_the_form(tmp_path):
    from codex_bridge.host.policy_form import form_from_raw, save_form
    store = PolicyStore(tmp_path / "policy.yaml")
    store.update(lender_delete_approval={"yamada": False})
    form = form_from_raw(store.as_dict())
    assert form["lender_delete_approval"] == {"yamada": False}
    save_form(store, form)
    assert store.current().lender_delete_approval == {"yamada": False}
    form.pop("lender_delete_approval")  # a form without the key keeps what is stored
    save_form(store, form)
    assert store.current().lender_delete_approval == {"yamada": False}


def test_old_global_exec_mode_still_applies(tmp_path):
    p = parse_policy({"exec": {"mode": "all"}})
    assert p.exec_mode_for("yamada") == "all" and p.exec_mode_for(None) == "all"
    p = parse_policy({"exec": {"mode": "all"}, "lender_exec_modes": {"yamada": "ask"}})
    assert p.exec_mode_for("yamada") == "ask" and p.exec_mode_for("suzuki") == "all"
