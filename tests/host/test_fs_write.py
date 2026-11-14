import pytest

from conftest import ORIGINAL_A, raises_msg
from codex_bridge.host.paths import ToolError


def test_edit_preserves_cp932_and_crlf(share):
    (share.root / "raw/sjis_crlf.txt").write_bytes("一行目\r\n日本語\r\n三行目\r\n".encode("cp932"))
    share.write.edit_file("raw/sjis_crlf.txt", "日本語", "にほんご")
    text = (share.root / "raw/sjis_crlf.txt").read_bytes().decode("cp932")
    assert text == "一行目\r\nにほんご\r\n三行目\r\n"


def test_edit_multiline_old_string_matches_crlf_file(share):
    (share.root / "raw/crlf.txt").write_bytes(b"a\r\nb\r\nc\r\n")
    share.write.edit_file("raw/crlf.txt", "a\nb", "x")
    assert (share.root / "raw/crlf.txt").read_bytes() == b"x\r\nc\r\n"


def test_edit_preserves_utf8_bom(share):
    (share.root / "raw/bom.txt").write_bytes(b"\xef\xbb\xbfhello\n")
    share.write.edit_file("raw/bom.txt", "hello", "bye")
    assert (share.root / "raw/bom.txt").read_bytes() == b"\xef\xbb\xbfbye\n"


def test_edit_requires_unique_match(share):
    (share.root / "raw/dup.txt").write_text("same\nsame\n", encoding="utf-8")
    with pytest.raises(ToolError, match="2 か所"):
        share.write.edit_file("raw/dup.txt", "same", "x")


def test_edit_replace_all(share):
    (share.root / "raw/dup.txt").write_text("same\nsame\n", encoding="utf-8", newline="\n")
    share.write.edit_file("raw/dup.txt", "same", "x", replace_all=True)
    assert (share.root / "raw/dup.txt").read_text(encoding="utf-8") == "x\nx\n"


def test_edit_no_match(share):
    assert "一致する箇所がありません: raw/a.txt" in raises_msg(
        lambda: share.write.edit_file("raw/a.txt", "zzz", "y"))


def test_write_to_read_only_area_refused(share):
    with pytest.raises(ToolError, match="書き込みは許可されていません"):
        share.write.write_file("docs/new.md", "x")


def test_write_creates_parent_dirs(share):
    share.write.write_file("out/sub/new.csv", "a,b\n")
    assert (share.root / "out/sub/new.csv").read_text(encoding="utf-8") == "a,b\n"


def test_write_existing_keeps_encoding(share):
    share.write.write_file("raw/sjis.txt", "新しい内容\n")
    assert (share.root / "raw/sjis.txt").read_bytes().decode("cp932") == "新しい内容\n"


def test_move_into_hidden_is_not_found(share):
    assert raises_msg(lambda: share.write.move_path("raw/a.txt", "raw/secret/x.txt")) == \
        "not found: raw/secret/x.txt"
    assert (share.root / "raw/a.txt").exists()


def test_move_onto_existing_refused(share):
    with pytest.raises(ToolError, match="既にあります"):
        share.write.move_path("raw/a.txt", "raw/b.txt")


async def test_undo_restores_edit_create_move_and_delete(share):
    share.task("t1")
    share.write.edit_file("raw/a.txt", "needle", "pin")
    share.write.edit_file("raw/a.txt", "pin", "pin2")  # second change keeps first snapshot
    share.write.write_file("raw/new.txt", "n")
    share.write.move_path("raw/sjis.txt", "out/moved.txt")
    share.approve_all()
    await share.write.delete_path("raw/b.txt")
    restored = share.snapshots.undo("t1")
    assert (share.root / "raw/a.txt").read_text(encoding="utf-8") == ORIGINAL_A
    assert not (share.root / "raw/new.txt").exists() and (share.root / "raw/b.txt").exists()
    assert (share.root / "raw/sjis.txt").exists() and not (share.root / "out/moved.txt").exists()
    assert sorted(restored) == ["out/moved.txt", "raw/a.txt", "raw/b.txt", "raw/new.txt", "raw/sjis.txt"]


async def test_undo_restores_deleted_directory(share):
    share.task("t2")
    (share.root / "raw/d/e").mkdir(parents=True)
    (share.root / "raw/d/e/f.txt").write_text("f", encoding="utf-8")
    await share.write.delete_path("raw/d")
    assert not (share.root / "raw/d").exists()
    share.snapshots.undo("t2")
    assert (share.root / "raw/d/e/f.txt").read_text(encoding="utf-8") == "f"


async def test_delete_denied_keeps_file(share):
    share.deny_all()
    with pytest.raises(ToolError, match="拒否"):
        await share.write.delete_path("raw/b.txt")
    assert (share.root / "raw/b.txt").exists()


async def test_delete_pending_returns_waiting(share):
    share.decision = "pending"
    r = await share.write.delete_path("raw/b.txt")
    assert r["status"] == "waiting_approval" and (share.root / "raw/b.txt").exists()


async def test_delete_without_approval_when_policy_allows(share):
    share.set_policy(delete_needs_approval=False)
    share.deny_all()
    r = await share.write.delete_path("raw/b.txt")
    assert r["status"] == "done" and not (share.root / "raw/b.txt").exists()


def test_binary_edit_refused(share):
    with pytest.raises(ToolError, match="テキストとして扱えません"):
        share.write.edit_file("raw/data.bin", "a", "b")


def test_changes_are_audited(share):
    share.task("t9")
    share.write.write_file("out/x.txt", "x")
    import json
    last = json.loads((share.data_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert last["kind"] == "file_change" and last["op"] == "write_file" and last["path"] == "out/x.txt"
    assert last["task_id"] == "t9"
