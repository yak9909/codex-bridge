import pytest

from conftest import ORIGINAL_A, raises_msg
from codex_bridge.host.patch import AddFile, DeleteFile, UpdateFile, apply_patch, parse_patch
from codex_bridge.host.paths import ToolError

UPDATE_A = """*** Begin Patch
*** Update File: raw/a.txt
@@
 line2
-the needle here
+the pin here
*** End Patch
"""

A_OK_THEN_B_BAD = """*** Begin Patch
*** Update File: raw/a.txt
@@
-the needle here
+changed
*** Update File: raw/b.txt
@@
-does not exist in b
+x
*** End Patch
"""


def test_parse_ops():
    ops = parse_patch("*** Begin Patch\n*** Add File: n.txt\n+hi\n*** Delete File: d.txt\n"
                      "*** Update File: u.txt\n*** Move to: v.txt\n@@\n-a\n+b\n*** End Patch\n")
    assert isinstance(ops[0], AddFile) and ops[0].content == "hi\n"
    assert isinstance(ops[1], DeleteFile) and ops[1].path == "d.txt"
    assert isinstance(ops[2], UpdateFile) and ops[2].move_to == "v.txt" and len(ops[2].hunks) == 1


def test_parse_rejects_missing_markers():
    with pytest.raises(ToolError, match="Begin Patch"):
        parse_patch("*** Update File: a\n")


async def test_update_hunk_applies(share):
    r = await apply_patch(share.write, UPDATE_A)
    assert r["changed"] == ["raw/a.txt"]
    assert (share.root / "raw/a.txt").read_text(encoding="utf-8") == "line1\nline2\nthe pin here\nline4\n"


async def test_update_keeps_crlf(share):
    (share.root / "raw/crlf.txt").write_bytes(b"a\r\nb\r\nc\r\n")
    await apply_patch(share.write, "*** Begin Patch\n*** Update File: raw/crlf.txt\n@@\n a\n-b\n+B\n*** End Patch\n")
    assert (share.root / "raw/crlf.txt").read_bytes() == b"a\r\nB\r\nc\r\n"


async def test_context_header_selects_location(share):
    (share.root / "raw/two.txt").write_text("def f():\n    x = 1\ndef g():\n    x = 1\n", encoding="utf-8", newline="\n")
    await apply_patch(share.write, "*** Begin Patch\n*** Update File: raw/two.txt\n@@ def g():\n-    x = 1\n+    x = 2\n*** End Patch\n")
    assert (share.root / "raw/two.txt").read_text(encoding="utf-8") == "def f():\n    x = 1\ndef g():\n    x = 2\n"


async def test_trailing_whitespace_tolerated(share):
    (share.root / "raw/ws.txt").write_text("a  \nb\n", encoding="utf-8", newline="\n")
    await apply_patch(share.write, "*** Begin Patch\n*** Update File: raw/ws.txt\n@@\n-a\n+A\n*** End Patch\n")
    assert (share.root / "raw/ws.txt").read_text(encoding="utf-8") == "A\nb\n"


async def test_add_move_and_delete(share):
    share.approve_all()
    r = await apply_patch(share.write, "*** Begin Patch\n*** Add File: out/n.txt\n+new\n"
                          "*** Update File: raw/a.txt\n*** Move to: out/a2.txt\n@@\n-line1\n+LINE1\n"
                          "*** Delete File: raw/b.txt\n*** End Patch\n")
    assert sorted(r["changed"]) == ["out/a2.txt", "out/n.txt", "raw/a.txt", "raw/b.txt"]
    assert (share.root / "out/n.txt").read_text(encoding="utf-8") == "new\n"
    assert (share.root / "out/a2.txt").read_text(encoding="utf-8").startswith("LINE1\n")
    assert not (share.root / "raw/a.txt").exists() and not (share.root / "raw/b.txt").exists()


async def test_all_or_nothing_when_second_file_fails(share):
    with pytest.raises(ToolError):
        await apply_patch(share.write, A_OK_THEN_B_BAD)
    assert (share.root / "raw/a.txt").read_text(encoding="utf-8") == ORIGINAL_A


async def test_patch_into_hidden_reports_not_found_only(share):
    with pytest.raises(ToolError) as ei:
        await apply_patch(share.write, "*** Begin Patch\n*** Add File: raw/secret/z.txt\n+x\n*** End Patch\n")
    assert str(ei.value) == "not found: raw/secret/z.txt"


async def test_update_hidden_reports_not_found_only(share):
    with pytest.raises(ToolError) as ei:
        await apply_patch(share.write, "*** Begin Patch\n*** Update File: raw/secret/k.txt\n@@\n-the\n+x\n*** End Patch\n")
    assert str(ei.value) == "not found: raw/secret/k.txt"


async def test_context_mismatch_message_names_file(share):
    with pytest.raises(ToolError) as ei:
        await apply_patch(share.write, "*** Begin Patch\n*** Update File: raw/a.txt\n@@\n-nothing like this\n+x\n*** End Patch\n")
    assert "raw/a.txt" in str(ei.value) and "一致する箇所がありません" in str(ei.value)


async def test_delete_denied_applies_nothing(share):
    share.deny_all()
    with pytest.raises(ToolError, match="拒否"):
        await apply_patch(share.write, "*** Begin Patch\n*** Add File: out/n.txt\n+x\n"
                          "*** Delete File: raw/b.txt\n*** End Patch\n")
    assert not (share.root / "out/n.txt").exists() and (share.root / "raw/b.txt").exists()


async def test_add_existing_file_refused(share):
    with pytest.raises(ToolError, match="既にあります"):
        await apply_patch(share.write, "*** Begin Patch\n*** Add File: raw/a.txt\n+x\n*** End Patch\n")


async def test_encoding_failure_in_second_file_applies_nothing(share):
    # Review I8: encoding errors must be caught before anything is written.
    (share.root / "raw/sj.txt").write_bytes("日本語\n".encode("cp932"))
    patch = ("*** Begin Patch\n*** Update File: raw/a.txt\n@@\n-line1\n+LINE1\n"
             "*** Update File: raw/sj.txt\n@@\n-日本語\n+日本語😀\n*** End Patch\n")
    with pytest.raises(ToolError):
        await apply_patch(share.write, patch)
    assert (share.root / "raw/a.txt").read_text(encoding="utf-8") == ORIGINAL_A
