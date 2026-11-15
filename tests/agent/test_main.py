import sys
import asyncio
from types import SimpleNamespace

import pytest

from codex_bridge.agent import main as agent_main
from codex_bridge.agent.settings import SettingsStore


def test_logview_server_builds_without_console(tmp_path, monkeypatch):
    # The windowed exe has no console: sys.stdout / sys.stderr are None.
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    server = agent_main.make_ui_server(SimpleNamespace(), SettingsStore(tmp_path / "settings.json"),
                                       SimpleNamespace(), versions={}, data_root=tmp_path)
    assert server.config.port == agent_main.LOGVIEW_PORT
    assert server.config.host == "127.0.0.1" and server.config.log_config is None


def test_tray_notification_and_os_failure_do_not_interrupt_task():
    sent = []
    tray = object.__new__(agent_main.Tray)
    tray.icon = SimpleNamespace(notify=lambda body, title: sent.append((body, title)))
    tray.notify_pending("承認してください")
    assert sent == [("承認してください", "codex-bridge: 事前承認待ち")]

    def fail(*args):
        raise OSError("notifications disabled")
    tray.icon.notify = fail
    tray.notify_pending("test")  # Windows notification policy cannot fail the request


def test_ensure_std_streams_fills_missing_streams(monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    agent_main.ensure_std_streams()
    assert sys.stdout is not None and sys.stderr is not None
    print("ok")  # must not raise


def test_crash_is_written_to_log_and_reported(tmp_path, monkeypatch):
    shown = []
    monkeypatch.setattr(agent_main, "show_info", lambda text, error=False: shown.append(text))

    def boom():
        raise RuntimeError("boom")

    agent_main.run_guarded(boom, tmp_path)
    log = (tmp_path / "logs" / "crash.log").read_text(encoding="utf-8")
    assert "RuntimeError: boom" in log
    assert shown and "crash.log" in shown[0]


@pytest.mark.parametrize("fails,already_paused", [(False, False), (True, False), (True, True)])
async def test_final_review_move_pauses_before_copy_and_restores_on_failure(tmp_path, monkeypatch,
                                                                         fails, already_paused):
    from codex_bridge.common.invite import Invite
    observed, refs = [], {}

    class App:
        def __init__(self, *args, **kwargs):
            pass

        async def start(self):
            pass

        async def stop(self):
            pass

        async def auth_mode(self):
            return "chatgpt"

    class Runner:
        def __init__(self, *args, **kwargs):
            self.settings = {}

        def apply_settings(self, settings):
            self.settings = settings

        async def start(self):
            pass

        async def shutdown(self):
            pass

        async def stop(self):
            pass

    class Client:
        def __init__(self, *args, **kwargs):
            self.paused = already_paused
            refs["client"] = self

        async def drop(self):
            observed.append("drop")

        async def refresh_hello(self):
            pass

        async def run_forever(self):
            await asyncio.Future()

    class Applier:
        def __init__(self, *args, **kwargs):
            pass

        async def apply(self, settings):
            pass

        async def move_data(self, path):
            observed.append(("move", refs["client"].paused))
            if fails:
                raise OSError("copy failed")
            return []

    def tray(on_quit, on_pause_toggle):
        refs["quit"] = on_quit
        return SimpleNamespace(icon=SimpleNamespace(update_menu=lambda: None, stop=lambda: None),
                               run_detached=lambda: None, set_status=lambda s: None)

    def server(*args, on_move, **kwargs):
        async def serve():
            try:
                await on_move(tmp_path / "new")
            except OSError:
                pass
            observed.append(("after", refs["client"].paused))
            refs["quit"]()
        return SimpleNamespace(serve=serve, should_exit=False)

    for name, value in {"AppServer": App, "AgentRunner": Runner, "ControlClient": Client,
                        "SettingsApplier": Applier, "Tray": tray, "make_ui_server": server,
                        "resolve_workdir": lambda *a: tmp_path / "wd", "user_mcp_names": lambda: [],
                        "codex_version": lambda cmd: "test"}.items():
        monkeypatch.setattr(agent_main, name, value)
    result = await agent_main.run_agent(Invite("http://test", "tok", "friend"), tmp_path / "codex.exe", tmp_path)
    assert ("move", True) in observed
    assert ("after", already_paused if fails else True) in observed
    assert result is (not fails)
