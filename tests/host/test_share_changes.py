import pytest

from codex_bridge.host.app import build_host
from codex_bridge.host.config import HostConfig
from codex_bridge.host.paths import ToolError
from codex_bridge.host.share import (ShareBusy, add_shared_folder, change_share_root,
                                    remove_shared_folder, set_main_shared_folder)
from codex_bridge.host.tasks import TaskRecord


@pytest.fixture
def host(share):
    return build_host(HostConfig(share.root, share.data_dir, "127.0.0.1:8765", 8770,
                                 share.data_dir / "rg.exe"))


@pytest.fixture
def running_task(host):
    task = TaskRecord(task_id="t1", prompt="p", created=0, state="running")
    host.mgr.tasks[task.task_id] = task
    return task


def test_add_and_remove_share_during_task(host, running_task, tmp_path):
    extra = tmp_path / "extra"
    extra.mkdir()
    file = extra / "test.txt"
    add_shared_folder(host, extra.as_posix(), "write")
    assert host.paths.resolve(file.as_posix(), "write") == file.resolve()
    file.write_text("test", encoding="utf-8")
    assert host.paths.resolve(file.as_posix(), "read").read_text(encoding="utf-8") == "test"

    remove_shared_folder(host, extra.as_posix())
    for need in ("read", "write"):
        with pytest.raises(ToolError, match="not found"):
            host.paths.resolve(file.as_posix(), need)
    assert file.read_text(encoding="utf-8") == "test"


def test_main_removal_requires_every_active_task_to_end(host, running_task, tmp_path):
    extra = tmp_path / "extra"
    extra.mkdir()
    add_shared_folder(host, extra.as_posix(), "read")
    original = host.paths.root
    folders = host.paths.roots()
    second = TaskRecord(task_id="t2", prompt="p", created=0, state="running")
    host.mgr.tasks[second.task_id] = second

    def assert_main_removal_blocked():
        with pytest.raises(ShareBusy) as error:
            remove_shared_folder(host, original.as_posix())
        assert str(error.value) == "作業中はメインのフォルダを外せません。終わってから外してください"
        assert host.paths.root == original
        assert host.paths.roots() == folders

    assert host.mgr.active_task_ids() == [running_task.task_id, second.task_id]
    assert_main_removal_blocked()
    running_task.state = "completed"
    assert host.mgr.active_task_ids() == [second.task_id]
    assert_main_removal_blocked()

    second.state = "completed"
    remove_shared_folder(host, original.as_posix())
    assert host.paths.root == original / "out"
    assert original.as_posix() not in {folder["path"] for folder in host.paths.roots()}


@pytest.mark.parametrize("switch", [set_main_shared_folder, change_share_root])
def test_main_switch_requires_no_active_task(host, running_task, tmp_path, switch):
    extra = tmp_path / "extra"
    extra.mkdir()
    add_shared_folder(host, extra.as_posix(), "read")
    original = host.paths.root
    with pytest.raises(ShareBusy) as error:
        switch(host, extra.as_posix())
    assert str(error.value) == "作業中はメインのフォルダを切り替えられません。終わってから切り替えてください"
    assert host.paths.root == original
    assert host.paths.level_of(extra) == "read"

    running_task.state = "completed"
    switch(host, extra.as_posix())
    assert host.paths.root == extra.resolve()
