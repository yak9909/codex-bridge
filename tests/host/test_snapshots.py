import pytest

from codex_bridge.host.snapshots import Snapshots

# Every "unsafe" id below points INSIDE tmp_path, so even a broken undo() cannot reach real folders.


@pytest.mark.parametrize("bad", ["..", "../..", r"..\x", "a/b", ""])
def test_undo_rejects_unsafe_task_ids(tmp_path, share, bad):
    # Review C3: undo("..") used to rmtree the data dir.
    data = tmp_path / "a" / "b" / "data"
    data.mkdir(parents=True)
    (tmp_path / "a" / "b" / "keep.txt").write_text("k", encoding="utf-8")
    (data / "tokens.json").write_text("{}", encoding="utf-8")
    s = Snapshots(data, share.paths)
    with pytest.raises(ValueError):
        s.undo(bad)
    assert (data / "tokens.json").exists() and (tmp_path / "a" / "b" / "keep.txt").exists()


def test_undo_rejects_absolute_path(tmp_path, share):
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "f.txt").write_text("v", encoding="utf-8")
    s = Snapshots(tmp_path / "data", share.paths)
    with pytest.raises(ValueError):
        s.undo(str(victim))
    assert (victim / "f.txt").exists()


@pytest.mark.parametrize("change", ["remove", "read"])
def test_undo_rejects_all_changes_if_one_target_is_no_longer_writable(share, change):
    from codex_bridge.host.policy_form import remove_folder, set_folder_level

    share.add_folder(share.outside, "write")
    share.task("t1")
    outside = share.outside / "outside.txt"
    inside = share.root / "out" / "changed.txt"
    share.write.write_file(outside.as_posix(), "outside change")
    share.write.write_file("out/changed.txt", "inside change")
    if change == "remove":
        remove_folder(share.policy, share.outside.as_posix())
    else:
        set_folder_level(share.policy, share.outside.as_posix(), "read")
        assert share.paths.level_of(outside) == "read"

    with pytest.raises(ValueError) as error:
        share.snapshots.undo("t1")
    assert str(error.value) == f"書き込めない場所のファイルは元に戻せません（共有を外した・読むだけや見せないに変えた）: {outside}"
    assert outside.read_text(encoding="utf-8") == "outside change"
    assert inside.read_text(encoding="utf-8") == "inside change"
    assert len(share.snapshots.touched("t1")) == 2
