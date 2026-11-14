import pytest

from codex_bridge.host.snapshots import Snapshots

# Every "unsafe" id below points INSIDE tmp_path, so even a broken undo() cannot reach real folders.


@pytest.mark.parametrize("bad", ["..", "../..", r"..\x", "a/b", ""])
def test_undo_rejects_unsafe_task_ids(tmp_path, bad):
    # Review C3: undo("..") used to rmtree the data dir.
    data = tmp_path / "a" / "b" / "data"
    data.mkdir(parents=True)
    (tmp_path / "a" / "b" / "keep.txt").write_text("k", encoding="utf-8")
    (data / "tokens.json").write_text("{}", encoding="utf-8")
    s = Snapshots(data)
    with pytest.raises(ValueError):
        s.undo(bad)
    assert (data / "tokens.json").exists() and (tmp_path / "a" / "b" / "keep.txt").exists()


def test_undo_rejects_absolute_path(tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "f.txt").write_text("v", encoding="utf-8")
    s = Snapshots(tmp_path / "data")
    with pytest.raises(ValueError):
        s.undo(str(victim))
    assert (victim / "f.txt").exists()
