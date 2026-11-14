import json

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Mount

from codex_bridge.host.claude_mcp import build_claude_mcp
from codex_bridge.host.tasks import TaskManager
from codex_bridge.host.tokens import TokenStore
from codex_bridge.common.invite import parse_invite
from mcp_helpers import running

ACCEPT = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture
async def env(share):
    mgr = TaskManager(share.data_dir, touched_fn=share.snapshots.touched)
    share.tokens = TokenStore(share.data_dir)
    ep = build_claude_mcp(mgr, share.approvals, share.snapshots, policy=share.policy, tokens=share.tokens,
                          public_bind="100.1.2.3:8765", share_root=share.root)
    app = Starlette(routes=[Mount("/claude", app=ep.asgi)])
    async with running(ep):
        local = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 5000)),
                                  base_url="http://127.0.0.1:8770")
        remote = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("100.64.0.2", 5000)),
                                   base_url="http://127.0.0.1:8770")
        async with local, remote:
            yield mgr, local, remote


async def call(client, name: str, args: dict) -> dict:
    r = await client.post("/claude/mcp", headers=ACCEPT, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
    assert r.status_code == 200, r.text
    return r.json()["result"]


async def test_tool_names(env):
    mgr, local, _ = env
    r = await local.post("/claude/mcp", headers=ACCEPT, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert {t["name"] for t in r.json()["result"]["tools"]} == {
        "send_task", "send_followup", "get_status", "get_result", "cancel_task", "get_usage",
        "set_thresholds", "undo_task", "list_sessions", "read_session", "delete_session",
        "list_approvals", "decide_approval", "get_policy", "set_policy", "set_exec_mode", "set_paused",
        "set_log_visibility", "list_tasks", "issue_invite", "revoke_invite", "list_invites", "get_overview"}


def text_of(res: dict):
    assert not res.get("isError"), res
    return json.loads(res["content"][0]["text"])


async def test_claude_can_decide_approvals(env, share):
    _, local, _ = env
    a = share.approvals.request("command", "python x.py", task_id="t1")
    listed = text_of(await call(local, "list_approvals", {}))
    assert listed[0]["id"] == a.id and listed[0]["summary"] == "python x.py"
    assert text_of(await call(local, "decide_approval", {"approval_id": a.id, "approve": True}))["status"] == "approved"
    assert share.approvals.get(a.id).status == "approved"
    res = await call(local, "decide_approval", {"approval_id": a.id, "approve": False})
    assert res["isError"] and "もうありません" in res["content"][0]["text"]


async def test_claude_can_read_and_change_policy(env, share):
    _, local, _ = env
    assert "default" in text_of(await call(local, "get_policy", {}))["text"]
    bad = await call(local, "set_policy", {"text": "rules: ["})
    assert bad["isError"] and share.policy.current().exec_mode == "allowlist"
    text_of(await call(local, "set_policy", {"text": "default: write\nexec: {mode: ask}\n"}))
    assert share.policy.current().default == "write" and share.policy.current().exec_mode == "ask"


async def test_claude_can_switch_exec_mode_pause_and_visibility(env, share):
    mgr, local, _ = env
    text_of(await call(local, "set_exec_mode", {"mode": "off"}))
    assert share.policy.current().exec_mode == "off"
    assert (await call(local, "set_exec_mode", {"mode": "weird"}))["isError"]
    text_of(await call(local, "set_paused", {"paused": True}))
    assert share.policy.current().paused is True
    text_of(await call(local, "set_log_visibility", {"value": "all"}))
    assert mgr.settings["log_visibility"] == "all"


async def test_claude_can_manage_invites(env, share):
    _, local, _ = env
    code = text_of(await call(local, "issue_invite", {"name": "friend"}))["code"]
    assert parse_invite(code).url == "http://100.1.2.3:8765"
    assert [i["name"] for i in text_of(await call(local, "list_invites", {}))] == ["friend"]
    text_of(await call(local, "revoke_invite", {"name": "friend"}))
    assert share.tokens.hashes() == set()


async def test_overview_and_task_list(env, share):
    _, local, _ = env
    o = text_of(await call(local, "get_overview", {}))
    assert o["agent"]["connected"] is False and o["share_root"] == str(share.root) and "usage" in o
    assert text_of(await call(local, "list_tasks", {})) == []


async def test_non_loopback_rejected(env):
    _, _, remote = env
    r = await remote.post("/claude/mcp", headers=ACCEPT, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 403


async def test_status_shows_pending_approval(env, share):
    mgr, local, _ = env
    share.approvals.request("command", "python x.py", task_id="t1")
    res = await call(local, "get_status", {})
    s = json.loads(res["content"][0]["text"])
    assert s["pending_approvals"][0]["summary"] == "python x.py" and "decide_approval" in s["note"]


async def test_send_task_without_agent_is_error(env):
    _, local, _ = env
    res = await call(local, "send_task", {"prompt": "x"})
    assert res["isError"] and "接続されていません" in res["content"][0]["text"]


async def test_send_task_with_agent(env):
    mgr, local, _ = env
    sent = []

    async def send(m):
        sent.append(m)
    mgr.attach(send)
    res = await call(local, "send_task", {"prompt": "x"})
    tid = json.loads(res["content"][0]["text"])["task_id"]
    assert sent[-1]["type"] == "task_start" and sent[-1]["task_id"] == tid


async def test_undo_task(env, share):
    mgr, local, _ = env
    from codex_bridge.host.tasks import TaskRecord
    mgr.tasks["t5"] = TaskRecord(task_id="t5", prompt="p", created=0, state="completed")
    share.task("t5")
    share.write.write_file("out/u.txt", "u")
    res = await call(local, "undo_task", {"task_id": "t5"})
    assert json.loads(res["content"][0]["text"])["restored"] == ["out/u.txt"]
    assert not (share.root / "out/u.txt").exists()


async def test_get_usage_and_thresholds(env):
    mgr, local, _ = env
    res = await call(local, "set_thresholds", {"five_hour": 80})
    assert json.loads(res["content"][0]["text"])["five_hour"] == 80
    u = json.loads((await call(local, "get_usage", {}))["content"][0]["text"])
    assert u["thresholds"]["five_hour"] == 80 and u["five_hour"] is None


async def test_undo_unknown_or_unsafe_task_id_refused(env, share):
    # Review C3
    mgr, local, _ = env
    (share.data_dir / "keep.txt").write_text("k", encoding="utf-8")
    for bad in ("..", "nope"):
        res = await call(local, "undo_task", {"task_id": bad})
        assert res["isError"]
    assert (share.data_dir / "keep.txt").exists()


async def test_get_status_waits_until_task_finishes(env):
    # Claude Code cannot sleep; get_status(wait_sec) lets it wait for progress.
    import asyncio, time
    mgr, local, _ = env
    sent = []

    async def send(m):
        sent.append(m)
    link = mgr.attach(send)
    tid = json.loads((await call(local, "send_task", {"prompt": "x"}))["content"][0]["text"])["task_id"]

    async def finish_later():
        await asyncio.sleep(0.5)
        await mgr.on_agent_message(link, {"type": "task_state", "seq": 1, "task_id": tid, "state": "completed",
                                          "thread_id": "th", "report": "ok"})
    asyncio.create_task(finish_later())
    t0 = time.monotonic()
    s = json.loads((await call(local, "get_status", {"task_id": tid, "wait_sec": 10}))["content"][0]["text"])
    assert s["state"] == "completed" and 0.3 < time.monotonic() - t0 < 5


async def test_get_status_wait_returns_on_new_approval(env, share):
    import asyncio, time
    mgr, local, _ = env

    async def send(m):
        pass
    mgr.attach(send)
    tid = json.loads((await call(local, "send_task", {"prompt": "x"}))["content"][0]["text"])["task_id"]
    asyncio.get_running_loop().call_later(0.5, lambda: share.approvals.request("command", "python x.py", tid))
    t0 = time.monotonic()
    s = json.loads((await call(local, "get_status", {"task_id": tid, "wait_sec": 10}))["content"][0]["text"])
    assert s["pending_approvals"] and time.monotonic() - t0 < 5
