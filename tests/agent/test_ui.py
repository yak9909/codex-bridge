"""The local lender API: real runner/stores, no host or Codex process needed."""

from pathlib import Path
import asyncio
from types import SimpleNamespace

import httpx
import pytest

from codex_bridge.agent.appserver import AppServerError
from codex_bridge.agent.runner import AgentRunner, Current
from codex_bridge.agent.sessions import SessionRegistry
from codex_bridge.agent.settings import SettingsStore
from codex_bridge.agent.ui import create_ui_app
from codex_bridge.agent.usage import QuotaGuard, UsageSnapshot, Window
from codex_bridge.agent.worklog import WorkLog
from codex_bridge.common.invite import Invite

WRITE = {"X-Codex-Bridge": "1"}


class App:
    def __init__(self, root):
        self.workdir = root / "workdir"
        self.deleted = []
        self.fail_delete = False

    async def rate_limits(self):
        return UsageSnapshot(Window(46, 2000000000), Window(23, 2000100000))

    async def thread_start(self, developer_instructions, ephemeral=False, model=None):
        return {"id": "new-thread", "model": None, "effort": None}

    async def turn_start(self, thread_id, text, model=None, effort=None):
        return "turn"

    async def thread_delete(self, thread_id):
        if self.fail_delete:
            raise AppServerError("削除できません")
        self.deleted.append(thread_id)


@pytest.fixture
async def env(tmp_path):
    sent, changed, moved, pauses = [], [], [], []

    async def send(message):
        sent.append(message)

    settings = SettingsStore(tmp_path / "settings.json")
    log = WorkLog(tmp_path / "logs" / "tasks")
    runner = AgentRunner(App(tmp_path), QuotaGuard({"five_hour": 90, "weekly": 70}),
                         SessionRegistry(tmp_path / "sessions.json"), send, worklog=log)
    runner.apply_settings(settings.get())
    client = SimpleNamespace(invite=Invite("http://host.invalid", "SECRET", "依頼主 <名前>"),
                             ws=object(), paused=False)

    async def apply(saved):
        changed.append(saved)
        runner.apply_settings(saved)

    async def move(path):
        if path.name == "bad":
            raise OSError("書けません")
        moved.append(path)
        return []

    async def pause(value):
        pauses.append(value)
        client.paused = value

    app = create_ui_app(runner, settings, client, versions={"agent_version": "0.2.test", "codex_version": "0.167"},
                        data_root=tmp_path, on_settings=apply, on_move=move, on_pause=pause)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8766") as http:
        yield SimpleNamespace(http=http, app=app, runner=runner, log=log, settings=settings, client=client,
                              sent=sent, changed=changed, moved=moved, pauses=pauses, root=tmp_path)


async def test_page_and_shared_assets(env):
    for path, content in [("/", "Codex を貸している人"), ("/static/app.js", "UI.taskDetail"),
                          ("/admin/common/ui.js", "function meter"), ("/admin/common/ui.css", ".topbar")]:
        response = await env.http.get(path)
        assert response.status_code == 200 and content in response.text
        assert response.headers["cache-control"] == "no-store"
    assert (await env.http.get("/api/data-location")).status_code == 404
    assert (await env.http.put("/api/data-location", json={"path": "D:/x"}, headers=WRITE)).status_code == 404


async def test_final_review_shutdown_records_failure_and_sends_to_host(env):
    await env.runner.handle({"type": "task_start", "task_id": "t1", "prompt": "do"})
    assert env.runner.cur is not None
    await env.runner.shutdown()
    states = [e["data"] for e in env.log.task_events("t1") if e["kind"] == "state"]
    assert states[-1]["state"] == "failed"
    assert "終了" in states[-1]["report"] and "中断" in states[-1]["report"]
    assert env.runner.cur is None
    sent = [m for m in env.sent if m["type"] == "task_state"]
    assert sent[-1]["state"] == "failed" and sent[-1]["report"] == states[-1]["report"]
    await env.runner.shutdown()
    assert len([m for m in env.sent if m["type"] == "task_state"]) == len(sent)


@pytest.mark.parametrize("state", ["running", "awaiting_lender", "queued"])
async def test_final_review_orphaned_worklog_task_shows_interrupted(env, state):
    env.log.add("lost", "task_prompt", {"text": "do"})
    env.log.add("lost", "state", {"state": state})
    task = (await env.http.get("/api/tasks/lost")).json()
    assert task["state"] == "failed" and "中断" in task["report"] and task["finished"] is not None


@pytest.mark.parametrize("path", ["/", "/static/app.js", "/admin/common/ui.js", "/api/overview", "/api/tasks",
                                  "/api/tasks/t1", "/api/pending", "/api/sessions", "/api/settings",
                                  "/api/connections", "/api/connections/missing/settings"])
async def test_every_read_checks_host(env, path):
    assert (await env.http.get(path, headers={"Host": "evil.invalid:8766"})).status_code == 403


@pytest.mark.parametrize("host", ["127.0.0.1.evil:8766", "evil:127.0.0.1", "[::1]:8766", "127.0.0.1:99999"])
async def test_host_lookalikes_are_rejected(env, host):
    assert (await env.http.get("/api/overview", headers={"Host": host})).status_code == 403


async def test_nonlocal_peer_is_rejected_even_with_good_host(env):
    transport = httpx.ASGITransport(app=env.app, client=("192.168.1.8", 1))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8766") as http:
        assert (await http.get("/api/overview")).status_code == 403


@pytest.mark.parametrize("method,path,body", [
    ("PUT", "/api/settings", {"disposable": True}), ("POST", "/api/settings/data-root", {"path": "D:/new"}),
    ("POST", "/api/logs/clear", {}), ("POST", "/api/pause", {"paused": True}),
    ("POST", "/api/pending", {"task_id": "t1", "approve": True}), ("DELETE", "/api/sessions/s1", None),
    ("POST", "/api/connections", {"code": "bad"}), ("PUT", "/api/connections/a/invite", {"code": "bad"}),
    ("DELETE", "/api/connections/a", None), ("PUT", "/api/connections/a/settings", {}),
])
async def test_every_write_requires_header_and_checks_host(env, method, path, body):
    for headers in ({}, {"X-Codex-Bridge": "0"}, {**WRITE, "Host": "evil:8766"}):
        assert (await env.http.request(method, path, json=body, headers=headers)).status_code == 403
    assert env.changed == env.moved == env.pauses == []


async def test_overview_reports_live_state_versions_usage_and_both_limits_without_secrets(env):
    await env.runner.refresh_usage()
    data = (await env.http.get("/api/overview")).json()
    # Invite.name identifies this lender, not the host. With no host display-name field in the protocol,
    # show the connection address rather than misidentifying the lender as the requester.
    assert data["host_name"] == "host.invalid" and data["connected"] is True
    assert data["state"] == "idle" and data["agent_version"] == "0.2.test"
    assert data["codex_version"] == "0.167"
    assert data["usage"]["five_hour"]["used_percent"] == 46
    assert data["usage"]["five_hour"]["resets_at_local"]
    assert data["limits"] == {"five_hour": 80, "weekly": 80, "force": False}
    assert data["thresholds"] == {"five_hour": 90, "weekly": 70}
    assert data["effective_limits"] == {"five_hour": 80, "weekly": 70}
    assert "SECRET" not in str(data)
    env.runner.cur = Current("t1", "s1")
    assert (await env.http.get("/api/overview")).json()["state"] == "running"
    env.client.ws = None
    assert (await env.http.get("/api/overview")).json()["connected"] is False


def records(env):
    for kind, data in [
        ("task_prompt", {"text": "依頼 <script>"}), ("task_prompt", {"text": "追加の指示"}),
        ("agent_message", {"text": "途中経過"}), ("plan", {"steps": [{"step": "確認", "status": "completed"}]}),
        ("reasoning_summary", {"text": "要約"}), ("token_usage", {"input": 10, "output": 2}),
        ("tool_started", {"tool": "write_file", "arguments": {"path": "a.txt", "content": "PRIVATE"}}),
        ("tool_finished", {"tool": "write_file", "arguments": {"path": "a.txt", "content": "PRIVATE"},
                           "success": True, "result_summary": "PRIVATE RESULT"}),
        ("tool_call", {"tool": "read_file", "arguments": '{"path":"legacy.txt","other":"PRIVATE"}'}),
        ("state", {"state": "completed", "report": "最終報告 <img>", "resume_at": None}),
    ]:
        env.log.add("t1", kind, data)


@pytest.mark.parametrize("visibility", ["summary", "files", "all", "unknown"])
async def test_tasks_from_worklog_always_show_required_records_and_filter_tools(env, visibility):
    records(env)
    env.runner.log_visibility = visibility
    env.runner.sessions.add("s1", "t1")
    before = env.log.task_events("t1")
    task = (await env.http.get("/api/tasks/t1")).json()
    assert task["prompt"] == "依頼 <script>" and task["report"] == "最終報告 <img>"
    assert task["state"] == "completed" and task["finished"] and task["thread_id"] == "s1"
    kinds = [e["kind"] for e in task["events"]]
    assert {"task_prompt", "agent_message", "plan", "state", "reasoning_summary", "token_usage"} <= set(kinds)
    assert sum(k == "task_prompt" for k in kinds) == 2
    if visibility in ("summary", "unknown"):
        assert not any(k.startswith("tool_") for k in kinds)
    elif visibility == "files":
        tools = [e["data"] for e in task["events"] if e["kind"].startswith("tool_")]
        assert tools[0]["arguments"] == {"path": "a.txt"}
        assert tools[-1]["arguments"] == {"path": "legacy.txt"}
        assert "PRIVATE" not in str(tools)
    else:
        assert "PRIVATE RESULT" in str(task["events"])
    assert env.log.task_events("t1") == before  # display filtering never changes the recording
    listing = (await env.http.get("/api/tasks")).json()
    assert listing[0]["task_id"] == "t1" and listing[0]["state"] == "completed"
    assert "PRIVATE" not in str(listing)


async def test_live_task_uses_memory_progress_and_does_not_record_deltas(env):
    records(env)
    env.runner.cur = Current("t1", "s1", progress="書きかけ <b>")
    task = (await env.http.get("/api/tasks/t1")).json()
    assert task["progress"] == "書きかけ <b>"
    assert task["state"] == "running" and task["finished"] is None
    assert all(e["kind"] != "progress" for e in env.log.task_events("t1"))
    assert (await env.http.get("/api/tasks/missing")).status_code == 404
    assert (await env.http.get("/api/tasks/bad.name")).status_code == 404


async def hold(env, task_id="t1"):
    env.runner.settings["pre_approval"]["enabled"] = True
    await env.runner.handle({"type": "task_start", "task_id": task_id, "prompt": "承認して <b>"})


@pytest.mark.parametrize("approve", [True, False])
async def test_pending_decisions_use_the_task_id_seen_and_cannot_be_repeated(env, approve):
    assert (await env.http.get("/api/pending")).json() is None
    await hold(env)
    pending = (await env.http.get("/api/pending")).json()
    assert pending["task_id"] == "t1" and pending["kind"] == "start" and pending["created"]
    assert (await env.http.get("/api/overview")).json()["state"] == "awaiting_lender"
    body = {"task_id": "t1", "approve": approve, "reason": "今は使えません"}
    assert (await env.http.post("/api/pending", json=body, headers=WRITE)).status_code == 200
    assert env.runner.pending_approval is None
    assert (await env.http.post("/api/pending", json=body, headers=WRITE)).status_code == 409
    if not approve:
        assert "今は使えません" in env.sent[-1]["report"]


async def test_stale_approval_cannot_decide_a_different_pending_task(env):
    await hold(env, "new")
    response = await env.http.post("/api/pending", json={"task_id": "old", "approve": True}, headers=WRITE)
    assert response.status_code == 409 and env.runner.pending_approval["task_id"] == "new"
    assert env.runner.cur is None
    assert await env.runner.decide_pending(True, task_id="old") is False
    assert env.runner.pending_approval["task_id"] == "new"


@pytest.mark.parametrize("body", [{"approve": True}, {"task_id": "t1", "approve": "false"},
                                  {"task_id": "t1", "approve": True, "reason": 1}])
async def test_invalid_decisions_do_not_touch_pending(env, body):
    await hold(env)
    assert (await env.http.post("/api/pending", json=body, headers=WRITE)).status_code == 422
    assert env.runner.pending_approval["task_id"] == "t1"


async def test_sessions_are_owned_only_active_disposable_and_filter_tasks(env):
    records(env)
    env.runner.sessions.add("s1", "t1")
    env.runner.sessions.add("s2", "t2", ephemeral=True)
    env.runner.cur = Current("t2", "s2", ephemeral=True)
    sessions = (await env.http.get("/api/sessions")).json()
    assert set(sessions[0]) == {"thread_id", "created", "task_ids", "active", "disposable"}
    assert sessions[0]["task_ids"] == ["t1"] and sessions[0]["active"] is False
    assert sessions[1]["active"] is sessions[1]["disposable"] is True
    assert [t["task_id"] for t in (await env.http.get("/api/tasks?thread_id=s1")).json()] == ["t1"]
    assert (await env.http.get("/api/tasks?thread_id=unowned")).status_code == 404
    assert (await env.http.delete("/api/sessions/s2", headers=WRITE)).status_code == 409
    assert (await env.http.delete("/api/sessions/unowned", headers=WRITE)).status_code == 404
    assert (await env.http.delete("/api/sessions/s1", headers=WRITE)).status_code == 200
    assert env.runner.app.deleted == ["s1"] and not env.runner.sessions.owns("s1")
    assert env.log.task_events("t1")  # deleting conversation does not erase the work log


async def test_failed_session_delete_preserves_registry(env):
    env.runner.sessions.add("s1", "t1")
    env.runner.app.fail_delete = True
    assert (await env.http.delete("/api/sessions/s1", headers=WRITE)).status_code == 502
    assert env.runner.sessions.owns("s1")


async def test_settings_defaults_validation_and_apply(env):
    data = (await env.http.get("/api/settings")).json()
    assert data["settings"]["limits"] == {"five_hour": 80, "weekly": 80, "force": False}
    assert all(v is False for v in data["settings"]["features"].values())
    assert data["settings"]["pre_approval"] == {"enabled": False, "timeout_min": 30}
    assert data["settings"]["disposable"] is False and data["settings"]["log_retention_days"] == 30
    assert data["feature_info"]["multi_agent"]["best_effort"] is True
    assert data["data_root"] == str(env.root) and data["workdir"] == str(env.runner.app.workdir)
    response = await env.http.put("/api/settings", json={"disposable": True, "limits": {"five_hour": 60}}, headers=WRITE)
    assert response.status_code == 200 and env.changed[-1] == env.settings.get()
    for bad in ({"disposable": "true"}, {"limits": {"weekly": 0}}, {"unexpected": True}):
        assert (await env.http.put("/api/settings", json=bad, headers=WRITE)).status_code == 400
    assert len(env.changed) == 1


async def test_data_root_route_reports_failure_and_requires_absolute_path(env):
    for path in ("", "relative", str(env.root / "bad")):
        assert (await env.http.post("/api/settings/data-root", json={"path": path}, headers=WRITE)).status_code == 400
    target = env.root / "new"
    response = await env.http.post("/api/settings/data-root", json={"path": str(target)}, headers=WRITE)
    assert response.status_code == 200 and response.json()["moved"] is True
    assert env.moved == [target]


async def test_clear_only_worklogs_and_pause_uses_runtime_callback(env):
    records(env)
    env.runner.sessions.add("s1", "t1")
    assert (await env.http.post("/api/logs/clear", headers=WRITE)).json() == {"cleared": 1}
    assert env.log.tasks() == [] and env.runner.sessions.owns("s1")
    assert (await env.http.post("/api/pause", json={"paused": True}, headers=WRITE)).json() == {"paused": True}
    assert env.pauses == [True] and (await env.http.get("/api/overview")).json()["state"] == "paused"
    assert (await env.http.post("/api/pause", json={"paused": False}, headers=WRITE)).status_code == 200
    assert env.pauses == [True, False]
    assert (await env.http.post("/api/pause", json={"paused": "false"}, headers=WRITE)).status_code == 422


async def test_pending_same_task_new_followup_cannot_use_an_old_screen(env):
    await hold(env)
    body = {**env.runner.pending_approval, "approve": False}
    body.pop("prompt")
    body.pop("kind")
    env.runner.approval.pending.created += 1
    assert (await env.http.post("/api/pending", json=body, headers=WRITE)).status_code == 409
    assert env.runner.pending_approval is not None


@pytest.mark.parametrize("pending", [True, False])
async def test_data_move_is_refused_while_busy_or_waiting(env, pending):
    if pending:
        await hold(env)
    else:
        env.runner.cur = Current("t1", "s1")
    response = await env.http.post("/api/settings/data-root", json={"path": str(env.root / "new")}, headers=WRITE)
    assert response.status_code == 409 and env.moved == []


async def test_successful_data_move_prevents_any_more_writes_to_old_stores(env):
    assert (await env.http.post("/api/settings/data-root", json={"path": str(env.root / "new")}, headers=WRITE)).status_code == 200
    env.runner.sessions.add("s1", "t1")
    assert (await env.http.delete("/api/sessions/s1", headers=WRITE)).status_code == 409
    assert env.runner.sessions.owns("s1") and env.runner.app.deleted == []
    assert (await env.http.put("/api/settings", json={"disposable": True}, headers=WRITE)).status_code == 409


async def test_a_session_cannot_be_resumed_while_its_deletion_is_in_flight(env):
    env.runner.sessions.add("s1", "t1")
    entered, finish = asyncio.Event(), asyncio.Event()

    async def delete(thread_id):
        entered.set()
        await finish.wait()
    env.runner.app.thread_delete = delete
    deletion = asyncio.create_task(env.http.delete("/api/sessions/s1", headers=WRITE))
    await entered.wait()
    resume = asyncio.create_task(env.runner.handle({"type": "task_start", "task_id": "t2", "prompt": "続き",
                                                    "continue_thread_id": "s1"}))
    await asyncio.sleep(0)
    assert env.runner.cur is None
    finish.set()
    assert (await deletion).status_code == 200
    await resume
    assert env.runner.cur is None and env.sent[-1]["state"] == "failed"


async def test_files_visibility_never_exposes_nested_nonpath_arguments(env):
    env.runner.log_visibility = "files"
    env.log.add("t1", "tool_finished", {"tool": "write_file", "arguments": {"path": {"content": "SECRET"}},
                                        "result_summary": "SECRET"})
    task = (await env.http.get("/api/tasks/t1")).json()
    assert "SECRET" not in str(task)
