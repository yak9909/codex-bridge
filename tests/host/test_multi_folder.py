import sys

import pytest

from conftest import raises_msg
from codex_bridge.host.paths import ToolError

PY = f'"{sys.executable}"'


def test_absolute_path_in_second_folder_is_readable(multi):
    r = multi.read.read_file((multi.tools / "lib.py").as_posix())
    assert r["text"] == "needle in tools\n" and r["path"] == (multi.tools / "lib.py").as_posix()


def test_backslash_absolute_path_works(multi):
    assert multi.read.read_file(str(multi.tools / "lib.py"))["text"].startswith("needle")


def test_second_folder_read_only_refuses_write(multi):
    with pytest.raises(ToolError, match="書き込みは許可されていません"):
        multi.write.write_file((multi.tools / "new.py").as_posix(), "x")


def test_writable_subfolder_of_read_folder(multi):
    multi.write.write_file((multi.tools / "build" / "out.txt").as_posix(), "ok")
    assert (multi.tools / "build" / "out.txt").read_text(encoding="utf-8") == "ok"


def test_not_registered_absolute_path_is_not_found(multi):
    target = (multi.outside / "outside.txt").as_posix()
    assert raises_msg(lambda: multi.read.read_file(target)) == f"not found: {target}"


def test_prefix_sibling_folder_not_visible(multi):
    sibling = multi.root.parent / "share2" / "x.txt"
    with pytest.raises(ToolError, match="not found"):
        multi.read.read_file(sibling.as_posix())


def test_list_root_shows_registered_folders(multi):
    entries = multi.read.list_dir("/")
    paths = {e["path"] for e in entries}
    assert multi.root.as_posix() in paths and multi.tools.as_posix() in paths
    assert all("secret" not in p for p in paths)
    assert next(e for e in entries if e["path"] == multi.root.as_posix())["main"] is True


def test_grep_and_find_cover_all_folders(multi):
    hits = {m["path"] for m in multi.search.grep("needle")["matches"]}
    assert "raw/a.txt" in hits and (multi.tools / "lib.py").as_posix() in hits
    assert not any("share2" in h or "secret" in h for h in hits)
    found = multi.read.find_files("**/*.py")
    assert (multi.tools / "lib.py").as_posix() in found


def test_grep_explicit_absolute_folder(multi):
    hits = [m["path"] for m in multi.search.grep("needle", path=multi.tools.as_posix())["matches"]]
    assert hits == [(multi.tools / "lib.py").as_posix()]


async def test_run_command_cwd_in_second_folder(multi):
    multi.set_policy(exec_mode="all")
    r = await multi.runner.run_command(f'{PY} -c "import os;print(os.listdir())"', cwd=multi.tools.as_posix())
    assert "lib.py" in r["stdout"]


async def test_changed_files_include_second_writable_folder(multi):
    multi.set_policy(exec_mode="all")
    target = (multi.tools / "build" / "made.txt").as_posix()
    r = await multi.runner.run_command(f"{PY} -c \"open(r'{target}','w').write('m')\"")
    assert target in r["changed_files"]


def test_move_between_folders_respects_levels(multi):
    with pytest.raises(ToolError, match="書き込みは許可されていません"):
        multi.write.move_path("raw/a.txt", (multi.tools / "a.txt").as_posix())
    multi.write.move_path("raw/a.txt", (multi.tools / "build" / "a.txt").as_posix())
    assert (multi.tools / "build" / "a.txt").exists()


def test_relative_paths_still_use_main(multi):
    assert multi.read.read_file("raw/a.txt")["path"] == "raw/a.txt"
