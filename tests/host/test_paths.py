import pytest

from conftest import make_junction, raises_msg
from codex_bridge.host.paths import ToolError


def test_dotdot_escape_is_not_found(share):
    with pytest.raises(ToolError, match="not found: ../outside/outside.txt"):
        share.paths.resolve("../outside/outside.txt", "read")


def test_absolute_path_is_not_found(share):
    msg = raises_msg(lambda: share.paths.resolve(str(share.outside / "outside.txt"), "read"))
    assert msg.startswith("not found: ") and "outside" in msg


def test_junction_to_outside_is_not_found(share):
    make_junction(share.root / "link", share.outside)
    with pytest.raises(ToolError, match="not found"):
        share.paths.resolve("link/outside.txt", "read")


def test_junction_to_hidden_inside_is_not_found(share):
    make_junction(share.root / "docs" / "peek", share.root / "raw" / "secret")
    with pytest.raises(ToolError, match="not found: docs/peek/k.txt"):
        share.paths.resolve("docs/peek/k.txt", "read")


def test_read_write_on_read_only_area_is_refused(share):
    with pytest.raises(ToolError, match="書き込みは許可されていません: docs/readme.md"):
        share.paths.resolve("docs/readme.md", "write")


def test_backslash_and_dot_paths_normalize(share):
    p = share.paths.resolve(".\\raw\\a.txt", "write")
    assert share.paths.to_rel(p) == "raw/a.txt"


def test_paused_blocks_resolve(share):
    share.set_policy(paused=True)
    with pytest.raises(ToolError, match="停止中"):
        share.paths.resolve("raw/a.txt", "read")
