import sys
from types import SimpleNamespace

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
