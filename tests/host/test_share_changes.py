import subprocess
from pathlib import Path

import pytest

from codex_bridge.host.app import build_host
from codex_bridge.host.config import HostConfig
from codex_bridge.host.paths import ToolError
from codex_bridge.host.policy_form import form_from_raw, yaml_from_form
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
        assert str(error.value) == "作業中はメインのフォルダを外せません（メインのフォルダが共有から外れるため）。終わってから外してください"
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



def test_cannot_remove_main_registered_through_junction_during_work(host, running_task, tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    junction = tmp_path / "junction"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(target)],
                   check=True, capture_output=True)
    running_task.state = "completed"
    add_shared_folder(host, junction.as_posix(), "write")
    set_main_shared_folder(host, junction.as_posix())
    assert host.paths.root == target.resolve()
    folders = host.paths.roots()

    running_task.state = "running"
    with pytest.raises(ShareBusy, match="作業中はメインのフォルダを外せません"):
        remove_shared_folder(host, junction.as_posix())
    assert host.paths.root == target.resolve()
    assert host.paths.roots() == folders


def test_removing_parent_of_main_requires_idle_and_promotes_next_folder(host, running_task, tmp_path):
    original = host.paths.root
    outer = tmp_path / "outer"
    nested = outer / "nested"
    nested.mkdir(parents=True)
    running_task.state = "completed"
    add_shared_folder(host, outer.as_posix(), "write")
    set_main_shared_folder(host, nested.as_posix())
    assert host.paths.root == nested.resolve()
    folders = host.paths.roots()

    running_task.state = "running"
    with pytest.raises(ShareBusy) as error:
        remove_shared_folder(host, outer.as_posix())
    assert str(error.value) == "作業中はメインのフォルダを外せません（メインのフォルダが共有から外れるため）。終わってから外してください"
    assert host.paths.root == nested.resolve()
    assert host.paths.roots() == folders

    running_task.state = "completed"
    remove_shared_folder(host, outer.as_posix())
    assert host.paths.root == host.config.share_root == original
    assert host.paths.level_of(nested) is None



@pytest.mark.parametrize("state", ["running", "completed"])
@pytest.mark.parametrize("removed", ["parent", "junction"])
def test_removal_keeps_main_that_is_still_shared(host, running_task, tmp_path, state, removed):
    outer = tmp_path / "outer"
    project = outer / "project"
    project.mkdir(parents=True)
    running_task.state = "completed"
    if removed == "parent":
        add_shared_folder(host, outer.as_posix(), "read")
        add_shared_folder(host, project.as_posix(), "write")
        folder = outer
    else:
        add_shared_folder(host, outer.as_posix(), "write")
        folder = tmp_path / "alias"
        subprocess.run(["cmd", "/c", "mklink", "/J", str(folder), str(outer)],
                       check=True, capture_output=True)
        add_shared_folder(host, folder.as_posix(), "read")
    set_main_shared_folder(host, project.as_posix())
    expected = project.resolve()

    running_task.state = state
    remove_shared_folder(host, folder.as_posix())

    assert host.paths.root == host.config.share_root == expected
    assert host.paths.resolve(".", "write") == expected
    assert folder.as_posix() not in {row["path"] for row in host.paths.roots()}


def test_remove_unreachable_unc_registration_without_probing_path(host, running_task, monkeypatch):
    unc = Path(r"\\unreachable.invalid\share\folder")
    form = form_from_raw(host.policy.as_dict())
    form["folders"].append({"path": unc.as_posix(), "level": "read"})
    host.policy.path.write_text(yaml_from_form(form), encoding="utf-8")
    original = host.paths.root
    assert host.policy.current().level_for_abs(unc) == "read"

    resolve, stat = Path.resolve, Path.stat

    def reject_unc_probe(operation):
        def guarded(path, *args, **kwargs):
            assert not path.is_relative_to(unc), f"Probed the unreachable share: {path}"
            return operation(path, *args, **kwargs)
        return guarded

    monkeypatch.setattr(Path, "resolve", reject_unc_probe(resolve))
    monkeypatch.setattr(Path, "stat", reject_unc_probe(stat))
    remove_shared_folder(host, unc.as_posix())

    assert host.paths.root == original
    assert host.policy.current().level_for_abs(unc) is None


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
