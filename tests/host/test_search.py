import pytest

from conftest import make_junction, raises_msg
from codex_bridge.host.paths import ToolError


def test_grep_finds_and_reports_relative_paths(share):
    r = share.search.grep("needle")
    assert {"path": "raw/a.txt", "line": 3, "text": "the needle here"} in r["matches"]


def test_grep_never_returns_hidden(share):
    r = share.search.grep("needle")
    assert r["matches"] and all(not m["path"].startswith("raw/secret") for m in r["matches"])


def test_grep_never_returns_hidden_via_junction(share):
    make_junction(share.root / "docs" / "peek", share.root / "raw" / "secret")
    make_junction(share.root / "docs" / "out2", share.outside)
    paths = {m["path"] for m in share.search.grep("needle|outside")["matches"]}
    assert not any("peek" in p or "out2" in p or "secret" in p for p in paths)


def test_grep_hidden_dir_as_target_is_not_found(share):
    with pytest.raises(ToolError, match="not found: raw/secret"):
        share.search.grep("x", path="raw/secret")


def test_grep_limit(share):
    r = share.search.grep("row", limit=500)
    assert len(r["matches"]) == 500 and r["truncated"]


def test_grep_limit_capped_by_policy(share):
    r = share.search.grep("row", limit=10_000)
    assert len(r["matches"]) == 500 and r["truncated"]


def test_grep_glob_and_ignore_case(share):
    r = share.search.grep("README", glob="*.md", ignore_case=True)
    assert [m["path"] for m in r["matches"]] == ["docs/readme.md"]


def test_grep_shift_jis_file(share):
    r = share.search.grep("日本語")
    assert any(m["path"] == "raw/sjis.txt" for m in r["matches"])


def test_grep_bad_regex_message_does_not_leak_paths(share):
    msg = raises_msg(lambda: share.search.grep("("))
    assert "正規表現" in msg and str(share.root) not in msg


def test_grep_single_file_target(share):
    r = share.search.grep("needle", path="raw/a.txt")
    assert [m["path"] for m in r["matches"]] == ["raw/a.txt"]


def test_grep_path_starting_with_dash_is_a_path_not_an_option(share):
    # Review C1: a top-level folder named like an rg option must be searched as a path.
    d = share.root / "--files"
    d.mkdir()
    (d / "x.txt").write_text("needle in dash dir\n", encoding="utf-8")
    r = share.search.grep("needle", path="--files")
    assert [m["path"] for m in r["matches"]] == ["--files/x.txt"]
