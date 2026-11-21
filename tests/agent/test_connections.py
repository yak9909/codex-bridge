"""Several hosts share one lender's task slot; persisted invites stay encrypted."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from codex_bridge.agent.connections import ConnectionStore
from codex_bridge.agent.main import AgentConnections
from codex_bridge.agent.runner import Current
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


def test_connection_settings_keep_model_limits(tmp_path):
    store = ConnectionStore(tmp_path)
    a = store.add(make_invite("http://a.invalid", "secret-a", "friend"))
    b = store.add(make_invite("http://b.invalid", "secret-b", "friend"))
    patch = {"allowed_models": ["m1"], "max_effort": "high"}
    updated = store.update_settings(a.id, patch)
    reread = {c.id: c.settings for c in ConnectionStore(tmp_path).list()}
    assert updated["allowed_models"] == ["m1"] and updated["max_effort"] == "high"
    assert reread[a.id]["allowed_models"] == ["m1"] and reread[a.id]["max_effort"] == "high"
    assert reread[b.id]["allowed_models"] is None and reread[b.id]["max_effort"] == "xhigh"


@pytest.mark.parametrize("damaged", [False, True])
async def test_unreadable_connection_is_skipped_and_logged_without_blocking_startup(multi, damaged):
    bad_id = "000000000000"
    path = multi.home / "connections" / bad_id
    path.mkdir()
    if damaged:
        (path / "invite.bin").write_bytes(b"not a DPAPI invite")
    logged = []
    runtime = AgentConnections(multi.home, app_factory=multi.runtime.app_factory, versions={}, on_log=logged.append)
    try:
        assert {c.id for c in runtime.store.list()} == {multi.a.id, multi.b.id}
        assert {r["id"] for r in runtime.rows()} == {multi.a.id, multi.b.id}
        assert any(bad_id in row["text"] and "読めません" in row["text"] for row in logged)
        assert path.is_dir()  # retain the damaged original for recovery
        await runtime.add(make_invite("http://new.invalid", "s", "friend"))
        assert len(runtime.rows()) == 3
    finally:
        await runtime.stop()


async def test_stop_bounds_each_shutdown_waiting_for_the_shared_lock(multi, monkeypatch):
    from codex_bridge.agent import main
    stopped = []
    with monkeypatch.context() as patch:
        patch.setattr(main, "SHUTDOWN_TIMEOUT", 0.02, raising=False)
        for id, runner in multi.runtime.runners.items():
            original = runner.app.stop

            async def stop(id=id, original=original):
                stopped.append(id)
                await original()

            patch.setattr(runner.app, "stop", stop)
        async with multi.runtime.slot.lock:
            await asyncio.wait_for(multi.runtime.stop(), timeout=0.4)
        assert set(stopped) == {multi.a.id, multi.b.id}
        assert not multi.runtime.started


async def test_stop_continues_after_shutdown_and_runner_cleanup_errors(multi, monkeypatch):
    stopped, logged, shutdowns = [], [], []
    a, b = (multi.runtime.runners[c.id] for c in (multi.a, multi.b))

    async def fail_shutdown():
        raise OSError("shutdown failed")

    async def fail_stop():
        raise OSError("runner stop failed")

    async def second_shutdown():
        shutdowns.append("second")

    with monkeypatch.context() as patch:
        patch.setattr(multi.runtime, "on_log", logged.append)
        patch.setattr(a, "shutdown", fail_shutdown)
        patch.setattr(a, "stop", fail_stop)
        patch.setattr(b, "shutdown", second_shutdown)
        for id, runner in multi.runtime.runners.items():
            original = runner.app.stop

            async def stop(id=id, original=original):
                stopped.append(id)
                await original()

            patch.setattr(runner.app, "stop", stop)
        await multi.runtime.stop()
        assert shutdowns == ["second"]
        assert set(stopped) == {multi.a.id, multi.b.id}
        assert any("shutdown failed" in row["text"] for row in logged)
        assert any("runner stop failed" in row["text"] for row in logged)


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
        "limits", "workdir", "log_retention_days", "max_parallel"}
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


@pytest.mark.parametrize("legacy", [True, False])
def test_pre_approval_legacy_enabled_in_the_connections_store(tmp_path, legacy):
    store = ConnectionStore(tmp_path)
    c = store.add(make_invite("http://a.invalid", "secret-a", "friend"))
    path = tmp_path / "connections" / c.id / "settings.json"
    path.write_text(json.dumps({"pre_approval": {"enabled": legacy, "timeout_min": 5}}), encoding="utf-8")
    assert ConnectionStore(tmp_path).list()[0].settings["pre_approval"] == {
        "new_task": legacy, "followup": legacy, "timeout_min": 5}
    saved = store.update_settings(c.id, {"pre_approval": {"followup": True}})["pre_approval"]
    assert saved == {"new_task": legacy, "followup": True, "timeout_min": 5}
    assert json.loads(path.read_text(encoding="utf-8"))["pre_approval"] == saved


async def test_lender_models_resent_on_settings_change(multi):
    m, a, b = multi.runtime, multi.a.id, multi.b.id
    sent = {a: [], b: []}
    for id in (a, b):
        async def send(msg, id=id):
            sent[id].append(msg)
        m.runners[id].send = send
    await m.update_settings(a, {"pre_approval": {"new_task": True}})
    models = [x for x in sent[a] if x["type"] == "lender_models"]
    assert models and models[-1]["lender_view"]["pre_approval"]["new_task"] is True
    assert not [x for x in sent[b] if x["type"] == "lender_models"]
    await m.apply_shared({"limits": {"five_hour": 35, "weekly": 45, "force": True}})
    for id in (a, b):
        assert [x for x in sent[id] if x["type"] == "lender_models"][-1]["lender_view"]["over_limit"] == "stop"


async def test_clear_one_connection_log(multi):
    m, a, b = multi.runtime, multi.a.id, multi.b.id
    for id, task in ((a, "a-task"), (b, "b-task")):
        m.runners[id].worklog.add(task, "task_prompt", {"text": "依頼"})
    response = await multi.http.post(f"/api/connections/{a}/logs/clear", headers=WRITE)
    assert response.status_code == 200 and response.json() == {"cleared": 1}
    assert m.runners[a].worklog.tasks() == []
    assert [t["task_id"] for t in m.runners[b].worklog.tasks()] == ["b-task"]  # the other connection stays
    assert (await multi.http.post(f"/api/connections/{a}/logs/clear")).status_code == 403  # needs the header


async def test_clear_unknown_connection_404(multi):
    assert (await multi.http.post("/api/connections/000000000000/logs/clear", headers=WRITE)).status_code == 404


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
    assert first.settings["pre_approval"] == {"new_task": True, "followup": True, "timeout_min": None}
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
    assert len(rows) == 3 and all(set(r) == {"id", "host_name", "connected", "invite_problem", "busy", "active_task_ids"} for r in rows)
    assert "secret" not in str(rows)
    assert (await multi.http.get("/api/connections/missing/settings")).status_code == 404
    for body in ({}, {"code": 1}, {"code": "bad", "other": "x"}):
        assert (await multi.http.post("/api/connections", json=body, headers=WRITE)).status_code == 400


async def test_concurrent_requests_reserve_one_slot_and_keep_each_pending_timeout(multi):
    m = multi.runtime
    await m.apply_shared(m.settings.update({"max_parallel": 1}))
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


async def test_prepare_does_not_restart_an_idle_appserver_for_deferred_settings(multi):
    runtime = multi.runtime
    runner = runtime.runners[multi.a.id]
    await runner.prepare()
    app = runner.app
    process = app.proc
    old_features = dict(app.features)
    runner.settings["features"]["goals"] = not old_features["goals"]
    await runner.prepare()
    assert app.proc is process and app.features == old_features
    await runner.handle(start("needs-new-features"))
    assert app.proc is not process and app.features == runner.settings["features"]


@pytest.mark.parametrize("operation", ["remove", "replace_invite", "move_data"])
async def test_connection_changes_finish_while_followup_steer_is_pending(multi, monkeypatch, operation):
    runtime = multi.runtime
    connection_id = multi.a.id
    runner = runtime.runners[connection_id]
    entered = asyncio.Event()

    async def steer(*args):
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr(runner.app, "turn_steer", steer)
    monkeypatch.setattr("codex_bridge.agent.main.move_data_root", lambda *a, **k: [])
    assert await runner._reserve_slot(start("task")) is None
    runner.currents["task"] = Current("task", "thread-1", "turn-1")
    await runner.receive({"type": "task_followup", "task_id": "task", "text": "more"})
    await asyncio.wait_for(entered.wait(), 3)
    worker = runner._followup_workers["task"]
    async with runner._lock:
        await runner._on_event({"method": "turn/completed", "params": {
            "threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}}})
    assert not runner.active_task_ids()
    assert not worker.done()

    if operation == "remove":
        change = runtime.remove(connection_id)
    elif operation == "replace_invite":
        invite = make_invite("http://a.invalid", "rotated", "friend")
        change = runtime.replace_invite(connection_id, invite)
    else:
        change = runtime.move_data(multi.home / "new")
    await asyncio.wait_for(change, 3)
    assert worker.cancelled()
    assert "task" not in runner._followup_workers


async def test_data_move_blocks_new_admission_without_holding_the_pc_lock(multi, monkeypatch):
    from codex_bridge.agent import main

    runtime = multi.runtime
    entered, resume = asyncio.Event(), asyncio.Event()
    original = runtime._stop_connection

    async def stop(id):
        entered.set()
        await resume.wait()
        await original(id)

    monkeypatch.setattr(runtime, "_stop_connection", stop)
    monkeypatch.setattr(main, "move_data_root", lambda *a, **k: [])
    moving = asyncio.create_task(runtime.move_data(multi.home / "new"))
    try:
        await asyncio.wait_for(entered.wait(), 2)

        async def inspect():
            async with runtime.slot.lock:
                assert runtime.slot.moving and runtime.slot.count == 0

        await asyncio.wait_for(inspect(), 0.5)
        # Admission is blocked by the moving flag, not by a long-held PC lock.
        r = runtime.runners[multi.b.id]
        refusal = await asyncio.wait_for(r._reserve_slot(start("during-move")), 0.5)
        assert "変更中" in refusal and not r._reservations
    finally:
        resume.set()
        await asyncio.wait_for(moving, 2)


async def test_prepare_does_not_restart_a_task_waiting_for_thread_start(multi, monkeypatch):
    r = multi.runtime.runners[multi.a.id]
    entered, resume = asyncio.Event(), asyncio.Event()
    original = r.app.thread_start

    async def thread_start(*args, **kwargs):
        entered.set()
        await resume.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(r.app, "thread_start", thread_start)
    starting = asyncio.create_task(r.handle(start("starting")))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        process, old_features = r.app.proc, dict(r.app.features)
        r.settings["features"]["goals"] = not old_features["goals"]
        await r.prepare()
        assert r.app.proc is process and r.app.features == old_features
        assert r.currents["starting"].thread_id is None
    finally:
        resume.set()
        await asyncio.wait_for(asyncio.gather(starting, return_exceptions=True), 10)
