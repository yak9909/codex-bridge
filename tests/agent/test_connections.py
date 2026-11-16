"""Several hosts share one lender's task slot; persisted invites stay encrypted."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from codex_bridge.agent.connections import ConnectionStore
from codex_bridge.agent.main import AgentConnections
from codex_bridge.agent.settings import SettingsStore
from codex_bridge.agent.setup import load_invite, save_invite
from codex_bridge.agent.ui import create_ui_app
from codex_bridge.agent.worklog import WorkLog
from codex_bridge.common.invite import make_invite, parse_invite
from fake_helpers import Fake

WRITE = {"X-Codex-Bridge": "1"}


@pytest.fixture
async def multi(tmp_path):
    fakes = {}

    def factory(connection, settings):
        root = tmp_path / ("fake-" + connection.id)
        root.mkdir(exist_ok=True)
        fake = fakes[connection.id] = Fake(root)
        fake.app.host_url = connection.invite.url
        fake.app.token = connection.invite.token
        fake.app.features = settings["features"]
        return fake.app

    runtime = AgentConnections(tmp_path, app_factory=factory, versions={"auth_mode": "chatgpt"})
    a = await runtime.add(make_invite("http://a.invalid", "secret-a", "friend"))
    b = await runtime.add(make_invite("http://b.invalid", "secret-b", "friend"))
    app = create_ui_app(None, runtime.settings, None, versions={}, data_root=tmp_path, connections=runtime,
                        on_settings=runtime.apply_shared)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8766") as http:
        yield SimpleNamespace(runtime=runtime, a=a, b=b, http=http, fakes=fakes, home=tmp_path)
    await runtime.stop()


def start(task_id):
    return {"type": "task_start", "task_id": task_id, "prompt": "依頼"}


async def test_settings_are_per_connection_but_limits_are_shared(multi):
    m, a, b = multi.runtime, multi.a.id, multi.b.id
    await m.update_settings(a, {"pre_approval": {"enabled": True, "timeout_min": None},
                                "features": {"web_search": True}})
    await m.runners[a].handle(start("a-task"))
    assert m.runners[a].pending_approval["connection_id"] == a
    assert not m.runners[a].app.running  # pre-approval never starts Codex
    await m.runners[a].decide_pending(False)
    await m.runners[b].handle(start("b-task"))
    assert m.runners[b].cur is not None and m.runners[b].pending_approval is None
    patch = {"limits": {"five_hour": 35, "weekly": 45, "force": True}}
    response = await multi.http.put("/api/settings", json=patch, headers=WRITE)
    assert response.status_code == 200
    assert all(r.friend_limits == patch["limits"] for r in m.runners.values())
    assert all(r.guard.effective_limits()["five_hour"] == 35 for r in m.runners.values())
    await m.runners[b].shutdown()
    await m.runners[a].handle(start("a-allowed"))
    await m.runners[a].decide_pending(True)
    assert 'web_search="disabled"' not in multi.fakes[a].argv
    assert 'web_search="disabled"' in multi.fakes[b].argv
    assert set((await multi.http.get("/api/settings")).json()["settings"]) == {
        "limits", "workdir", "log_retention_days"}
    assert (await multi.http.put(f"/api/connections/{a}/settings", json={"limits": {}}, headers=WRITE)).status_code == 400


@pytest.mark.parametrize("pending", [False, True])
async def test_replace_invite_and_remove_refused_while_that_connection_is_busy(multi, pending):
    m, a, b = multi.runtime, multi.a.id, multi.b.id
    await m.update_settings(a, {"pre_approval": {"enabled": pending}})
    await m.runners[a].handle(start("busy"))
    for method, path, body in [("PUT", f"/api/connections/{a}/invite", {"code": make_invite("http://new", "s", "f")}),
                               ("DELETE", f"/api/connections/{a}", None)]:
        assert (await multi.http.request(method, path, json=body, headers=WRITE)).status_code == 409
    assert (await multi.http.put(f"/api/connections/{b}/invite", headers=WRITE,
                                 json={"code": make_invite("http://new", "s", "f")})).status_code == 200
    assert (await multi.http.delete(f"/api/connections/{b}", headers=WRITE)).status_code == 200


def test_migrates_single_invite_to_first_connection(tmp_path):
    code = make_invite("http://old.invalid", "secret", "friend")
    save_invite(tmp_path, parse_invite(code))
    SettingsStore(tmp_path / "settings.json").update({"pre_approval": {"enabled": True, "timeout_min": None},
                                                    "disposable": True, "limits": {"weekly": 40}})
    log = WorkLog(tmp_path / "logs" / "tasks")
    log.add("old-task", "task_prompt", {"text": "以前の依頼"})
    (tmp_path / "logs" / "visibility.txt").write_text("files", encoding="utf-8")
    (tmp_path / "sessions.json").write_text(json.dumps({"thread-old": {"created": 1, "task_ids": ["old-task"]}}))
    first, = ConnectionStore(tmp_path).list()
    assert first.invite == parse_invite(code)
    assert first.settings["pre_approval"] == {"enabled": True, "timeout_min": None}
    assert first.settings["disposable"] is True
    assert first.settings["log_visibility"] == "files"
    assert not (tmp_path / "invite.bin").exists()
    assert load_invite(tmp_path / "connections" / first.id) == first.invite
    assert b"secret" not in (tmp_path / "connections" / first.id / "invite.bin").read_bytes()
    assert WorkLog(tmp_path / "connections" / first.id / "logs" / "tasks").task_events("old-task")
    assert (tmp_path / "connections" / first.id / "sessions.json").exists()
    assert SettingsStore(tmp_path / "settings.json", shared=True).get()["limits"]["weekly"] == 40
    assert ConnectionStore(tmp_path).list()[0].id == first.id


async def test_worklog_and_sessions_show_their_connection(multi):
    m, a, b = multi.runtime, multi.a.id, multi.b.id
    await m.runners[a].handle(start("a-task"))
    await m.runners[a].shutdown()
    await m.runners[b].handle(start("b-task"))
    rows = (await multi.http.get("/api/tasks")).json()
    assert {(r["connection_id"], r["host_name"]) for r in rows} == {(a, "a.invalid"), (b, "b.invalid")}
    assert (await multi.http.get("/api/tasks/a-task", params={"connection_id": a})).json()["host_name"] == "a.invalid"
    sessions = (await multi.http.get("/api/sessions")).json()
    assert {(s["connection_id"], s["host_name"]) for s in sessions} == {(a, "a.invalid"), (b, "b.invalid")}
    assert len((await multi.http.get("/api/tasks", params={"connection_id": a})).json()) == 1
    assert len((await multi.http.get("/api/sessions", params={"connection_id": b})).json()) == 1


async def test_connections_api_validates_codes_settings_and_local_write_header(multi):
    for method, path, body in [("POST", "/api/connections", {"code": "bad"}),
                               ("PUT", f"/api/connections/{multi.a.id}/invite", {"code": "bad"}),
                               ("DELETE", f"/api/connections/{multi.a.id}", None),
                               ("PUT", f"/api/connections/{multi.a.id}/settings", {})]:
        assert (await multi.http.request(method, path, json=body)).status_code == 403
    assert (await multi.http.post("/api/connections", json={"code": "bad"}, headers=WRITE)).status_code == 400
    assert (await multi.http.post("/api/connections", json={"code": make_invite("http://a.invalid", "new", "friend")}, headers=WRITE)).status_code == 400
    new = await multi.http.post("/api/connections", json={"code": make_invite("http://c.invalid", "s", "f")}, headers=WRITE)
    assert new.status_code == 201
    rows = (await multi.http.get("/api/connections")).json()
    assert len(rows) == 3 and all(set(r) == {"id", "host_name", "connected", "invite_problem", "busy"} for r in rows)
    assert "secret" not in str(rows)
    assert (await multi.http.get("/api/connections/missing/settings")).status_code == 404
    for body in ({}, {"code": 1}, {"code": "bad", "other": "x"}):
        assert (await multi.http.post("/api/connections", json=body, headers=WRITE)).status_code == 400


async def test_concurrent_requests_reserve_one_slot_and_keep_each_pending_timeout(multi):
    m = multi.runtime
    for connection in (multi.a, multi.b):
        await m.update_settings(connection.id, {"pre_approval": {"enabled": True, "timeout_min": None}})
    a, b = (m.runners[c.id] for c in (multi.a, multi.b))
    await asyncio.gather(a.handle(start("same-id")), b.handle(start("same-id")))
    owner = m.slot.owner
    assert sum(r.pending_approval is not None for r in (a, b)) == 1
    assert len([msg for r in (a, b) for msg in r._unacked.values() if msg.get("state") == "rejected_by_lender"]) == 1
    await m.update_settings(owner.connection_id, {"pre_approval": {"timeout_min": 1}})
    owner.clock = lambda: 1e20
    await owner.expire_pending()
    assert owner.pending_approval is not None and owner.approval.pending.timeout_min is None
    await owner.decide_pending(False)
    assert m.slot.owner is None


async def test_repeated_task_and_thread_ids_are_isolated_by_connection(multi):
    m = multi.runtime
    for connection in (multi.a, multi.b):
        runner = m.runners[connection.id]
        await runner.handle(start("same-id"))
        await runner.shutdown()
    assert (await multi.http.get("/api/tasks/same-id")).status_code == 400
    for connection, host in ((multi.a, "a.invalid"), (multi.b, "b.invalid")):
        row = (await multi.http.get("/api/tasks/same-id", params={"connection_id": connection.id})).json()
        assert row["host_name"] == host
        assert all(e["connection_id"] == connection.id for e in row["events"])
    await m.update_settings(multi.a.id, {"log_visibility": "all"})
    await m.runners[multi.a.id].handle({"type": "settings", "log_visibility": "summary"})
    assert m.runners[multi.a.id].log_visibility == "all"
    assert m.runners[multi.b.id].log_visibility == "summary"


async def test_replacing_invite_keeps_sessions_only_for_the_same_host(multi):
    m, id = multi.runtime, multi.a.id
    await m.runners[id].handle(start("old-task"))
    await m.runners[id].shutdown()
    sessions = m.runners[id].sessions.list()
    unacked = dict(m.runners[id]._unacked)
    await m.replace_invite(id, make_invite("http://a.invalid", "rotated", "friend"))
    assert m.runners[id].sessions.list() == sessions
    assert m.runners[id]._unacked == unacked
    await m.replace_invite(id, make_invite("http://different.invalid", "other", "friend"))
    assert m.runners[id].sessions.list() == []
    assert m.runners[id]._unacked == {}
    row = (await multi.http.get("/api/tasks/old-task", params={"connection_id": id})).json()
    assert row["host_name"] == "a.invalid"  # the local history retains the original requester


def test_failed_migration_leaves_original_untouched(tmp_path, monkeypatch):
    import codex_bridge.agent.connections as module
    save_invite(tmp_path, parse_invite(make_invite("http://old", "s", "f")))
    original = (tmp_path / "invite.bin").read_bytes()

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(module, "save_invite", fail)
    with pytest.raises(OSError):
        ConnectionStore(tmp_path)
    assert (tmp_path / "invite.bin").read_bytes() == original
    assert not (tmp_path / "connections").exists()
