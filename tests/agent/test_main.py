import sys

from codex_bridge.agent import main as agent_main
from codex_bridge.agent.logview import LogStore


def test_logview_server_builds_without_console(tmp_path, monkeypatch):
    # The windowed exe has no console: sys.stdout / sys.stderr are None.
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    server = agent_main.make_logview_server(LogStore(tmp_path / "logs"))
    assert server.config.port == agent_main.LOGVIEW_PORT


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
