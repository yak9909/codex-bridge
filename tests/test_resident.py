"""The resident app: fake roles, no real tray, no real Codex, no fixed ports, nothing under %LOCALAPPDATA%."""

import asyncio
import re
import socket
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml
from fastapi import FastAPI

from codex_bridge import resident
from codex_bridge.agent import main as agent_main
from codex_bridge.common.applog import AppLog
from codex_bridge.common.invite import make_invite
from codex_bridge.host.app import build_host, create_local_app
from codex_bridge.host.config import load_host_config
from codex_bridge.host.main import ensure_config
from codex_bridge.host.role import HostRole, HostStartError, bind_socket
from codex_bridge.host.setup_cli import run_setup
from codex_bridge.resident import Resident, UiServer, create_resident_app

H = {"X-Codex-Bridge": "1"}
PAGE = "http://127.0.0.1:8766/"


def make_host(tmp_path, **overrides):
    share = tmp_path / "share"
    share.mkdir(exist_ok=True)
    cfg = tmp_path / "host.yaml"
    cfg.write_text(yaml.safe_dump({"share_root": str(share), "data_dir": str(tmp_path / "data"),
                                   "public_bind": "100.64.0.1:8765", "local_port": 8770,
                                   "rg_path": str(tmp_path / "rg.exe"), **overrides}), encoding="utf-8")
    ensure_config(cfg)
    return build_host(load_host_config(cfg), config_path=cfg)


class FakeHostRole:
    def __init__(self, host=None, error=None):
        self.host, self.error, self.running = host, error, False
        self.start_error = error
        self.starts = 0
        self.stops = 0

    async def start(self):
        self.starts += 1
        if self.start_error:
            self.error = self.start_error
            raise HostStartError(self.start_error)
        self.running = True

    async def stop(self):
        self.stops += 1
        self.running = False

    def counts(self):
        return (1, 2, 3)


class FakeAgent:
    def __init__(self, connections=0):
        self.paused = False
        self.connections = connections
        self.on_moved = None

    async def set_paused(self, paused):
        self.paused = paused

    def counts(self):
        return (self.connections, self.connections + 1, 4)

    def ui_app(self):
        app = FastAPI()

        @app.get("/api/connections")
        async def connections():
            return []

        return app


class FakeTray:
    def __init__(self):
        self.notices = []

    def notify(self, title, body):
        self.notices.append((title, body))

    def refresh(self):
        pass


def make_resident(tmp_path, host_role=None, agent=None):
    return Resident(applog=AppLog(tmp_path / "logs" / "app.jsonl"), agent=agent or FakeAgent(),
                    host_role=host_role or FakeHostRole(), config_path=tmp_path / "host.yaml",
                    tray_factory=lambda r: FakeTray())


def client_for(app, base="http://127.0.0.1:8766"):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 5000)), base_url=base)


@pytest.fixture
async def res(tmp_path):
    return make_resident(tmp_path)


@pytest.fixture
async def web(res):
    async with client_for(create_resident_app(res)) as c:
        yield c


class FakeProc:
    """subprocess.Popen stand-in: wait() blocks until released, then returns the exit code."""

    def __init__(self, code=0, block=False):
        self.code = code
        self.release = threading.Event()
        if not block:
            self.release.set()

    def wait(self):
        self.release.wait(5)
        return self.code


@pytest.fixture
def popen(monkeypatch):
    seen = SimpleNamespace(calls=[], proc=FakeProc())

    def fake(args, **kwargs):
        seen.calls.append((args, kwargs))
        return seen.proc

    monkeypatch.setattr(resident.subprocess, "Popen", fake)
    return seen


# ---- the borrower's side is not running --------------------------------------
async def test_overview_stub_without_host_yaml(web, tmp_path):
    r = await web.get("/admin/api/overview")
    assert r.status_code == 200
    assert r.json() == {"configured": False, "running": False, "error": None}
    (tmp_path / "host.yaml").write_text("# test only", encoding="utf-8")
    assert (await web.get("/admin/api/overview")).json()["configured"] is True


async def test_admin_api_503_without_host(web):
    for method, path in (("GET", "/admin/api/tasks"), ("GET", "/admin/api/invites"), ("POST", "/admin/api/pause"),
                         ("PUT", "/admin/api/policy"), ("GET", "/admin/api/events")):
        r = await web.request(method, path, headers=H)
        assert r.status_code == 503 and r.json() == {"detail": "使う側は動いていません"}, (method, path)
    r = await web.get("/admin/", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == PAGE


async def test_host_start_error_shown(tmp_path):
    role = FakeHostRole(error="ポート 8770 は使われています")
    res = make_resident(tmp_path, host_role=role)
    await res.start_host()  # the error is kept for the screen; nothing is raised
    assert res.host_role.running is False
    async with client_for(create_resident_app(res)) as web:
        assert (await web.get("/admin/api/overview")).json()["error"] == "ポート 8770 は使われています"
        state = (await web.get("/app/api/state")).json()
        assert state["use"]["error"] == "ポート 8770 は使われています" and state["use"]["running"] is False
        r = await web.get("/lend/api/connections")  # the lender's side keeps working
        assert r.status_code == 200 and r.json() == []


async def test_unexpected_host_failure_does_not_stop_the_app(tmp_path):
    class Boom(FakeHostRole):
        async def start(self):
            raise RuntimeError("boom")

    res = make_resident(tmp_path, host_role=Boom())
    await res.start_host()
    assert "boom" in res.use_error()
    assert "boom" in res.applog.entries(side="use")[-1]["text"]


# ---- first-time setup in its own console -------------------------------------
async def test_setup_spawns_console(web, res, popen):
    r = await web.post("/app/api/host/setup", headers=H)
    assert r.status_code == 202
    args, kwargs = popen.calls[0]
    assert args == [sys.executable, "-m", "codex_bridge", "host", "setup", "--from-app", "--config",
                    str(res.config_path)]
    assert kwargs["creationflags"] & subprocess.CREATE_NEW_CONSOLE
    assert kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert kwargs["cwd"] == str(res.config_path.parent)
    await res.setup_task


async def test_setup_spawns_frozen_command(web, res, popen, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert (await web.post("/app/api/host/setup", headers=H)).status_code == 202
    assert popen.calls[0][0] == [sys.executable, "host", "setup", "--from-app", "--config", str(res.config_path)]
    await res.setup_task


async def test_setup_409_when_running(web, res, popen):
    res.host_role.running = True
    assert (await web.post("/app/api/host/setup", headers=H)).status_code == 409
    assert popen.calls == []
    res.host_role.running = False
    popen.proc = FakeProc(block=True)
    assert (await web.post("/app/api/host/setup", headers=H)).status_code == 202
    assert (await web.post("/app/api/host/setup", headers=H)).status_code == 409  # one setup window at a time
    assert len(popen.calls) == 1
    popen.proc.release.set()
    await res.setup_task


async def test_host_starts_after_setup_exit_0(web, res, popen, tmp_path):
    make_host(tmp_path)  # the setup wrote a valid host.yaml
    await web.post("/app/api/host/setup", headers=H)
    await res.setup_task
    assert res.host_role.starts == 1 and res.host_role.running
    assert (await web.get("/app/api/state")).json()["use"]["running"] is True
    assert res.setup_running is False
    assert not res.applog.entries(min_level="warn")


async def test_host_starts_after_setup_closed_with_nonzero_exit(web, res, popen, tmp_path):
    make_host(tmp_path)  # closing the finished console with X gives a non-zero code, but host.yaml is fine
    popen.proc = FakeProc(code=-1073741510)
    await web.post("/app/api/host/setup", headers=H)
    await res.setup_task
    assert res.host_role.starts == 1 and res.host_role.running
    warns = [e["text"] for e in res.applog.entries(min_level="warn")]
    assert warns == ["初期設定が途中で終わった可能性があります。"
                     "うまく動かないときは、もう一度「準備を始める」を押してください。"]


async def test_setup_failure_shown(web, res, popen, tmp_path):
    popen.proc = FakeProc(code=1)  # no host.yaml was written
    await web.post("/app/api/host/setup", headers=H)
    await res.setup_task
    assert res.host_role.starts == 0
    error = (await web.get("/admin/api/overview")).json()["error"]
    assert "初期設定" in error
    assert res.setup_running is False  # it can be opened again
    (tmp_path / "host.yaml").write_text("public_bind: [unclosed", encoding="utf-8")  # invalid counts as failed too
    await web.post("/app/api/host/setup", headers=H)
    await res.setup_task
    assert res.host_role.starts == 0 and "初期設定" in res.use_error()
    make_host(tmp_path)
    popen.proc = FakeProc(code=0)
    assert (await web.post("/app/api/host/setup", headers=H)).status_code == 202
    await res.setup_task
    assert res.setup_error is None and res.host_role.starts == 1


async def test_setup_popen_failure_is_a_500_with_a_log_line(web, res, monkeypatch):
    def boom(args, **kwargs):
        raise OSError("access denied")

    monkeypatch.setattr(resident.subprocess, "Popen", boom)
    r = await web.post("/app/api/host/setup", headers=H)
    assert r.status_code == 500 and r.json()["detail"] == "初期設定を始められませんでした: access denied"
    assert res.applog.entries(side="use", min_level="error")[-1]["text"] == r.json()["detail"]
    assert res.setup_running is False


async def test_host_start_is_retried_until_it_runs(tmp_path):
    class Flaky(FakeHostRole):
        async def start(self):
            self.starts += 1
            if self.starts < 3:
                self.error = "Tailscale につながっていません。つながると自動で起動します。"
                raise HostStartError(self.error)
            self.error, self.running = None, True

    res = make_resident(tmp_path, host_role=Flaky())
    res.retry_interval = 0.01
    await res.start_host()
    assert res.use_error().startswith("Tailscale につながっていません")
    for _ in range(200):
        if res.host_role.running:
            break
        await asyncio.sleep(0.01)
    assert res.host_role.running and res.host_role.starts == 3 and res.use_error() is None


def test_setup_from_app_text(tmp_path, monkeypatch, capsys):
    from codex_bridge.host import setup_tools as st
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(st, "tailscale_ip", lambda: "100.64.0.9")
    monkeypatch.setattr(st, "firewall_rule_exists", lambda: True)
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "rg.exe").write_bytes(b"")
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or "n")
    run_setup(tmp_path / "host.yaml", from_app=True)
    out = capsys.readouterr().out
    assert "画面に戻ってください" in out
    assert "start.bat" not in out and "使う側" not in out and "知人" not in out
    assert "「使う」タブで「招待」を押してコードを発行し" in out and "「貸す」タブにコードを貼る" in out
    assert prompts[-1].startswith("Enter キーを押すと閉じます")  # it waits for Enter before the exit code 0
    assert (tmp_path / "host.yaml").exists()


# ---- /admin/ and the console way --------------------------------------------
async def test_admin_redirects_to_ui_when_resident(tmp_path):
    host = make_host(tmp_path)
    res = make_resident(tmp_path, host_role=FakeHostRole(host=host))
    res.host_role.running = True
    async with client_for(create_resident_app(res)) as web:
        r = await web.get("/admin/", follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == PAGE
        overview = await web.get("/admin/api/overview")  # the real admin router answers while the host runs
        assert overview.status_code == 200 and "share_root" in overview.json()
    async with client_for(create_local_app(host, page_url=PAGE), "http://127.0.0.1:8770") as local:
        r = await local.get("/admin/", follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == PAGE


async def test_console_way_serves_the_page_itself(tmp_path):
    host = make_host(tmp_path)
    async with client_for(create_local_app(host), "http://127.0.0.1:8770") as local:
        r = await local.get("/admin/")
        assert r.status_code == 200 and "/app/static/app.js" in r.text
        assert (await local.get("/app/static/index.html")).status_code == 200
        assert (await local.get("/app/api/state")).status_code == 404  # no resident: the page runs in console mode


async def test_resident_serves_page_and_static(web):
    r = await web.get("/")
    assert r.status_code == 200 and "/app/static/app.js" in r.text
    assert (await web.get("/app/static/index.html")).status_code == 200


@pytest.mark.parametrize("running", [False, True])
async def test_resident_does_not_expose_old_page_mounts(tmp_path, running):
    from codex_bridge.agent.ui import create_ui_app

    agent = FakeAgent()
    agent.ui_app = lambda: create_ui_app(None, None, None, versions={}, data_root=tmp_path)
    role = FakeHostRole(host=make_host(tmp_path))
    role.running = running
    state = make_resident(tmp_path, agent=agent, host_role=role)
    async with client_for(create_resident_app(state)) as web:
        for path in ("/admin/static/index.html", "/admin/common/ui.js", "/admin/common/app/index.html",
                     "/lend/", "/lend/static/app.js", "/lend/admin/common/ui.js"):
            assert (await web.get(path)).status_code == 404, path
        assert (await web.get("/")).status_code == 200
        assert (await web.get("/lend/api/connections")).json() == []
    async with client_for(create_local_app(role.host, page_url=PAGE), "http://127.0.0.1:8770") as local:
        assert (await local.get("/admin/")).headers["location"] == PAGE
        for path in ("/admin/static/index.html", "/admin/common/ui.js", "/app/static/app.js"):
            assert (await local.get(path)).status_code == 404, path


# ---- the page shell ------------------------------------------------------------
async def test_page_served_on_8766(web):
    r = await web.get("/")
    assert r.status_code == 200
    assert "/app/static/app.js" in r.text and "/app/static/ui.css" in r.text
    assert re.search(r"<script(?![^>]*\bsrc=)[^>]*>", r.text) is None  # the CSP forbids inline scripts
    assert re.search(r"<script[^>]*>\s*[^<\s]", r.text) is None


async def test_static_assets(web):
    for name, needle in (("ui.css", "--fs-cap2"), ("ui.js", "--sbw"), ("icons.svg", "i-send"),
                         ("app.js", "location.hash"), ("api.js", "X-Codex-Bridge"), ("log.js", "renderLog")):
        r = await web.get(f"/app/static/{name}")
        assert r.status_code == 200 and needle in r.text, name
    assert "mock page only" not in (await web.get("/app/static/ui.css")).text


async def test_admin_page_on_8770(tmp_path):
    host = make_host(tmp_path)
    async with client_for(create_local_app(host), "http://127.0.0.1:8770") as local:
        page = (await local.get("/admin/")).text
        assert page == (await local.get("/app/static/index.html")).text
        assert "/app/static/app.js" in page
        assert (await local.get("/app/static/ui.css")).status_code == 200


def test_spec_bundles_app_folder():
    spec = (Path(__file__).resolve().parents[1] / "codex-bridge.spec").read_text(encoding="utf-8")
    assert '("src/codex_bridge/common/web/app", "codex_bridge/common/web/app")' in spec


# ---- overrides ---------------------------------------------------------------
def test_overrides_from_env(monkeypatch, tmp_path):
    for name in ("CODEX_BRIDGE_UI_PORT", "CODEX_BRIDGE_HOST_CONFIG", "CODEX_BRIDGE_MUTEX"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    assert resident.UI_PORT == 8766 and resident.ui_port() == 8766
    assert resident.mutex_name() == r"Local\codex-bridge-agent"
    assert resident.host_config_path() == tmp_path / "host.yaml"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "bin" / "codex-bridge.exe"))
    assert resident.host_config_path() == (tmp_path / "bin").resolve() / "host.yaml"
    monkeypatch.setenv("CODEX_BRIDGE_UI_PORT", "18766")
    monkeypatch.setenv("CODEX_BRIDGE_HOST_CONFIG", str(tmp_path / "other.yaml"))
    monkeypatch.setenv("CODEX_BRIDGE_MUTEX", r"Local\codex-bridge-test")
    assert resident.ui_port() == 18766
    assert resident.host_config_path() == tmp_path / "other.yaml"
    assert resident.mutex_name() == r"Local\codex-bridge-test"
    monkeypatch.setenv("CODEX_BRIDGE_UI_PORT", "not a number")
    assert resident.ui_port() == 8766


# ---- pause and state ---------------------------------------------------------
async def test_pause_both_roles(tmp_path):
    host = make_host(tmp_path)
    res = make_resident(tmp_path, host_role=FakeHostRole(host=host))
    res.host_role.running = True
    async with client_for(create_resident_app(res)) as web:
        r = await web.post("/app/api/pause", json={"paused": True}, headers=H)
        assert r.status_code == 200 and r.json() == {"paused": True}
        assert host.policy.current().paused is True and res.agent.paused is True
        assert (await web.get("/app/api/state")).json()["paused"] is True
        await web.post("/app/api/pause", json={"paused": False}, headers=H)
        assert host.policy.current().paused is False and res.agent.paused is False
        assert (await web.post("/app/api/pause", json={"paused": "yes"}, headers=H)).status_code == 400


async def test_pause_without_host_still_pauses_the_lender(web, res):
    assert (await web.post("/app/api/pause", json={"paused": True}, headers=H)).status_code == 200
    assert res.agent.paused is True


async def test_state_counts(tmp_path):
    res = make_resident(tmp_path, agent=FakeAgent(connections=3))
    (tmp_path / "host.yaml").write_text("# test only", encoding="utf-8")
    async with client_for(create_resident_app(res)) as web:
        assert (await web.get("/app/api/state")).json() == {
            "use": {"configured": True, "running": False, "error": None},
            "lend": {"connections": 4}, "paused": False}
    res.host_role.running = True
    assert res.status_line() == "貸す人 1/2・依頼主 3/4・承認待ち 7"


# ---- logs --------------------------------------------------------------------
async def test_logs_filter_and_export(web, res):
    res.applog.add("use", "info", "使う側を始めました")
    res.applog.add("lend", "warn", "host: 接続が切れました")
    res.applog.add("lend", "error", "失敗した\nつづき")
    assert len((await web.get("/app/api/logs")).json()) == 3
    assert [e["text"] for e in (await web.get("/app/api/logs", params={"side": "lend"})).json()] == [
        "host: 接続が切れました", "失敗した\nつづき"]
    assert len((await web.get("/app/api/logs", params={"level": "warn"})).json()) == 2
    assert [e["text"] for e in (await web.get("/app/api/logs", params={"q": "始め"})).json()] == ["使う側を始めました"]
    assert len((await web.get("/app/api/logs", params={"limit": 1})).json()) == 1
    r = await web.get("/app/api/logs/export")
    assert r.headers["content-type"].startswith("text/plain")
    assert 'filename="codex-bridge-log.txt"' in r.headers["content-disposition"]
    lines = r.text.splitlines()
    assert len(lines) == 3
    parts = [line.split("\t") for line in lines]
    assert [p[1:] for p in parts] == [["使う", "情報", "使う側を始めました"], ["貸す", "警告", "host: 接続が切れました"],
                                      ["貸す", "失敗", "失敗した つづき"]]
    assert all(len(p[0]) == len("MM/DD HH:MM:SS") and p[0][2] == "/" for p in parts)


async def test_open_folder_calls_startfile(web, res, monkeypatch):
    opened = []
    monkeypatch.setattr(resident.os, "startfile", opened.append, raising=False)
    assert (await web.post("/app/api/logs/open-folder", headers=H)).status_code == 200
    assert opened == [res.applog.path.parent]


# ---- guards ------------------------------------------------------------------
async def test_guards(web, res):
    foreign = await web.get("/app/api/state", headers={"Host": "evil.example:8766"})
    assert foreign.status_code == 403
    assert (await web.get("/lend/api/connections", headers={"Host": "evil.example"})).status_code == 403
    assert (await web.post("/app/api/pause", json={"paused": True})).status_code == 403  # no X-Codex-Bridge
    assert res.agent.paused is False
    assert (await web.post("/admin/api/pause", json={"paused": True})).status_code == 403
    for path in ("/app/api/state", "/", "/lend/api/connections", "/admin/api/overview"):
        r = await web.get(path)
        assert r.headers["content-security-policy"] == (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'"), path
    assert "X-Codex-Bridge" in (await web.post("/app/api/pause", json={"paused": True})).text
    stranger = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_resident_app(res),
                                                               client=("192.0.2.1", 5000)),
                                 base_url="http://127.0.0.1:8766")
    async with stranger:
        assert (await stranger.get("/app/api/state")).status_code == 403


# ---- the lender with no connection yet ---------------------------------------
async def test_zero_connections_then_add(tmp_path, monkeypatch):
    from fake_helpers import Fake
    resolved, threads = [], []
    real_to_thread = asyncio.to_thread

    async def recording_to_thread(func, *args, **kwargs):
        threads.append(func)
        return await real_to_thread(func, *args, **kwargs)

    def resolve(home):
        resolved.append(threading.get_ident())
        return tmp_path / "codex.exe"

    monkeypatch.setattr(asyncio, "to_thread", recording_to_thread)
    monkeypatch.setattr(agent_main, "resolve_codex", resolve)
    monkeypatch.setattr(agent_main, "codex_version", lambda cmd: "fake")
    monkeypatch.setattr(agent_main, "user_mcp_names", lambda: [])
    made = []

    def fake_app(cmd, url, token, workdir, **kwargs):
        root = tmp_path / f"fake-{len(made)}"
        root.mkdir()
        made.append(cmd)
        return Fake(root).app

    monkeypatch.setattr(agent_main, "AppServer", fake_app)
    role = agent_main.AgentRole(tmp_path / "home", AppLog(tmp_path / "app.jsonl"), lambda title, body: None)
    role.runtime._ensure_usage_probe = lambda: None  # no Codex process in this test
    await role.start()
    assert resolved == []  # nothing to resolve with zero connections; no runner started
    assert role.runtime.rows() == []
    res = make_resident(tmp_path, agent=role)
    try:
        async with client_for(create_resident_app(res)) as web:
            assert (await web.get("/lend/api/connections")).json() == []
            code = make_invite("http://host.invalid", "tok", "友")
            r = await web.post("/lend/api/connections", json={"code": code}, headers=H)
            assert r.status_code == 201
            assert agent_main.resolve_codex in threads  # resolved in a worker thread
            assert resolved and resolved[0] != threading.get_ident()
            assert made and made[0] == agent_main.codex_command(tmp_path / "codex.exe")
            assert len((await web.get("/lend/api/connections")).json()) == 1
            r = await web.post("/lend/api/connections", json={"code": make_invite("http://b.invalid", "tok", "他")},
                               headers=H)
            assert r.status_code == 201 and len(resolved) == 1  # resolved once
            bad = await web.post("/lend/api/connections", json={"code": "nonsense"}, headers=H)
            assert bad.status_code == 400 and len(resolved) == 1
    finally:
        await role.stop()


async def test_declined_codex_download_is_a_message_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_main, "resolve_codex", lambda home: None)
    role = agent_main.AgentRole(tmp_path / "home", AppLog(tmp_path / "app.jsonl"), lambda title, body: None)
    await role.start()
    res = make_resident(tmp_path, agent=role)
    try:
        async with client_for(create_resident_app(res)) as web:
            r = await web.post("/lend/api/connections", headers=H,
                               json={"code": make_invite("http://host.invalid", "tok", "友")})
            assert r.status_code == 409 and "Codex" in r.json()["detail"]
            assert (await web.get("/lend/api/connections")).json() == []
    finally:
        await role.stop()


# ---- tray, notifications, servers --------------------------------------------
def test_tray_notification_and_os_failure_do_not_interrupt_task():
    sent = []
    tray = object.__new__(resident.Tray)
    tray.icon = SimpleNamespace(notify=lambda body, title: sent.append((body, title)))
    tray.notify("codex-bridge: 事前承認待ち", "承認してください")
    assert sent == [("承認してください", "codex-bridge: 事前承認待ち")]

    def fail(*args):
        raise OSError("notifications disabled")
    tray.icon.notify = fail
    tray.notify("t", "test")  # Windows notification policy cannot fail the request


async def test_new_host_approval_shows_a_balloon_once(tmp_path):
    host = make_host(tmp_path)
    res = make_resident(tmp_path, host_role=FakeHostRole(host=host))
    res.tray = FakeTray()
    await res.start_host()
    try:
        approval = host.approvals.request("exec", "python scripts/a.py", None)
        host.approvals.on_change[-1]()  # a second change while it is still pending must not repeat the balloon
        assert res.tray.notices == [("codex-bridge: 承認待ち", "python scripts/a.py")]
        assert approval.id in res._seen_approvals
    finally:
        for task in res._background:
            task.cancel()


async def test_host_shutdown_request_quits_the_app(tmp_path):
    host = make_host(tmp_path)
    res = make_resident(tmp_path, host_role=FakeHostRole(host=host))
    res.loop, res.stop_event = asyncio.get_running_loop(), asyncio.Event()
    await res.start_host()
    host.shutdown.set()  # what /admin/api/shutdown does
    await asyncio.wait_for(res.stop_event.wait(), 2)
    for task in res._background:
        task.cancel()


def test_ui_server_reports_a_taken_port():
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    try:
        with pytest.raises(RuntimeError, match="使われています"):
            UiServer(FastAPI(), blocker.getsockname()[1])
    finally:
        blocker.close()


async def test_run_starts_everything_and_stops_on_quit(tmp_path):
    started, served = [], []

    class Server:
        should_exit = False

        async def serve(self):
            served.append(True)
            while not self.should_exit:
                await asyncio.sleep(0.01)

    tray = FakeTray()
    tray.run_detached = lambda: started.append("tray")
    tray.stop = lambda: started.append("tray stopped")
    (tmp_path / "host.yaml").write_text("# test only", encoding="utf-8")
    res = Resident(applog=AppLog(tmp_path / "app.jsonl"), agent=FakeAgentRun(), host_role=FakeHostRole(),
                   config_path=tmp_path / "host.yaml", tray_factory=lambda r: tray,
                   server_factory=lambda app, port: Server())

    async def quit_soon():
        while not res.host_role.running:
            await asyncio.sleep(0.01)
        res.request_quit()

    asyncio.get_running_loop().create_task(quit_soon())
    assert await asyncio.wait_for(res._run(), 5) is False
    assert served and res.host_role.starts == 1 and res.host_role.stops == 1
    assert started == ["tray", "tray stopped"] and res.agent.log == ["start", "stop"]


class FakeAgentRun(FakeAgent):
    moved = False

    def __init__(self):
        super().__init__()
        self.log = []

    async def start(self):
        self.log.append("start")

    async def stop(self):
        self.log.append("stop")


async def test_data_move_asks_for_a_relaunch(tmp_path):
    class Server:
        should_exit = False

        async def serve(self):
            while not self.should_exit:
                await asyncio.sleep(0.01)

    agent = FakeAgentRun()
    tray = FakeTray()
    tray.run_detached = lambda: None
    tray.stop = lambda: None
    res = Resident(applog=AppLog(tmp_path / "app.jsonl"), agent=agent, host_role=FakeHostRole(),
                   config_path=tmp_path / "host.yaml", tray_factory=lambda r: tray,
                   server_factory=lambda app, port: Server())

    async def moved():
        while agent.on_moved is None:
            await asyncio.sleep(0.01)
        agent.moved = True
        agent.on_moved()

    asyncio.get_running_loop().create_task(moved())
    assert await asyncio.wait_for(res._run(), 5) is True


# ---- the borrower's role object ----------------------------------------------
def role_config(tmp_path, **overrides):
    return make_host(tmp_path, public_bind="127.0.0.1:0", local_port=0, **overrides).config_path


async def test_host_role_starts_and_stops(tmp_path):
    applog = AppLog(tmp_path / "app.jsonl")
    role = HostRole(role_config(tmp_path), applog, tailscale_ip=None)
    await role.start()
    try:
        assert role.running and role.error is None and role.host is not None
        role.host.mgr.log("warn", "テスト")  # Task 3 hooks: the host writes to the shared log
        role.host.approvals.log("info", "承認")
        assert [(e["side"], e["level"], e["text"]) for e in applog.entries()][-2:] == [
            ("use", "warn", "テスト"), ("use", "info", "承認")]
    finally:
        await role.stop()
    assert role.running is False and role.host is None


async def test_host_role_reports_a_bad_config(tmp_path):
    bad = tmp_path / "host.yaml"
    bad.write_text("public_bind: [unclosed", encoding="utf-8")
    role = HostRole(bad, AppLog(tmp_path / "app.jsonl"), tailscale_ip=None)
    with pytest.raises(HostStartError, match="host.yaml"):
        await role.start()
    assert role.running is False and "host.yaml" in role.error
    assert role.applog.entries(side="use", min_level="error")


async def test_host_role_reports_a_taken_port(tmp_path):
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    port = blocker.getsockname()[1]
    try:
        config = make_host(tmp_path, public_bind="127.0.0.1:0", local_port=port).config_path
        role = HostRole(config, AppLog(tmp_path / "app.jsonl"), tailscale_ip=None)
        with pytest.raises(HostStartError) as error:
            await role.start()
        assert f"ポート {port} は使われています" == str(error.value) and role.error == str(error.value)
        assert role.running is False and role.host is None
    finally:
        blocker.close()


def test_bind_socket_is_exclusive():
    first = bind_socket("127.0.0.1", 0)
    try:
        with pytest.raises(OSError):
            bind_socket("127.0.0.1", first.getsockname()[1])
    finally:
        first.close()


async def test_host_role_waits_for_tailscale_then_starts(tmp_path):
    config = role_config(tmp_path)
    ips = [None, "127.0.0.1"]  # not signed in yet, then up
    role = HostRole(config, AppLog(tmp_path / "app.jsonl"), tailscale_ip=lambda: ips.pop(0))
    with pytest.raises(HostStartError) as error:
        await role.start()
    assert str(error.value) == "Tailscale につながっていません。つながると自動で起動します。"
    assert role.error == str(error.value) and role.running is False
    try:
        await role.start()
        assert role.running and role.error is None
    finally:
        await role.stop()


async def test_host_role_updates_public_bind_to_the_current_tailscale_ip(tmp_path):
    config = role_config(tmp_path)
    applog = AppLog(tmp_path / "app.jsonl")
    role = HostRole(config, applog, tailscale_ip=lambda: "127.0.0.1")
    config.write_text(config.read_text(encoding="utf-8").replace("127.0.0.1:0", "100.64.0.1:0"), encoding="utf-8")
    assert load_host_config(config).public_bind == "100.64.0.1:0"
    try:
        await role.start()
        assert load_host_config(config).public_bind == "127.0.0.1:0"
        assert role.running
        assert applog.entries(min_level="warn")[-1]["text"] == (
            "Tailscale の IP が変わったので設定を更新しました。招待コードは発行し直してください。")
    finally:
        await role.stop()
    again = HostRole(config, applog, tailscale_ip=lambda: "127.0.0.1")
    before = len(applog.entries(min_level="warn"))
    try:
        await again.start()
        assert len(applog.entries(min_level="warn")) == before  # unchanged IP: no line
    finally:
        await again.stop()


async def test_one_server_dying_stops_the_other_and_a_retry_works(tmp_path):
    role = HostRole(role_config(tmp_path), AppLog(tmp_path / "app.jsonl"), tailscale_ip=None)
    await role.start()
    public, local = role._servers
    role._tasks[0].cancel()  # the public listener dies
    for _ in range(200):
        if local.should_exit and role._tasks[1].done():
            break
        await asyncio.sleep(0.01)
    assert local.should_exit and role._tasks[1].done()  # its port is released too
    assert role.running is False and "止まりました" in role.error
    await role.start()  # the retry is not blocked by our own leftover listener
    try:
        assert role.running and role.error is None
    finally:
        await role.stop()
