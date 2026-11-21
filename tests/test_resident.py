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

from codex_bridge import entry, resident, updater
from codex_bridge.agent import main as agent_main
from codex_bridge.agent.runner import TaskSlot
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
        self.error = None
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
    for word in ("start.bat", "使う側", "知人", "管理画面"):
        assert word not in out and not any(word in p for p in prompts)
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
            "lend": {"connections": 4, "error": None}, "paused": False, "update": res.update_status()}
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
            assert (await web.get("/app/api/state")).json()["lend"]["error"] == r.json()["detail"]
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


@pytest.mark.parametrize("overlap", ["start", "stop"])
async def test_host_lifecycle_calls_are_serialized(tmp_path, monkeypatch, overlap):
    role = HostRole(role_config(tmp_path), AppLog(tmp_path / "app.jsonl"), tailscale_ip=None)
    entered, release = asyncio.Event(), asyncio.Event()
    refreshes = []

    async def refresh():
        refreshes.append(True)
        entered.set()
        await release.wait()

    monkeypatch.setattr(role, "_refresh_public_bind", refresh)
    first = asyncio.create_task(role.start())
    await asyncio.wait_for(entered.wait(), 2)
    second = asyncio.create_task(getattr(role, overlap)())
    await asyncio.sleep(0)
    release.set()
    try:
        await asyncio.wait_for(asyncio.gather(first, second), 5)
        assert len(refreshes) == 1
        if overlap == "start":
            assert role.running and role.host is not None and role.error is None
            assert len(role._tasks) == 2 and all(not t.done() for t in role._tasks)
        else:
            assert not role.running and role.host is None and not role._tasks
    finally:
        release.set()
        await asyncio.gather(first, second, return_exceptions=True)
        await role.stop()


async def test_resident_retries_a_host_whose_server_ends(tmp_path):
    role = HostRole(role_config(tmp_path), AppLog(tmp_path / "app.jsonl"), tailscale_ip=None)
    res = make_resident(tmp_path, host_role=role)
    res.retry_interval = 0.01
    try:
        await res.start_host()
        original = role.host
        role._tasks[0].cancel()

        async def restarted():
            while not (role.running and role.host is not None and role.host is not original):
                await asyncio.sleep(0.01)

        await asyncio.wait_for(restarted(), 5)
        assert role.error is None
        assert any("使う側が止まりました" in e["text"] for e in role.applog.entries(min_level="error"))
    finally:
        for task in res._background:
            task.cancel()
        await asyncio.gather(*res._background, return_exceptions=True)
        await role.stop()


async def test_shutdown_after_log_folder_is_removed_stops_both_roles(tmp_path):
    applog = AppLog(tmp_path / "logs" / "app.jsonl")
    role = HostRole(role_config(tmp_path), applog, tailscale_ip=None)
    agent = FakeAgentRun()
    res = Resident(applog=applog, agent=agent, host_role=role, config_path=role.config_path)
    await role.start()
    applog.path.unlink()
    applog.path.parent.rmdir()
    serving = asyncio.create_task(asyncio.sleep(0))
    res._ui_server = SimpleNamespace(should_exit=False)
    res._ui_serving = serving
    await res._shutdown()
    assert not role.running and role.host is None
    assert agent.log == ["stop"]
    assert not applog.path.parent.exists()


@pytest.mark.parametrize("saved_pause", [False, True])
@pytest.mark.parametrize("host_fails", [False, True])
async def test_resident_applies_saved_host_pause_before_lending_starts(tmp_path, saved_pause, host_fails):
    host = make_host(tmp_path)
    host.policy.update(paused=saved_pause)
    observed = []

    class Agent(FakeAgentRun):
        async def start(self):
            observed.append(self.paused)
            res.stop_event.set()

    class Server:
        should_exit = False

        async def serve(self):
            while not self.should_exit:
                await asyncio.sleep(0.01)

    agent = Agent()
    agent.paused = not saved_pause
    tray = FakeTray()
    tray.run_detached = tray.stop = lambda: None
    role = FakeHostRole(host=host, error="ポートは使用中です" if host_fails else None)
    res = Resident(applog=AppLog(tmp_path / "app.jsonl"), agent=agent, host_role=role,
                   config_path=host.config_path, tray_factory=lambda r: tray,
                   server_factory=lambda app, port: Server())
    await asyncio.wait_for(res._run(), 5)
    assert observed == [saved_pause] and agent.paused is saved_pause


async def test_starting_host_after_lender_pause_pauses_both(tmp_path):
    res = make_resident(tmp_path)
    await res.set_paused(True)
    host = make_host(tmp_path)  # setup finishes after the lender was paused
    res.host_role = FakeHostRole(host=host)
    try:
        await res.start_host()
        assert res.agent.paused and host.policy.current().paused and res.paused
    finally:
        for task in res._background:
            task.cancel()
        await asyncio.gather(*res._background, return_exceptions=True)


async def test_resume_while_host_is_down_updates_its_saved_pause(tmp_path):
    host = make_host(tmp_path)
    host.policy.update(paused=True)
    res = make_resident(tmp_path)  # no Host object until a retry succeeds
    async with client_for(create_resident_app(res)) as web:
        assert (await web.get("/app/api/state")).json()["paused"] is True
        assert (await web.post("/app/api/pause", json={"paused": False}, headers=H)).json() == {"paused": False}
    assert not res.agent.paused and not host.policy.current().paused


@pytest.mark.parametrize("host_paused,agent_paused", [(True, False), (False, True)])
async def test_pause_state_and_tray_use_either_side_and_resume_both(tmp_path, host_paused, agent_paused):
    host = make_host(tmp_path)
    host.policy.update(paused=host_paused)
    role = FakeHostRole(host=host)
    role.running = True
    res = make_resident(tmp_path, host_role=role)
    res.agent.paused = agent_paused
    menu = []

    class Icon:
        def __init__(self, *args, **kwargs):
            menu.extend(kwargs["menu"])

    # Capture the real tray menu label callback without creating a native icon.
    from unittest.mock import patch
    with patch.dict(sys.modules, {"pystray": SimpleNamespace(
            Icon=Icon, Menu=lambda *items: items,
            MenuItem=lambda text, action, **kwargs: (text, action))}):
        res._make_tray(res)
    assert menu[2][0](None) == "再開"
    async with client_for(create_resident_app(res)) as web:
        assert (await web.get("/app/api/state")).json()["paused"] is True
        assert (await web.post("/app/api/pause", json={"paused": False}, headers=H)).json() == {"paused": False}
        assert (await web.get("/app/api/state")).json()["paused"] is False
    assert not res.agent.paused and not host.policy.current().paused
    assert menu[2][0](None) == "一時停止"


async def test_state_exposes_lender_setup_error(web, res):
    res.agent.error = "Codex をダウンロードできませんでした: オフラインです"
    assert (await web.get("/app/api/state")).json()["lend"]["error"] == res.agent.error


async def test_source_app_checks_and_notifies_without_preparing(tmp_path, res, web, monkeypatch):
    calls = []
    release = updater.Release("9.0.0", "fake", "0" * 64)
    monkeypatch.setattr(updater, "check_release", lambda: release)
    monkeypatch.setattr(updater, "prepare_release", lambda *args: calls.append(args))
    res.update_exe = None
    res.tray = FakeTray()
    assert res.update_mode == "confirm"
    assert (await web.post("/app/api/update/check")).status_code == 403
    assert (await web.post("/app/api/update/mode", headers=H, json={"mode": "auto"})).status_code == 200
    assert make_resident(tmp_path).update_mode == "auto"
    response = await web.post("/app/api/update/check", headers=H)
    assert response.status_code == 202 and response.json()["phase"] == "checking"
    await res._update_job
    assert res.update_status()["phase"] == "available"
    assert res.update_status()["latest"] == "9.0.0"
    assert len(res.tray.notices) == 1
    assert (await web.post("/app/api/update/install", headers=H)).status_code == 409
    assert not calls


@pytest.mark.parametrize("automatic", [True, False])
async def test_switch_waits_for_work_on_both_sides_and_keeps_approvals(tmp_path, monkeypatch, automatic):
    host = make_host(tmp_path)
    role = FakeHostRole(host)
    role.running = True
    agent = FakeAgent()
    slot = TaskSlot()
    busy = SimpleNamespace(active_task_ids=lambda: ["lend work"])
    slot.runners.append(busy)

    async def announce():
        pass

    slot.announce = announce
    agent.runtime = SimpleNamespace(slot=slot, moving=False)
    app = make_resident(tmp_path, host_role=role, agent=agent)
    app.loop = asyncio.get_running_loop()
    app.stop_event = asyncio.Event()
    host.mgr.update_waiting = role.update_waiting
    app.update_exe = tmp_path / updater.EXE
    app.update_exe.write_bytes(b"old exe")
    app.update_mode = "auto" if automatic else "confirm"
    newest = updater.Release("9.0.0", "fake", "0" * 64)
    package = tmp_path / "data" / "updates" / "download" / newest.folder
    installs = []
    monkeypatch.setattr(updater, "check_release", lambda: newest)
    monkeypatch.setattr(updater, "prepare_release", lambda *args: package)
    monkeypatch.setattr(updater, "install_release", lambda *args: installs.append(args))
    host.mgr.check_start = None

    async def send(message):
        pass

    host.tokens.issue("alice")
    link = host.mgr.attach("alice", send)
    await host.mgr.on_agent_message(link, {"type": "hello", "protocol": 4})
    working = await host.mgr.start("still working", lender="alice")
    approval = host.approvals.request("command", "echo approved", working)
    async with client_for(create_resident_app(app)) as http:
        await http.post("/app/api/update/check", headers=H)
        if not automatic:
            await app._update_job
            assert app.update_phase == "available" and not installs
            assert (await http.post("/app/api/update/install", headers=H)).status_code == 202
        async with asyncio.timeout(3):
            while not slot.update_waiting:
                await asyncio.sleep(0.01)
        assert app.update_waiting
        idle_task = await host.mgr.start("after restart", lender="alice")
        assert host.mgr.status(idle_task)["state"] == "update_waiting"
        assert (await http.post(f"/admin/api/approvals/{approval.id}", headers=H, json={"approve": True})).status_code == 200
        slot.runners.remove(busy)
        await asyncio.sleep(0.3)
        assert not installs
        await host.mgr.on_agent_message(link, {"type": "task_state", "task_id": working, "state": "completed"})
        await asyncio.wait_for(app._update_job, 3)
        await asyncio.wait_for(app.stop_event.wait(), 3)
    assert installs == [(app.update_exe, package)]


@pytest.mark.parametrize("cancel", [True, False])
async def test_end_switch_wait_releases_held_task(tmp_path, monkeypatch, cancel):
    host = make_host(tmp_path)
    role = FakeHostRole(host)
    role.running = True
    app = make_resident(tmp_path, host_role=role)
    host.mgr.check_start = None
    host.mgr.update_waiting = role.update_waiting
    host.tokens.issue("alice")
    messages = []

    async def send(message):
        messages.append(message)

    link = host.mgr.attach("alice", send)
    await host.mgr.on_agent_message(link, {"type": "hello", "protocol": 5})
    app.update_exe = tmp_path / updater.EXE
    app.update_exe.write_bytes(b"old exe")
    newest = updater.Release("9.0.0", "fake", "0" * 64)
    app.update_mode = "auto"
    staged = tmp_path / "data" / "updates" / "download" / newest.folder
    prepares = []
    installs = []
    busy = [True]

    def prepare(*args):
        prepares.append(args)
        staged.mkdir(parents=True)
        return staged

    def fail(*args):
        installs.append(args)
        raise updater.UpdateError("入れ替えの失敗")

    monkeypatch.setattr(updater, "check_release", lambda: newest)
    monkeypatch.setattr(updater, "prepare_release", prepare)
    monkeypatch.setattr(updater, "install_release", fail)
    monkeypatch.setattr(app, "_update_busy", lambda: busy[0])
    app.check_update()
    async with asyncio.timeout(3):
        while not app.update_waiting:
            await asyncio.sleep(0.01)
    held = await host.mgr.start("held", lender="alice")
    async with client_for(create_resident_app(app)) as web:
        if cancel:
            assert (await web.post("/app/api/update/cancel")).status_code == 403
            response = await web.post("/app/api/update/cancel", headers=H)
            assert response.json()["phase"] == "cancelled"
        else:
            busy[0] = False
            await asyncio.wait_for(app._update_job, 3)
            assert app.update_phase == "failed"
            assert app.update_message == "入れ替えの失敗"
        assert installs == ([] if cancel else [(app.update_exe, staged)])
        assert not staged.parent.exists()
        assert [m["task_id"] for m in messages if m["type"] == "task_start"] == [held]
        if cancel:
            assert make_resident(tmp_path)._cancelled_version == newest.version
            app.check_update()
            await app._update_job
            assert len(prepares) == 1
            await web.post("/app/api/update/check", headers=H)
            async with asyncio.timeout(3):
                while not app.update_waiting:
                    await asyncio.sleep(0.01)
            assert len(prepares) == 2
            assert (await web.post("/app/api/update/cancel", headers=H)).status_code == 200


async def test_preflight_failure_does_not_enter_switch_wait(tmp_path, res, monkeypatch):
    res.update_exe = tmp_path / updater.EXE
    res.update_release = updater.Release("9.0.0", "fake", "0" * 64)
    res.update_phase = "available"

    def deny(exe):
        raise updater.UpdateError(f"exe のフォルダに書き込めません: {exe.parent}")

    monkeypatch.setattr(updater, "check_install_folder", deny)
    monkeypatch.setattr(updater, "prepare_release", lambda *args: pytest.fail("download after failed check"))
    assert res.prepare_update()
    await res._update_job
    assert res.update_phase == "failed" and "exe のフォルダ" in res.update_message


async def test_check_failure_keeps_installable_release(tmp_path, res, web, monkeypatch):
    res.update_exe = tmp_path / updater.EXE
    newest = updater.Release("9.0.0", "fake", "0" * 64)
    res.update_release = newest

    def offline():
        raise ConnectionError("offline")

    monkeypatch.setattr(updater, "check_release", offline)
    await web.post("/app/api/update/check", headers=H)
    await res._update_job
    update = (await web.get("/app/api/state")).json()["update"]
    assert update["latest"] == newest.version
    assert update["phase"] == "failed" and update["can_install"]
    assert res.update_release is newest


def runnable_update_app(tmp_path, host_role=None):
    with bind_socket("127.0.0.1", 0) as sock:
        port = sock.getsockname()[1]
    tray = FakeTray()
    tray.run_detached = tray.stop = lambda: None
    app = Resident(applog=AppLog(tmp_path / "logs" / "app.jsonl"), agent=FakeAgentRun(),
                   host_role=host_role or FakeHostRole(), config_path=tmp_path / "host.yaml", port=port,
                   tray_factory=lambda r: tray)
    app.update_exe = tmp_path / updater.EXE
    app.update_exe.write_bytes(b"old exe")
    return app


async def test_new_resident_signals_only_after_listening_then_waits_to_clean_up(tmp_path, monkeypatch):
    app = runnable_update_app(tmp_path)
    notices = []
    parent_gone = asyncio.Event()
    acknowledged = asyncio.Event()
    monkeypatch.setattr(updater, "clean_up", lambda *args: notices.append("cleanup"))
    binds = []

    def server_factory(asgi, port):
        binds.append(port)
        if len(binds) == 1:
            raise RuntimeError("old socket is still closing")
        return UiServer(asgi, port)

    app.server_factory = server_factory

    async def ready():
        assert binds == [app.port, app.port]
        async with httpx.AsyncClient(trust_env=False) as web:
            assert (await web.get(app.page_url)).status_code == 200
            assert (await web.post(app.page_url + "app/api/update/check", headers=H)).status_code == 503
        acknowledged.set()
        await parent_gone.wait()

    run = asyncio.create_task(app._run(update_ready=ready))
    try:
        await asyncio.wait_for(acknowledged.wait(), 5)
        assert not notices and not app.agent.log
        parent_gone.set()
        async with asyncio.timeout(3):
            while not app.agent.log:
                await asyncio.sleep(0.01)
        assert notices == ["cleanup"]
    finally:
        parent_gone.set()
        app.request_quit()
        await asyncio.wait_for(run, 5)


@pytest.mark.parametrize("child_fails", [False, True])
async def test_old_resident_waits_for_handover_and_recovers_if_child_fails(tmp_path, monkeypatch, child_fails):
    host = make_host(tmp_path)
    app = runnable_update_app(tmp_path, FakeHostRole(host))
    host.mgr.update_waiting = app.host_role.update_waiting
    host.mgr.check_start = None
    host.tokens.issue("alice")
    messages = []

    async def send(message):
        messages.append(message)

    link = host.mgr.attach("alice", send)
    await host.mgr.on_agent_message(link, {"type": "hello"})
    package = tmp_path / "data" / "updates" / "download" / "pkg"
    package.mkdir(parents=True)
    (package / updater.EXE).write_bytes(b"new exe")
    launch_started, child_finished = threading.Event(), threading.Event()
    events = []
    monkeypatch.setattr(entry, "release_agent_instance", lambda: events.append("released"))
    monkeypatch.setattr(entry, "claim_agent_instance", lambda: events.append("reclaimed") or True)
    monkeypatch.setattr(updater, "clean_up", lambda *args: None)
    monkeypatch.setattr(updater, "prepare_release", lambda *args: package)

    def launch(exe, staging):
        assert events == ["released"]
        assert exe.with_suffix(".exe.old").read_bytes() == b"old exe"
        launch_started.set()
        assert child_finished.wait(6)
        if child_fails:
            raise updater.UpdateError("子プロセスが起動できませんでした")

    monkeypatch.setattr(updater, "_launch", launch)
    run = asyncio.create_task(app._run())
    try:
        async with asyncio.timeout(5):
            while not app.agent.log:
                await asyncio.sleep(0.01)
        app.update_release = updater.Release("9.0.0", "fake", "0" * 64)
        app.update_phase = "available"
        assert app.prepare_update()
        assert await asyncio.to_thread(launch_started.wait, 5)
        assert not app.stop_event.is_set() and app._ui_serving is None
        assert app.host_role.running
        held = await host.mgr.start("queued during handover", lender="alice")
        assert host.mgr.status(held)["state"] == "update_waiting"
        child_finished.set()
        await asyncio.wait_for(app._update_job, 5)
        if child_fails:
            assert not app.stop_event.is_set()
            assert app.update_phase == "failed"
            assert events == ["released", "reclaimed"]
            assert [m["task_id"] for m in messages if m["type"] == "task_start"] == [held]
            assert app.update_exe.read_bytes() == b"old exe"
            async with httpx.AsyncClient(trust_env=False) as web:
                async with asyncio.timeout(5):
                    while not app._ui_server.server.started:
                        await asyncio.sleep(0.01)
                assert (await web.get(app.page_url + "app/api/state")).json()["update"]["phase"] == "failed"
        else:
            assert app.update_exe.read_bytes() == b"new exe"
            await asyncio.wait_for(run, 5)
    finally:
        child_finished.set()
        app.request_quit()
        await asyncio.wait_for(run, 5)


async def test_backup_cleanup_retries_after_start_and_at_release_check(tmp_path, res, monkeypatch):
    res.update_exe = tmp_path / updater.EXE
    events = []
    monkeypatch.setattr(resident, "BACKUP_CLEANUP_DELAY", 0)
    monkeypatch.setattr(updater, "CHECK_INTERVAL", 0)
    monkeypatch.setattr(updater, "clean_old_exes", lambda exe: events.append(("cleanup", exe)))

    await res._retry_old_exes_after_start()
    assert events == [("cleanup", res.update_exe)]

    def check():
        if events[-1] == ("cleanup", res.update_exe) and len(events) > 1:
            raise asyncio.CancelledError
        events.append("check")

    monkeypatch.setattr(res, "check_update", check)
    with pytest.raises(asyncio.CancelledError):
        await res._periodic_update()
    assert events == [("cleanup", res.update_exe), "check", ("cleanup", res.update_exe)]
    res._installing = asyncio.create_task(asyncio.sleep(0))
    await res._installing
    res._clean_old_exes()
    assert len(events) == 3


@pytest.mark.parametrize("failure", ["notify", "wait", "resident"])
def test_handover_crash_reporting_begins_after_readiness(tmp_path, monkeypatch, failure):
    from codex_bridge.agent import setup

    notices = []
    monkeypatch.setattr(setup, "data_root", lambda: tmp_path)
    monkeypatch.setattr(agent_main, "ensure_std_streams", lambda: None)
    monkeypatch.setattr(agent_main, "show_info", lambda text, **kw: notices.append((text, kw)))
    monkeypatch.setattr(agent_main, "AgentRole", lambda *args, **kw: FakeAgent())
    monkeypatch.setattr("codex_bridge.host.role.HostRole", lambda *args, **kw: FakeHostRole())
    monkeypatch.setattr(resident, "host_config_path", lambda: tmp_path / "host.yaml")

    async def notify_ready():
        if failure == "notify":
            raise RuntimeError("before readiness")

    def wait_for_previous():
        if failure == "wait":
            raise RuntimeError("after readiness, waiting for old process")

    def run(self, *, check_updates, update_ready):
        if update_ready is not None:
            asyncio.run(update_ready())
        raise RuntimeError("resident crash")

    monkeypatch.setattr(Resident, "run", run)
    log = tmp_path / "logs" / "crash.log"
    if failure != "notify":
        assert resident.main(update_ready=notify_ready, wait_for_previous=wait_for_previous) == 0
        first = log.read_text(encoding="utf-8")
        assert re.match(r"--- \d{4}-\d\d-\d\d \d\d:\d\d:\d\d \(agent [^)]+\)", first)
        reason = "after readiness, waiting for old process" if failure == "wait" else "resident crash"
        assert f"RuntimeError: {reason}" in first
        assert len(notices) == 1 and notices[0][1] == {"error": True}
        assert str(log) in notices[0][0]
        assert resident.main() == 0
        appended = log.read_text(encoding="utf-8")
        assert appended.startswith(first)
        assert appended[len(first):].count("RuntimeError: resident crash") == 1
    else:
        with pytest.raises(RuntimeError, match="before readiness"):
            resident.main(update_ready=notify_ready, wait_for_previous=wait_for_previous)
        assert not log.exists() and not notices


@pytest.mark.parametrize("port_free", [True, False])
async def test_mutex_recovery_is_bounded_keeps_app_alive_and_reports_failure(res, monkeypatch, port_free):
    res.stop_event = asyncio.Event()
    res._handover = True
    res.update_phase = "failed"
    res.update_message = "新しい exe の起動を確認できませんでした"
    res.tray = FakeTray()
    res.update_exe = res.config_path.with_name(updater.EXE)
    monkeypatch.setattr(entry, "claim_agent_instance", lambda: False)
    monkeypatch.setattr(resident, "HANDOVER_RECOVERY_TIMEOUT", 0)
    attempts = []

    def start_ui():
        attempts.append("screen")
        if not port_free:
            raise RuntimeError("port taken")

    monkeypatch.setattr(res, "_start_ui", start_ui)
    await asyncio.wait_for(res._restore_update_handover(), 1)

    status = res.update_status()
    assert attempts == ["screen"]
    assert not res.stop_event.is_set() and not res._handover
    assert not status["can_install"] and status["phase"] == "failed"
    assert "ミューテックスを取り直せませんでした" in status["message"]
    assert ("画面の待ち受けを再開できませんでした" in status["message"]) is not port_free
    assert "ミューテックス" in res.applog.entries()[0]["text"]
    assert "ミューテックス" in res.tray.notices[0][1]


@pytest.mark.parametrize("configured,running", [(True, False), (True, True), (False, False)])
async def test_handover_recovery_restarts_only_configured_stopped_host(res, monkeypatch, configured, running):
    res.stop_event = asyncio.Event()
    res._handover = True
    res.host_role.running = running
    if configured:
        res.config_path.touch()
    monkeypatch.setattr(entry, "claim_agent_instance", lambda: True)
    monkeypatch.setattr(res, "_start_ui", lambda: None)
    attempts = []

    async def start_host():
        assert not res._handover
        attempts.append("start")

    monkeypatch.setattr(res, "start_host", start_host)
    await res._restore_update_handover()
    assert attempts == (["start"] if configured and not running else [])




