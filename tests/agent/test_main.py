import sys
import asyncio
import threading
from types import SimpleNamespace

import pytest

from codex_bridge.agent import main as agent_main
from codex_bridge.agent.settings import SettingsStore


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


def make_role(tmp_path, monkeypatch=None):
    from codex_bridge.common.applog import AppLog
    notices = []
    role = agent_main.AgentRole(tmp_path, AppLog(tmp_path / "logs" / "app.jsonl"),
                                lambda title, body: notices.append((title, body)))
    return role, notices


def test_unreadable_legacy_invite_is_reported_and_the_role_still_builds(tmp_path):
    original = b"not a DPAPI invite"
    (tmp_path / "invite.bin").write_bytes(original)
    role, _ = make_role(tmp_path)
    assert role.runtime.rows() == []
    assert (tmp_path / "invite.bin").read_bytes() == original
    assert "読めません" in (tmp_path / "logs" / "log.jsonl").read_text(encoding="utf-8")


async def test_role_stops_even_if_runtime_cleanup_raises(tmp_path, monkeypatch):
    class Runtime:
        def __init__(self, *args, **kwargs):
            self.runners = {"existing": SimpleNamespace(app=SimpleNamespace())}
            self.settings = None

        async def start(self):
            pass

        async def stop(self):
            raise OSError("cleanup failed")

    monkeypatch.setattr(agent_main, "AgentConnections", Runtime)
    monkeypatch.setattr(agent_main, "resolve_codex", lambda home: tmp_path / "fake.exe")
    monkeypatch.setattr(agent_main, "codex_version", lambda cmd: "fake")
    role, _ = make_role(tmp_path)
    await role.start()
    await role.stop()
    assert "cleanup failed" in (tmp_path / "logs" / "log.jsonl").read_text(encoding="utf-8")


async def test_no_runner_starts_while_codex_is_missing(tmp_path, monkeypatch):
    started = []

    class Runtime:
        def __init__(self, *args, **kwargs):
            self.runners = {"existing": SimpleNamespace(app=SimpleNamespace())}
            self.started = False

        async def start(self):
            started.append(True)

    monkeypatch.setattr(agent_main, "AgentConnections", Runtime)
    monkeypatch.setattr(agent_main, "resolve_codex", lambda home: None)  # the user declined the download
    role, notices = make_role(tmp_path)
    await role.start()
    assert started == [] and "Codex" in role.error and notices
    assert "Codex" in role.applog.entries(side="lend")[-1]["text"]


@pytest.mark.parametrize("operation", ["replace", "add"])
@pytest.mark.parametrize("download_error", [False, True])
async def test_invite_actions_retry_codex_setup_after_startup_failure(tmp_path, monkeypatch, operation, download_error):
    import httpx
    from codex_bridge.agent.connections import ConnectionStore
    from codex_bridge.common.invite import make_invite
    from fake_helpers import Fake

    code = make_invite("http://host.invalid", "old-token", "貸す人")
    original = ConnectionStore(tmp_path).add(code)
    attempts, activated = [], []

    def resolve(home):
        attempts.append(home)
        if len(attempts) == 1 and download_error:
            raise OSError("オフラインです")
        return None if len(attempts) == 1 else tmp_path / "codex.exe"

    def fake_app(*args, **kwargs):
        root = tmp_path / f"fake-{len(list(tmp_path.glob('fake-*')))}"
        root.mkdir()
        app = Fake(root).app
        app.codex_cmd = list(args[0])
        return app

    async def activate(id):
        activated.append(id)

    monkeypatch.setattr(agent_main, "resolve_codex", resolve)
    monkeypatch.setattr(agent_main, "codex_version", lambda cmd: "fake")
    monkeypatch.setattr(agent_main, "user_mcp_names", lambda: [])
    monkeypatch.setattr(agent_main, "AppServer", fake_app)
    role, _ = make_role(tmp_path)
    monkeypatch.setattr(role.runtime, "_activate", activate)
    monkeypatch.setattr(role.runtime, "_ensure_usage_probe", lambda: None)
    try:
        await role.start()
        assert role.error and not role.runtime.started and not activated
        transport = httpx.ASGITransport(app=role.ui_app(), client=("127.0.0.1", 1))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8766") as web:
            url = f"/api/connections/{original.id}/invite" if operation == "replace" else "/api/connections"
            method = "PUT" if operation == "replace" else "POST"
            bad = await web.request(method, url, json={"code": "bad"}, headers={"X-Codex-Bridge": "1"})
            assert bad.status_code == 400 and len(attempts) == 1
            new_url = "http://host.invalid" if operation == "replace" else "http://other.invalid"
            new_code = make_invite(new_url, "new-token", "貸す人")
            response = await web.request(method, url, json={"code": new_code}, headers={"X-Codex-Bridge": "1"})
            assert response.status_code == (200 if operation == "replace" else 201), response.text
        assert len(attempts) == 2 and role.runtime.started and role.error is None
        assert set(activated) == set(role.runtime.runners)
        assert all(r.app.codex_cmd == role.cmd for r in role.runtime.runners.values())
        if operation == "replace":
            assert role.runtime.store.list()[0].invite.token == "new-token"
    finally:
        await role.stop()


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
            self.runners, self.settings = {"existing": SimpleNamespace(app=SimpleNamespace())}, None

        async def apply_shared(self, saved):
            pass

        async def start(self):
            await self.app.start()

        async def stop(self):
            await self.app.stop()

    async def respond_while_guide_is_open():
        while not opened.is_set():
            await asyncio.sleep(0)
        released.set()

    monkeypatch.setattr(agent_main, "AppServer", lambda *args, **kwargs:
                        SimpleNamespace(start=start, stop=stop, auth_mode=auth_mode))
    monkeypatch.setattr(agent_main, "AgentConnections", Runtime)
    monkeypatch.setattr(agent_main, "resolve_codex", lambda home: tmp_path / "fake.exe")
    monkeypatch.setattr(agent_main, "codex_version", lambda cmd: "fake")
    monkeypatch.setattr(agent_main, "user_mcp_names", lambda: [])
    monkeypatch.setattr(agent_main, "show_info", show_info)
    monkeypatch.setattr(agent_main, "run_login", login)
    response = asyncio.create_task(respond_while_guide_is_open())
    try:
        role, _ = make_role(tmp_path)
        await asyncio.wait_for(role.start(), 5)
        await asyncio.wait_for(role.stop(), 5)
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


def test_applog_line_maps_runner_messages():
    from codex_bridge.agent.main import applog_line
    assert applog_line({"type": "error", "text": "boom"}) == ("error", "boom")
    assert applog_line({"type": "task_state", "task_id": "t1", "state": "running"}) == ("info", "t1 を受け付けました")
    assert applog_line({"type": "task_state", "task_id": "t1", "state": "completed"}) == ("info", "t1 が終わりました")
    assert applog_line({"type": "connection", "text": "x: 接続しました"}) == ("warn", "x: 接続しました")
    assert applog_line({"type": "task_prompt", "task_id": "t1", "prompt": "secret"}) is None
