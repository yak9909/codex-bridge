"""Unified entry point; no test may open a real window, console or browser."""

import sys
import runpy
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from codex_bridge import entry
from codex_bridge.agent import main as agent_main
from codex_bridge.host import main as host_main


@pytest.fixture
def launch(tmp_path, monkeypatch):
    install = tmp_path / "install"
    install.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(install / "codex-bridge.exe"))
    dialog = Mock(return_value=None)
    console = Mock()
    agent = Mock()
    browser = Mock()
    calls = []
    host = Mock(side_effect=lambda: calls.append((sys.argv[1:], Path.cwd())))
    monkeypatch.setattr(entry, "choose_role", dialog)
    monkeypatch.setattr(entry, "attach_console", console)
    monkeypatch.setattr(entry, "acquire_agent_instance", Mock(return_value=(True, None)))
    monkeypatch.setattr(host_main, "main", host)
    monkeypatch.setattr(agent_main, "main", agent)
    monkeypatch.setattr(entry.webbrowser, "open", browser)
    return SimpleNamespace(install=install, elsewhere=elsewhere, dialog=dialog, console=console,
                           agent=agent, host=host, browser=browser, calls=calls)


@pytest.mark.parametrize("configured,command", [(False, "setup"), (True, "start")])
def test_no_args_host_uses_config_next_to_exe(launch, configured, command):
    if configured:
        (launch.install / "host.yaml").write_text("# test only", encoding="utf-8")
    # A config in the caller's directory must not affect the choice.
    (launch.elsewhere / "host.yaml").write_text("# wrong location", encoding="utf-8")
    launch.dialog.return_value = "host"
    previous_argv = sys.argv
    assert entry.main([]) == 0
    launch.dialog.assert_called_once_with()
    launch.console.assert_called_once_with()
    assert launch.calls == [([command, "--config", "host.yaml"], launch.install)]
    assert Path.cwd() == launch.elsewhere
    assert sys.argv is previous_argv
    launch.agent.assert_not_called()


def test_no_args_agent_uses_existing_runner(launch):
    launch.dialog.return_value = "agent"
    assert entry.main([]) == 0
    launch.dialog.assert_called_once_with()
    launch.agent.assert_called_once_with()
    launch.console.assert_not_called()
    launch.host.assert_not_called()


def test_cancel_exits_without_starting_anything(launch):
    assert entry.main([]) == 0
    launch.dialog.assert_called_once_with()
    launch.host.assert_not_called()
    launch.agent.assert_not_called()
    launch.console.assert_not_called()


def test_explicit_host_passes_remaining_args_without_dialog(launch):
    argv = ["host", "start", "--config", "host.yaml"]
    previous_argv = sys.argv
    assert entry.main(argv) == 0
    assert launch.calls == [(["start", "--config", "host.yaml"], launch.elsewhere)]
    assert argv == ["host", "start", "--config", "host.yaml"]
    assert sys.argv is previous_argv
    launch.dialog.assert_not_called()
    launch.console.assert_called_once_with()


def test_explicit_agent_skips_role_dialog(launch):
    assert entry.main(["agent"]) == 0
    launch.agent.assert_called_once_with()
    launch.dialog.assert_not_called()
    launch.console.assert_not_called()


def test_console_script_reads_process_args(launch, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["codex-bridge", "host", "stop", "--config", "other.yaml"])
    assert entry.main() == 0
    assert launch.calls == [(["stop", "--config", "other.yaml"], launch.elsewhere)]
    launch.dialog.assert_not_called()


def test_source_dialog_host_uses_working_directory(launch, monkeypatch):
    monkeypatch.setattr(sys, "frozen", False)
    launch.dialog.return_value = "host"
    assert entry.main([]) == 0
    assert launch.calls == [(["setup", "--config", "host.yaml"], launch.elsewhere)]


def test_running_agent_opens_existing_page(launch, monkeypatch):
    monkeypatch.setattr(entry, "acquire_agent_instance", Mock(return_value=(False, None)))
    launch.dialog.return_value = "agent"
    assert entry.main([]) == 0
    launch.browser.assert_called_once_with("http://127.0.0.1:8766/")
    launch.agent.assert_not_called()


@pytest.mark.parametrize("argv", [["foo"], ["agent", "unexpected"]])
def test_invalid_args_show_usage_and_return_two(launch, capsys, argv):
    assert entry.main(argv) == 2
    assert "使い方" in capsys.readouterr().err
    launch.dialog.assert_not_called()
    launch.agent.assert_not_called()
    launch.host.assert_not_called()


def test_host_error_restores_process_state(launch):
    launch.dialog.return_value = "host"
    launch.host.side_effect = SystemExit(3)
    previous_argv = sys.argv
    with pytest.raises(SystemExit) as error:
        entry.main([])
    assert error.value.code == 3
    assert sys.argv is previous_argv
    assert Path.cwd() == launch.elsewhere


@pytest.mark.parametrize("attached", [True, False])
def test_console_attaches_or_allocates_and_reopens_streams(monkeypatch, attached):
    kernel = SimpleNamespace(AttachConsole=Mock(return_value=attached), AllocConsole=Mock(return_value=True))
    monkeypatch.setattr(entry.ctypes, "windll", SimpleNamespace(kernel32=kernel), raising=False)
    monkeypatch.setattr(entry.sys, "platform", "win32")
    streams = [Mock(), Mock(), Mock()]
    opened = Mock(side_effect=streams)
    monkeypatch.setattr(entry, "open", opened, raising=False)
    # Make stream replacement reversible even when pytest is capturing output.
    for name in ("stdout", "stderr", "stdin"):
        monkeypatch.setattr(sys, name, getattr(sys, name))
    entry.attach_console()
    kernel.AttachConsole.assert_called_once_with(-1)
    assert kernel.AllocConsole.call_count == (0 if attached else 1)
    assert [(call.args[0], call.args[1]) for call in opened.call_args_list] == [
        ("CONOUT$", "w"), ("CONOUT$", "w"), ("CONIN$", "r")]
    assert [sys.stdout, sys.stderr, sys.stdin] == streams


def test_agent_instance_handle_closes_on_runner_failure(launch, monkeypatch):
    closed = Mock()
    monkeypatch.setattr(entry, "acquire_agent_instance", Mock(return_value=(True, 123)))
    monkeypatch.setattr(entry.ctypes, "windll", SimpleNamespace(kernel32=SimpleNamespace(CloseHandle=closed)),
                        raising=False)
    launch.agent.side_effect = RuntimeError("test failure")
    with pytest.raises(RuntimeError, match="test failure"):
        entry.main(["agent"])
    closed.assert_called_once_with(123)


@pytest.mark.parametrize("exists", [False, True])
def test_named_marker_rejects_duplicate_without_leaking_handle(monkeypatch, exists):
    kernel = SimpleNamespace(CreateMutexW=Mock(return_value=2**40), CloseHandle=Mock(),
                             GetLastError=Mock(return_value=183 if exists else 0))
    monkeypatch.setattr(entry.ctypes, "windll", SimpleNamespace(kernel32=kernel), raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    assert entry.acquire_agent_instance() == ((False, None) if exists else (True, 2**40))
    kernel.CreateMutexW.assert_called_once_with(None, False, r"Local\codex-bridge-agent")
    assert kernel.CloseHandle.call_count == int(exists)


def test_agent_relaunch_releases_marker_before_starting_same_role(launch, monkeypatch):
    events = []
    monkeypatch.setattr(entry, "_agent_handle", 123)
    kernel = SimpleNamespace(CloseHandle=lambda handle: events.append(("closed", handle)))
    monkeypatch.setattr(entry.ctypes, "windll", SimpleNamespace(kernel32=kernel), raising=False)

    def popen(argv, **kwargs):
        events.append(("started", argv))
        assert kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"

    monkeypatch.setattr(agent_main.subprocess, "Popen", popen)
    agent_main.start_again()
    assert events == [("closed", 123), ("started", [sys.executable, "agent"])]
    entry.release_agent_instance()  # the entry's finally block must not close it twice
    assert len(events) == 2


def test_friend_kit_contains_unified_exe_and_guide(tmp_path, monkeypatch):
    from codex_bridge.host import setup_cli

    (tmp_path / "codex-bridge.exe").write_bytes(b"test executable")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "friend-guide.md").write_text("test guide", encoding="utf-8")
    build = Mock(side_effect=AssertionError("must use the shipped exe"))
    browser = Mock()
    monkeypatch.setattr(setup_cli.subprocess, "run", build)
    monkeypatch.setattr(setup_cli.os, "startfile", browser, raising=False)
    kit = setup_cli.build_friend_kit(tmp_path, open_folder=False)
    assert kit == tmp_path / "friend-kit"
    assert sorted(path.name for path in kit.iterdir()) == ["codex-bridge.exe", "使い方.md"]
    assert (kit / "codex-bridge.exe").read_bytes() == b"test executable"
    assert (kit / "使い方.md").read_text(encoding="utf-8") == "test guide"
    build.assert_not_called()
    browser.assert_not_called()


def test_friend_kit_source_build_uses_unified_spec(tmp_path, monkeypatch):
    from codex_bridge.host import setup_cli

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "friend-guide.md").write_text("guide", encoding="utf-8")

    def build(argv, **kwargs):
        assert argv == [sys.executable, "-m", "PyInstaller", "--noconfirm", str(tmp_path / "codex-bridge.spec")]
        assert kwargs["cwd"] == tmp_path
        (tmp_path / "dist").mkdir()
        (tmp_path / "dist" / "codex-bridge.exe").write_bytes(b"fake built exe")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(setup_cli.subprocess, "run", build)
    kit = setup_cli.build_friend_kit(tmp_path, open_folder=False)
    assert (kit / "codex-bridge.exe").read_bytes() == b"fake built exe"


def test_release_packages_one_exe_and_preserves_existing_releases(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[1] / "scripts" / "build_release.py"
    build = runpy.run_path(str(script))["main"]
    monkeypatch.setitem(build.__globals__, "ROOT", tmp_path)
    monkeypatch.setitem(build.__globals__, "VERSION", "0.2.0")
    run = Mock()
    monkeypatch.setitem(build.__globals__, "run", run)
    files = ["dist/codex-bridge.exe", "vendor/rg.exe", "setup.bat", "start.bat", "stop.bat",
             "install-mcp.bat", "docs/operation-guide.md", "docs/friend-guide.md",
             "release/codex-bridge-admin-v0.1.0.zip", "release/old-build/keep.txt"]
    for name in files:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode("utf-8"))
    build()
    run.assert_any_call(sys.executable, "-m", "PyInstaller", "--noconfirm", "codex-bridge.spec")
    assert len([call for call in run.call_args_list if "PyInstaller" in call.args]) == 1
    expected = {
        "admin": {"codex-bridge.exe", "setup.bat", "start.bat", "stop.bat", "install-mcp.bat",
                  "vendor/rg.exe", "docs/operation-guide.md", "docs/friend-guide.md", "はじめにお読みください.txt"},
        "friend": {"codex-bridge.exe", "使い方.md"},
    }
    for role, contents in expected.items():
        name = f"codex-bridge-{role}-v0.2.0"
        with zipfile.ZipFile(tmp_path / "release" / f"{name}.zip") as archive:
            assert set(archive.namelist()) == {f"{name}/{path}" for path in contents}
    for name in files[-2:]:
        assert (tmp_path / name).read_bytes() == name.encode("utf-8")
