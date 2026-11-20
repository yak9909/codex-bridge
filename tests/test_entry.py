"""Unified entry point; no test may open a real window, console or browser."""

import ast
import asyncio
from contextlib import contextmanager
import io
import sys
import runpy
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from codex_bridge import entry, resident
from codex_bridge.agent import main as agent_main
from codex_bridge.host import main as host_main


def test_entry_hook_dispatch_skips_console(monkeypatch):
    from codex_bridge import hook

    unexpected = Mock(side_effect=AssertionError("hook must skip the console and dialog"))
    monkeypatch.setattr(entry, "attach_console", unexpected)
    hook_main = Mock(return_value=7)
    monkeypatch.setattr(hook, "main", hook_main)
    assert entry.main(["hook", "codex"]) == 7
    hook_main.assert_called_once_with(["codex"])
    unexpected.assert_not_called()


def test_python_m_codex_bridge_hook():
    result = subprocess.run([sys.executable, "-m", "codex_bridge", "hook", "codex", "--port", "1"],
                            input=b"{}", capture_output=True, timeout=20)
    assert result.returncode == 0
    assert result.stdout == b""
    assert result.stderr == b""


@pytest.fixture
def launch(tmp_path, monkeypatch):
    install = tmp_path / "install"
    install.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(install / "codex-bridge.exe"))
    console = Mock(return_value=False)
    resident_main = Mock()
    browser = Mock()
    calls = []
    host = Mock(side_effect=lambda: calls.append((sys.argv[1:], Path.cwd())))
    monkeypatch.setattr(entry, "attach_console", console)
    monkeypatch.setattr(entry, "acquire_agent_instance", Mock(return_value=(True, None)))
    monkeypatch.setattr(host_main, "main", host)
    monkeypatch.setattr(resident, "main", resident_main)
    monkeypatch.setattr(entry.webbrowser, "open", browser)
    monkeypatch.setattr(entry, "input", Mock(side_effect=AssertionError("unexpected pause")), raising=False)
    return SimpleNamespace(install=install, elsewhere=elsewhere, console=console,
                           resident=resident_main, host=host, browser=browser, calls=calls)


def test_no_args_runs_resident(launch):
    assert entry.main([]) == 0
    launch.resident.assert_called_once_with()
    launch.console.assert_not_called()  # no console window, no role dialog
    launch.host.assert_not_called()
    launch.browser.assert_not_called()


def test_agent_arg_runs_resident(launch):
    assert entry.main(["agent"]) == 0
    launch.resident.assert_called_once_with()
    launch.console.assert_not_called()
    launch.host.assert_not_called()


def test_second_launch_opens_browser(launch, monkeypatch):
    monkeypatch.setattr(entry, "acquire_agent_instance", Mock(return_value=(False, None)))
    assert entry.main([]) == 0
    launch.browser.assert_called_once_with("http://127.0.0.1:8766/")
    launch.resident.assert_not_called()


def test_second_launch_follows_the_ui_port_override(launch, monkeypatch):
    monkeypatch.setenv("CODEX_BRIDGE_UI_PORT", "18766")
    monkeypatch.setattr(entry, "acquire_agent_instance", Mock(return_value=(False, None)))
    assert entry.main(["agent"]) == 0
    launch.browser.assert_called_once_with("http://127.0.0.1:18766/")


def test_host_cli_unchanged(launch):
    argv = ["host", "start", "--config", "host.yaml"]
    previous_argv = sys.argv
    assert entry.main(argv) == 0
    assert launch.calls == [(["start", "--config", "host.yaml"], launch.elsewhere)]
    assert argv == ["host", "start", "--config", "host.yaml"]
    assert sys.argv is previous_argv
    launch.console.assert_called_once_with()
    launch.resident.assert_not_called()


def test_console_script_reads_process_args(launch, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["codex-bridge", "host", "stop", "--config", "other.yaml"])
    assert entry.main() == 0
    assert launch.calls == [(["stop", "--config", "other.yaml"], launch.elsewhere)]
    launch.resident.assert_not_called()


@pytest.mark.parametrize("argv", [["foo"], ["agent", "unexpected"]])
def test_invalid_args_show_usage_and_return_two(launch, capsys, argv):
    assert entry.main(argv) == 2
    assert "使い方" in capsys.readouterr().err
    launch.resident.assert_not_called()
    launch.host.assert_not_called()


def test_host_error_restores_process_state(launch):
    launch.host.side_effect = SystemExit(3)
    previous_argv = sys.argv
    with pytest.raises(SystemExit) as error:
        entry.main(["host", "start"])
    assert error.value.code == 3
    assert sys.argv is previous_argv
    assert Path.cwd() == launch.elsewhere


@pytest.fixture
def console_api(monkeypatch):
    def processes(buffer, capacity):
        for index, pid in enumerate(state.pids[:capacity]):
            buffer[index] = pid
        return len(state.pids)

    kernel = SimpleNamespace(GetConsoleWindow=Mock(return_value=123),
                             GetConsoleProcessList=Mock(side_effect=processes),
                             AttachConsole=Mock(return_value=True), AllocConsole=Mock(return_value=True))
    user = SimpleNamespace(ShowWindow=Mock())
    monkeypatch.setattr(entry.ctypes, "windll", SimpleNamespace(kernel32=kernel, user32=user), raising=False)
    dll = Mock(return_value=kernel)
    last_error = Mock(return_value=0)
    monkeypatch.setattr(entry.ctypes, "WinDLL", dll, raising=False)
    monkeypatch.setattr(entry.ctypes, "get_last_error", last_error, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(entry.os, "getpid", lambda: 102)
    monkeypatch.setattr(entry.os, "getppid", lambda: 101)
    # Real in-memory streams stand in for valid redirected stdout/stderr/stdin.
    streams = [io.StringIO(), io.StringIO(), io.StringIO("redirected input\n")]
    def redirect_streams():
        # Set streams during the test call; pytest resets its capture after fixture setup.
        for name, stream in zip(("stdout", "stderr", "stdin"), streams):
            monkeypatch.setattr(sys, name, stream)
    opened = Mock(side_effect=AssertionError("must preserve valid standard streams"))
    monkeypatch.setattr(entry, "open", opened, raising=False)
    state = SimpleNamespace(kernel=kernel, user=user, pids=[100, 101, 102], streams=streams, opened=opened,
                            redirect_streams=redirect_streams, dll=dll, last_error=last_error)
    return state


def test_inherited_console_preserves_window_and_redirection(console_api):
    console_api.redirect_streams()
    assert entry.attach_console() is False
    console_api.kernel.AttachConsole.assert_not_called()
    console_api.kernel.AllocConsole.assert_not_called()
    console_api.user.ShowWindow.assert_not_called()
    console_api.opened.assert_not_called()
    print("redirected output")
    assert sys.stdout.getvalue() == "redirected output\n"
    assert input() == "redirected input"


def test_owned_onefile_console_is_shown_without_allocating(console_api):
    # Explorer -> bootloader (101) -> Python (102); there is no shell sharing this console.
    console_api.pids = [101, 102]
    assert entry.attach_console() is True
    console_api.user.ShowWindow.assert_called_once_with(123, 5)  # SW_SHOW
    console_api.kernel.AttachConsole.assert_not_called()
    console_api.kernel.AllocConsole.assert_not_called()
    console_api.opened.assert_not_called()


def test_source_console_shared_with_parent_is_not_owned(console_api, monkeypatch):
    monkeypatch.setattr(sys, "frozen", False)
    console_api.pids = [101, 102]
    assert entry.attach_console() is False
    console_api.user.ShowWindow.assert_not_called()


@pytest.mark.parametrize("attached", [True, False])
def test_missing_console_attaches_before_allocating(console_api, attached):
    console_api.kernel.GetConsoleWindow.side_effect = [0, 123]
    console_api.kernel.AttachConsole.return_value = attached
    assert entry.attach_console() is (not attached)
    console_api.kernel.AttachConsole.assert_called_once_with(-1)
    assert console_api.kernel.AllocConsole.call_count == int(not attached)
    assert console_api.user.ShowWindow.call_count == int(not attached)
    console_api.opened.assert_not_called()


@pytest.fixture
def windowless_console(console_api):
    console_api.kernel.GetConsoleWindow.return_value = 0
    console_api.kernel.AttachConsole.return_value = False
    console_api.kernel.AllocConsole.return_value = False
    console_api.last_error.return_value = 5  # ERROR_ACCESS_DENIED: already attached
    # A onefile build can be alone with its bootloader in a windowless console.
    console_api.pids = [101, 102]
    return console_api


def test_windowless_inherited_console_keeps_pipes_and_is_not_owned(windowless_console):
    windowless_console.redirect_streams()
    assert entry.attach_console() is False
    windowless_console.dll.assert_called_once_with("kernel32", use_last_error=True)
    windowless_console.last_error.assert_called_once_with()
    windowless_console.kernel.AttachConsole.assert_called_once_with(-1)
    windowless_console.kernel.AllocConsole.assert_called_once_with()
    windowless_console.kernel.GetConsoleProcessList.assert_not_called()
    windowless_console.user.ShowWindow.assert_not_called()
    windowless_console.opened.assert_not_called()
    assert [sys.stdout, sys.stderr, sys.stdin] == windowless_console.streams
    print("piped output")
    print("piped error", file=sys.stderr)
    assert sys.stdout.getvalue() == "piped output\n"
    assert sys.stderr.getvalue() == "piped error\n"
    assert input() == "redirected input"


@pytest.mark.parametrize("argv,code", [(["foo"], 2), (["host", "--help"], 0)])
def test_windowless_console_preserves_cli_exit_codes(windowless_console, monkeypatch, argv, code):
    windowless_console.redirect_streams()
    pause = Mock(side_effect=AssertionError("unexpected pause"))
    monkeypatch.setattr(entry, "input", pause, raising=False)
    if argv[0] == "host":
        with pytest.raises(SystemExit) as error:
            entry.main(argv)
        assert error.value.code == code
        assert "codex-bridge host" in sys.stdout.getvalue()
    else:
        assert entry.main(argv) == code
        assert "使い方" in sys.stderr.getvalue()
    pause.assert_not_called()
    windowless_console.opened.assert_not_called()


def test_console_allocation_other_error_is_preserved(windowless_console):
    windowless_console.last_error.return_value = 87
    with pytest.raises(OSError) as error:
        entry.attach_console()
    assert error.value.winerror == 87
    windowless_console.last_error.assert_called_once_with()
    windowless_console.opened.assert_not_called()


@pytest.mark.parametrize("invalid", ["missing", "closed"])
def test_only_invalid_standard_streams_are_reopened(console_api, monkeypatch, invalid):
    console_api.redirect_streams()
    broken = None if invalid == "missing" else io.StringIO()
    if broken is not None:
        broken.close()
    monkeypatch.setattr(sys, "stderr", broken)
    replacement = io.StringIO()
    console_api.opened.side_effect = None
    console_api.opened.return_value = replacement
    assert entry.attach_console() is False
    console_api.opened.assert_called_once_with("CONOUT$", "w", encoding="utf-8", buffering=1)
    assert sys.stderr is replacement
    assert sys.stdout is console_api.streams[0]
    assert sys.stdin is console_api.streams[2]


@pytest.mark.parametrize("argument,code,stream", [("--help", 0, "out"), ("bad-command", 2, "err")])
def test_host_help_and_errors_use_unified_command(monkeypatch, capsys, argument, code, stream):
    monkeypatch.setattr(sys, "argv", ["codex-bridge.exe", argument])
    with pytest.raises(SystemExit) as error:
        host_main.main()
    assert error.value.code == code
    output = getattr(capsys.readouterr(), stream)
    assert "codex-bridge host" in output
    assert "codex-bridge-host" not in output


def test_onefile_uses_native_console_with_early_hiding():
    path = Path(__file__).resolve().parents[1] / "codex-bridge.spec"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    exe = next(node for node in ast.walk(tree)
               if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "EXE")
    options = {option.arg: ast.literal_eval(option.value) for option in exe.keywords}
    assert options["console"] is True
    assert options["hide_console"] == "hide-early"


def test_agent_instance_handle_closes_on_runner_failure(launch, monkeypatch):
    closed = Mock()
    monkeypatch.setattr(entry, "acquire_agent_instance", Mock(return_value=(True, 123)))
    monkeypatch.setattr(entry.ctypes, "windll", SimpleNamespace(kernel32=SimpleNamespace(CloseHandle=closed)),
                        raising=False)
    launch.resident.side_effect = RuntimeError("test failure")
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


def _fake_build_tree(tmp_path, monkeypatch, extra=()):
    script = Path(__file__).resolve().parents[1] / "scripts" / "build_release.py"
    build = runpy.run_path(str(script))["main"]
    monkeypatch.setitem(build.__globals__, "ROOT", tmp_path)
    monkeypatch.setitem(build.__globals__, "VERSION", "0.2.0")
    run = Mock()
    monkeypatch.setitem(build.__globals__, "run", run)
    sources = ["dist/codex-bridge.exe", "vendor/rg.exe", "setup.bat", "start.bat", "stop.bat",
               "install-mcp.bat", "docs/operation-guide.md", "docs/friend-guide.md"]
    for name in [*sources, *extra]:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode("utf-8"))
    return build, run


def test_build_makes_one_zip(tmp_path, monkeypatch):
    build, run = _fake_build_tree(tmp_path, monkeypatch)
    build()
    run.assert_any_call(sys.executable, "-m", "PyInstaller", "--noconfirm", "codex-bridge.spec")
    assert len([call for call in run.call_args_list if "PyInstaller" in call.args]) == 1
    assert sorted(p.name for p in (tmp_path / "release").iterdir()) == ["codex-bridge-v0.2.0.zip"]
    contents = {"codex-bridge.exe", "setup.bat", "start.bat", "stop.bat", "install-mcp.bat",
                "vendor/rg.exe", "docs/operation-guide.md", "docs/friend-guide.md", "はじめにお読みください.txt"}
    with zipfile.ZipFile(tmp_path / "release" / "codex-bridge-v0.2.0.zip") as archive:
        assert set(archive.namelist()) == {f"codex-bridge-v0.2.0/{path}" for path in contents}
        readme = archive.read("codex-bridge-v0.2.0/はじめにお読みください.txt")
    assert readme.startswith(b"\xef\xbb\xbf") and b"\r\n" not in readme
    text = readme.decode("utf-8-sig")
    assert "貸す人の方へ" in text and "依頼主の方へ" in text
    assert "http://127.0.0.1:8766/" in text and "タスクトレイ" in text
    assert "friend-kit" not in text and "v0.2.0" in text
    assert "docs\\friend-guide.md" in text and "docs\\operation-guide.md" in text
    assert "\x0c" not in text


def test_build_reports_a_locked_same_version_zip(tmp_path, monkeypatch):
    build, _ = _fake_build_tree(tmp_path, monkeypatch, extra=["release/codex-bridge-v0.2.0.zip"])
    locked = tmp_path / "release" / "codex-bridge-v0.2.0.zip"
    real_unlink = Path.unlink

    def unlink(self, *args, **kwargs):
        if self == locked:
            raise PermissionError(13, "Access is denied", str(self))
        return real_unlink(self, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", unlink)
    with pytest.raises(SystemExit) as raised:
        build()
    assert raised.value.code == "release\\codex-bridge-v0.2.0.zip が開かれています。7-Zip などを閉じてからもう一度実行してください。"


def test_build_removes_same_version_split_zips_only(tmp_path, monkeypatch):
    keep = ["release/codex-bridge-admin-v0.1.0.zip", "release/old-build/keep.txt"]
    remove = ["release/codex-bridge-admin-v0.2.0.zip", "release/codex-bridge-friend-v0.2.0.zip"]
    build, _ = _fake_build_tree(tmp_path, monkeypatch, extra=[*keep, *remove])
    build()
    for name in remove:
        assert not (tmp_path / name).exists()
    for name in keep:
        assert (tmp_path / name).read_bytes() == name.encode("utf-8")
    assert (tmp_path / "release" / "codex-bridge-v0.2.0.zip").exists()


@pytest.mark.parametrize("acquired", [True, False])
def test_update_acknowledges_only_after_claim_and_resident_start(launch, monkeypatch, acquired):
    from codex_bridge import updater

    events = []
    stage = launch.install / (updater.INSTALL_STAGE + "abcdefgh")
    stage.mkdir()

    def claim():
        events.append("mutex")
        return acquired, None

    @contextmanager
    def previous_process(pid):
        assert pid == 1234
        def wait():
            assert (stage / "ready").is_file()
            events.append("old process exited")
        yield wait

    def start(*, update_ready):
        assert not (stage / "ready").exists()
        events.append("screen listening")
        asyncio.run(update_ready())
        return 0

    monkeypatch.setattr(entry, "acquire_agent_instance", claim)
    monkeypatch.setattr(entry, "_previous_process", previous_process)
    launch.resident.side_effect = start
    assert entry.main([updater.RESTART_ARG, stage.name, "1234"]) == (0 if acquired else 1)
    assert events == (["mutex", "screen listening", "old process exited"] if acquired else ["mutex"])
    assert (stage / "ready").exists() is acquired
    launch.browser.assert_not_called()

