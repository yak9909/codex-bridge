import sys
import asyncio
import threading
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


def test_unreadable_legacy_invite_asks_for_a_new_code_and_starts(tmp_path, monkeypatch):
    from codex_bridge.agent.connections import ConnectionStore
    from codex_bridge.common.invite import Invite
    original = b"not a DPAPI invite"
    (tmp_path / "invite.bin").write_bytes(original)
    invited, started = [], []
    invite = Invite("http://new.invalid", "new-token", "friend")

    def ask():
        invited.append(True)
        return invite

    async def run(invite, codex, home):
        started.append((codex, home))
        assert ConnectionStore(home).list()[0].invite == invite
        return False

    monkeypatch.setattr(agent_main, "ask_invite", ask)
    monkeypatch.setattr(agent_main, "choose_codex", lambda *args: tmp_path / "codex.exe")
    monkeypatch.setattr(agent_main, "run_agent", run)
    assert agent_main._main(tmp_path) is False
    assert invited == [True] and started == [(tmp_path / "codex.exe", tmp_path)]
    assert (tmp_path / "invite.bin").read_bytes() == original
    assert "読めません" in (tmp_path / "logs" / "log.jsonl").read_text(encoding="utf-8")


async def test_run_agent_stops_the_tray_even_if_runtime_cleanup_raises(tmp_path, monkeypatch):
    stopped, refs = [], {}

    class Runtime:
        def __init__(self, *args, **kwargs):
            self.runners = {"existing": None}
            self.settings = None

        async def start(self):
            pass

        async def apply_shared(self, saved):
            pass

        async def stop(self):
            raise OSError("cleanup failed")

    def tray(on_quit, on_pause_toggle):
        refs["quit"] = on_quit
        return SimpleNamespace(run_detached=lambda: None,
                               icon=SimpleNamespace(stop=lambda: stopped.append("tray")))

    def server(*args, **kwargs):
        async def serve():
            refs["quit"]()
        return SimpleNamespace(serve=serve, should_exit=False)

    monkeypatch.setattr(agent_main, "AgentConnections", Runtime)
    monkeypatch.setattr(agent_main, "Tray", tray)
    monkeypatch.setattr(agent_main, "make_ui_server", server)
    monkeypatch.setattr(agent_main, "codex_version", lambda cmd: "fake")
    assert await asyncio.wait_for(agent_main.run_agent(None, tmp_path / "fake.exe", tmp_path), 1) is False
    assert stopped == ["tray"]
    assert "cleanup failed" in (tmp_path / "logs" / "log.jsonl").read_text(encoding="utf-8")


async def test_login_guide_keeps_event_loop_responsive(tmp_path, monkeypatch):
    from codex_bridge.agent.settings import defaults
    from codex_bridge.common.invite import Invite

    opened, released = threading.Event(), threading.Event()
    dialog_threads, released_in_time, starts, refs = [], [], [], {}
    loop_thread = threading.get_ident()
    logged_in = False

    async def start():
        starts.append(True)

    async def stop():
        pass

    async def auth_mode():
        return "chatgpt" if logged_in else "none"

    def show_info(text):
        dialog_threads.append(threading.get_ident())
        opened.set()
        released_in_time.append(released.wait(2))

    def login(cmd):
        nonlocal logged_in
        logged_in = True

    class Runtime:
        def __init__(self, home, *, app_factory, **kwargs):
            connection = SimpleNamespace(invite=Invite("http://host.invalid", "tok", "friend"))
            self.app = app_factory(connection, defaults())
            self.runners, self.settings = {"existing": None}, None

        async def apply_shared(self, saved):
            pass

        async def start(self):
            await self.app.start()

        async def stop(self):
            await self.app.stop()

    def tray(on_quit, on_pause_toggle):
        refs["quit"] = on_quit
        return SimpleNamespace(run_detached=lambda: None, icon=SimpleNamespace(stop=lambda: None))

    def server(*args, **kwargs):
        async def serve():
            refs["quit"]()
        return SimpleNamespace(serve=serve, should_exit=False)

    async def respond_while_guide_is_open():
        while not opened.is_set():
            await asyncio.sleep(0)
        released.set()

    monkeypatch.setattr(agent_main, "AppServer", lambda *args, **kwargs:
                        SimpleNamespace(start=start, stop=stop, auth_mode=auth_mode))
    monkeypatch.setattr(agent_main, "AgentConnections", Runtime)
    monkeypatch.setattr(agent_main, "Tray", tray)
    monkeypatch.setattr(agent_main, "make_ui_server", server)
    monkeypatch.setattr(agent_main, "codex_version", lambda cmd: "fake")
    monkeypatch.setattr(agent_main, "user_mcp_names", lambda: [])
    monkeypatch.setattr(agent_main, "show_info", show_info)
    monkeypatch.setattr(agent_main, "run_login", login)
    response = asyncio.create_task(respond_while_guide_is_open())
    try:
        assert await asyncio.wait_for(agent_main.run_agent(None, tmp_path / "fake.exe", tmp_path), 5) is False
        await response
    finally:
        released.set()
        response.cancel()
        await asyncio.gather(response, return_exceptions=True)
    assert released_in_time == [True], "the event loop must respond before the modal closes"
    assert len(dialog_threads) == 1 and dialog_threads[0] != loop_thread
    assert len(starts) == 2 and logged_in


@pytest.mark.parametrize("fails,already_paused", [(False, False), (True, False), (True, True)])
async def test_final_review_move_pauses_before_copy_and_restores_on_failure(tmp_path, monkeypatch,
                                                                         fails, already_paused):
    from codex_bridge.common.invite import make_invite
    from fake_helpers import Fake
    observed = []

    def factory(connection, settings):
        root = tmp_path / ("fake-" + connection.id)
        root.mkdir()
        return Fake(root).app

    runtime = agent_main.AgentConnections(tmp_path / "home", app_factory=factory, versions={})
    await runtime.add(make_invite("http://a.invalid", "tok", "friend"))
    await runtime.add(make_invite("http://b.invalid", "tok", "friend"))
    await runtime.set_paused(already_paused)

    def move(*args, **kwargs):
        observed.append((runtime.paused, all(c.paused for c in runtime.clients.values()),
                         all(not r.app.running for r in runtime.runners.values())))
        if fails:
            raise OSError("copy failed")
        return []

    monkeypatch.setattr(agent_main, "move_data_root", move)
    try:
        if fails:
            with pytest.raises(OSError, match="copy failed"):
                await runtime.move_data(tmp_path / "new")
        else:
            assert await runtime.move_data(tmp_path / "new") == []
        assert observed == [(True, True, True)]
        assert runtime.paused is (already_paused if fails else True)
        assert runtime.moving is (not fails)
    finally:
        await runtime.stop()
