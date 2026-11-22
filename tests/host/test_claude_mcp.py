import json
import time
from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Mount

from codex_bridge.host import claude_mcp
from codex_bridge.host.claude_mcp import build_claude_mcp
from codex_bridge.host.policy_form import add_folder, remove_folder, set_main_folder
from codex_bridge.host.tasks import TaskManager
from codex_bridge.host.tokens import TokenStore
from codex_bridge.common.invite import parse_invite
from mcp_helpers import running

ACCEPT = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture
async def env(share):
    mgr = TaskManager(share.data_dir, touched_fn=share.snapshots.touched, policy_fn=share.policy.current)
    share.tokens = TokenStore(share.data_dir)
    ep = build_claude_mcp(mgr, share.approvals, share.snapshots, policy=share.policy, tokens=share.tokens,
                          public_bind="100.1.2.3:8765", share_root=lambda: share.paths.root,
                          folder_ops={"add": lambda p, lv: add_folder(share.policy, p, lv),
                                      "remove": lambda p: remove_folder(share.policy, p),
                                      "main": lambda p: set_main_folder(share.policy, p),
                                      "share_root": lambda p: (add_folder(share.policy, p, "write"),
                                                               set_main_folder(share.policy, p)) and Path(p).resolve()},
                          roots=lambda: share.paths.roots())
    app = Starlette(routes=[Mount("/claude", app=ep.asgi)])
    async with running(ep):
        local = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 5000)),
                                  base_url="http://127.0.0.1:8770")
        remote = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("100.64.0.2", 5000)),
                                   base_url="http://127.0.0.1:8770")
        async with local, remote:
            yield mgr, local, remote


async def join(mgr: TaskManager, name: str, send=None, **hello) -> int:
    """A lender's agent connects and says hello (only then is it given tasks). Returns the link id."""
    async def drop(m):
        pass
    link = mgr.attach(name, send or drop)
    await mgr.on_agent_message(link, {"type": "hello", "agent_version": "0.2.0", "codex_version": "x",
                                      "auth_mode": "chatgpt", **hello})
    return link


async def call(client, name: str, args: dict) -> dict:
    r = await client.post("/claude/mcp", headers=ACCEPT, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
    assert r.status_code == 200, r.text
    return r.json()["result"]


async def test_final_review_claude_refuses_data_parent(env, share):
    _, local, _ = env
    parent = share.data_dir.parent.as_posix()
    for name, args in [("set_folder_level", {"path": parent, "level": "write"}),
                       ("set_policy", {"text": f'folders: [{{path: "{parent}", level: write}}]'})]:
        result = await call(local, name, args)
        assert result.get("isError") and "データフォルダ" in result["content"][0]["text"]


def test_final_review_guide_explains_multiple_folders_and_legacy_alias():
    from codex_bridge.host.claude_mcp import GUIDE, INSTRUCTIONS
    for term in ("folders", "相対パス", "絶対パス", "add_shared_folder", "remove_shared_folder",
                 "set_main_folder", "set_share_root", "write", "旧名", "protocol", "v0.1", "メイン"):
        assert term in GUIDE
    assert "folders" in INSTRUCTIONS


async def test_tool_names(env):
    mgr, local, _ = env
    r = await local.post("/claude/mcp", headers=ACCEPT, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert {t["name"] for t in r.json()["result"]["tools"]} == {
        "send_task", "set_task_exec", "send_followup", "get_status", "get_result", "cancel_task", "get_usage",
        "undo_task", "list_sessions", "read_session", "delete_session",
        "list_approvals", "decide_approval", "get_policy", "set_policy", "set_folder_level", "set_exec_mode",
        "set_paused",
        "set_log_visibility", "list_tasks", "issue_invite", "revoke_invite", "list_invites", "get_overview",
        "set_share_root", "get_guide", "add_shared_folder", "remove_shared_folder", "set_main_folder",
        "list_lenders"}


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
    v2_text = f'folders:\n  - {{path: "{share.root.as_posix()}", level: write}}\nexec: {{mode: ask}}\n'
    text_of(await call(local, "set_policy", {"text": v2_text}))
    assert share.policy.current().level_for("x.txt") == "write" and share.policy.current().exec_mode == "ask"


async def test_claude_can_switch_exec_mode_pause_and_visibility(env, share):
    mgr, local, _ = env
    share.tokens.issue("alice")
    text_of(await call(local, "set_exec_mode", {"mode": "off", "lender": "alice"}))
    assert share.policy.current().exec_mode_for("alice") == "off"
    assert (await call(local, "set_exec_mode", {"mode": "weird", "lender": "alice"}))["isError"]
    text_of(await call(local, "set_paused", {"paused": True}))
    assert share.policy.current().paused is True
    text_of(await call(local, "set_log_visibility", {"value": "all", "lender": "alice"}))
    assert mgr.log_visibility_for("alice") == "all" and mgr.log_visibility_for("bob") == "summary"


async def test_mcp_set_exec_mode_needs_lender(env, share):
    _, local, _ = env
    result = await call(local, "set_exec_mode", {"mode": "off"})
    assert result["isError"] and "lender を指定してください" in result["content"][0]["text"]
    assert share.policy.current().exec_mode == "allowlist"


async def test_mcp_set_log_visibility_needs_lender(env):
    mgr, local, _ = env
    result = await call(local, "set_log_visibility", {"value": "all"})
    assert result["isError"] and "lender を指定してください" in result["content"][0]["text"]
    assert mgr.settings["log_visibility"] == "summary"


async def test_mcp_has_no_set_thresholds(env):
    _, local, _ = env
    r = await local.post("/claude/mcp", headers=ACCEPT, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert "set_thresholds" not in {t["name"] for t in r.json()["result"]["tools"]}
    assert (await call(local, "set_thresholds", {"five_hour": 50}))["isError"]


async def test_mcp_issue_invite_sets_ask_only_without_a_mode(env, share):
    _, local, _ = env
    text_of(await call(local, "issue_invite", {"name": "alice"}))
    assert share.policy.current().lender_exec_modes == {"alice": "ask"}
    share.policy.set_lender_exec_mode("alice", "all")
    text_of(await call(local, "issue_invite", {"name": "alice"}))
    assert share.policy.current().lender_exec_modes == {"alice": "all"}


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
    await join(mgr, "friend", send)
    res = await call(local, "send_task", {"prompt": "x"})
    tid = json.loads(res["content"][0]["text"])["task_id"]
    assert sent[-1]["type"] == "task_start" and sent[-1]["task_id"] == tid


async def test_send_task_origin_claude_by_default(env):
    mgr, local, _ = env
    await join(mgr, "friend")
    result = text_of(await call(local, "send_task", {"prompt": "x"}))
    rec = mgr.tasks[result["task_id"]]
    assert rec.origin == "claude" and rec.session is None
    assert result == {"task_id": rec.task_id, "lender": "friend", "model": None, "effort": None,
                      "exec_mode": None, "deny": [], "ask": [], "effective_exec_mode": "allowlist",
                      "note": claude_mcp.NOTIFY_NOTE}


async def test_task_exec_mcp_inheritance_and_persistence(env, share):
    mgr, local, _ = env
    share.set_policy(lender_exec_modes={"friend": "ask"})
    link = await join(mgr, "friend", capabilities={"resumable": True})
    fields = {"exec_mode": "all", "deny": ["git push *"], "ask": ["git commit *"]}
    reply = text_of(await call(local, "send_task", {"prompt": "first", **fields}))
    assert all(reply[k] == v for k, v in fields.items())
    assert reply["effective_exec_mode"] == "ask"
    task_id = reply["task_id"]
    task = {"task_id": task_id}
    for name in ("get_status", "get_result"):
        info = text_of(await call(local, name, task))
        assert all(info[k] == v for k, v in fields.items())
        assert info["effective_exec_mode"] == "ask"

    updated = text_of(await call(local, "set_task_exec", {**task, "ask": []}))
    assert updated == {**task, **fields, "ask": [], "effective_exec_mode": "ask"}
    share.policy.set_lender_exec_mode("friend", "all")
    assert text_of(await call(local, "get_status", task))["effective_exec_mode"] == "all"
    restored = TaskManager(share.data_dir, touched_fn=share.snapshots.touched, policy_fn=share.policy.current)
    assert restored.exec_settings(task_id) == {**fields, "ask": [], "effective_exec_mode": "all"}
    await mgr.on_agent_message(link, {"type": "task_state", **task, "thread_id": "th", "state": "completed"})
    refused = await call(local, "set_task_exec", {**task, "exec_mode": "off"})
    assert refused["isError"] and "作業中" in refused["content"][0]["text"]
    continued = text_of(await call(local, "send_task", {"prompt": "second", "continue_task_id": task_id}))
    assert continued["exec_mode"] == "all" and continued["ask"] == [] and continued["deny"] == fields["deny"]
    cleared = text_of(await call(local, "set_task_exec", {"task_id": continued["task_id"], "exec_mode": "inherit"}))
    assert cleared["exec_mode"] is None and cleared["effective_exec_mode"] == "all"


async def test_send_task_origin_codex_by_query(env):
    mgr, local, _ = env
    await join(mgr, "friend")
    response = await local.post("/claude/mcp?client=codex", headers=ACCEPT, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "send_task", "arguments": {"prompt": "x"}}})
    assert response.status_code == 200, response.text
    result = text_of(response.json()["result"])
    rec = mgr.tasks[result["task_id"]]
    assert rec.origin == "codex" and rec.session is None


@pytest.mark.parametrize("spec, model, effort", [({"model": "m1", "effort": "high"}, "m1", "high"),
                                                ({"model": "m1"}, "m1", None),
                                                ({"effort": "high"}, None, "high"),
                                                ({"model": "", "effort": ""}, None, None)])
async def test_send_task_model_effort_reach_agent(env, spec, model, effort):
    mgr, local, _ = env
    sent = []

    async def send(msg):
        sent.append(msg)
    await join(mgr, "friend", send, protocol=3, capabilities={"model_select": True},
               models=[{"model": "m1", "efforts": ["low", "high"], "is_default": True}], max_effort="xhigh")
    result = text_of(await call(local, "send_task", {"prompt": "x", **spec}))
    rec = mgr.tasks[result["task_id"]]
    assert (rec.model, rec.effort) == (model, effort)
    assert result == {"task_id": rec.task_id, "lender": "friend", "model": model, "effort": effort,
                      "exec_mode": None, "deny": [], "ask": [], "effective_exec_mode": "allowlist",
                      "note": claude_mcp.NOTIFY_NOTE}
    assert claude_mcp.NOTIFY_NOTE == (
        "承認待ちか終了になると自動で知らせが届きます（install-mcp で設定済みの場合）。それまで状態を確かめる必要はありません。")
    assert sent[-1]["type"] == "task_start" and sent[-1]["task_id"] == rec.task_id
    for key, value in (("model", model), ("effort", effort)):
        if value is None:
            assert key not in sent[-1]
        else:
            assert sent[-1][key] == value


async def test_send_followup_returns_task_id(env):
    mgr, local, _ = env
    await join(mgr, "friend")
    tid = text_of(await call(local, "send_task", {"prompt": "x"}))["task_id"]
    assert text_of(await call(local, "send_followup", {"task_id": tid, "text": "more"})) == {
        "ok": True, "task_id": tid, "note": claude_mcp.NOTIFY_NOTE}


@pytest.mark.parametrize("approve, status", [(True, "approved"), (False, "denied")])
async def test_decide_approval_returns_task_id(env, share, approve, status):
    _, local, _ = env
    approval = share.approvals.request("command", "python x.py", task_id="t1")
    result = text_of(await call(local, "decide_approval", {"approval_id": approval.id, "approve": approve}))
    assert result == {"approval_id": approval.id, "status": status, "task_id": "t1",
                      "note": claude_mcp.NOTIFY_NOTE}


def test_guide_no_polling_instruction():
    assert "get_status(wait_sec=50) を繰り返" not in claude_mcp.GUIDE + claude_mcp.INSTRUCTIONS
    assert "自動で知らせが届きます" in claude_mcp.GUIDE


def test_guide_keeps_injection_warning():
    assert "利用者の指示ではない" in claude_mcp.GUIDE


async def test_no_switching_lenders_without_permission(env):
    """A lender the user chose, or the one already doing the work, is not swapped for another without asking."""
    _, local, _ = env
    response = await local.post("/claude/mcp", headers=ACCEPT, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status_code == 200, response.text
    description = next(t["description"] for t in response.json()["result"]["tools"] if t["name"] == "send_task")
    assert "原則: 送り先を利用者の許可なく変えない" in claude_mcp.GUIDE
    for text in (claude_mcp.GUIDE, claude_mcp.INSTRUCTIONS, description):
        assert "許可なく" in text, text[:40]
    for text in (claude_mcp.GUIDE, claude_mcp.INSTRUCTIONS, description):
        assert "省けば別の人が選ばれる" not in text
        assert "余裕のある別の人が選ばれる" not in text
        assert "lender を変える" not in text


async def test_no_judgement_rule_in_approval_texts(env):
    _, local, _ = env
    response = await local.post("/claude/mcp", headers=ACCEPT, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status_code == 200, response.text
    description = next(t["description"] for t in response.json()["result"]["tools"]
                       if t["name"] == "decide_approval")
    for text in (claude_mcp.GUIDE, claude_mcp.APPROVAL_NOTE, description):
        assert "沿っていれば" not in text
        assert "必ず確かめてください" not in text


@pytest.mark.parametrize("session", ["s1", "", None, 7, [], {}])
async def test_caller_session_recorded(env, session):
    mgr, local, _ = env
    await join(mgr, "friend")
    response = await local.post("/claude/mcp?client=codex", headers=ACCEPT, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "send_task", "arguments": {"prompt": "x"},
                   "_meta": {"sessionId": session, "threadId": "s1",
                             "x-codex-turn-metadata": {"session_id": "s1"}}}})
    assert response.status_code == 200, response.text
    rec = mgr.tasks[text_of(response.json()["result"])["task_id"]]
    assert rec.origin == "codex"
    assert rec.session == ("s1" if session == "s1" else None)


def window(percent: int) -> dict:
    return {"used_percent": percent, "resets_at": int(time.time()) + 3600}


async def two_lenders(mgr) -> tuple[dict, dict]:
    """alice (little room left, sessions can be continued) and bob (lots of room, disposable sessions)."""
    sent = {"alice": [], "bob": []}
    links = {}
    for name, used, caps in (("alice", 60, {"resumable": True, "pre_approval": True}),
                             ("bob", 10, {"resumable": False, "pre_approval": False})):
        async def send(m, name=name):
            sent[name].append(m)
        links[name] = await join(mgr, name, send, protocol=2, capabilities=caps,
                                 limits={"five_hour": 70, "weekly": None, "force": False})
        await mgr.on_agent_message(links[name], {"type": "usage", "five_hour": window(used), "weekly": None, "ts": 0})
    return sent, links


def starts(sent: list[dict]) -> list[str]:
    return [m["task_id"] for m in sent if m["type"] == "task_start"]


async def test_list_lenders(env):
    mgr, local, _ = env
    assert text_of(await call(local, "list_lenders", {})) == []
    await two_lenders(mgr)
    tid = text_of(await call(local, "send_task", {"prompt": "x", "lender": "alice"}))["task_id"]
    rows = {r["name"]: r for r in text_of(await call(local, "list_lenders", {}))}
    assert list(rows) == ["alice", "bob"]
    a, b = rows["alice"], rows["bob"]
    assert a["connected"] is True and a["ready"] is True and a["current_task_id"] == tid
    assert a["capabilities"] == {"resumable": True, "pre_approval": True, "model_select": False, "max_parallel": 1}
    assert a["limits"] == {"five_hour": 70, "weekly": None, "force": False}
    assert a["usage"]["five_hour"]["used_percent"] == 60 and a["usage"]["blocked_until"] is None
    assert (a["agent_version"], a["codex_version"], a["auth_mode"]) == ("0.2.0", "x", "chatgpt")
    assert b["current_task_id"] is None and b["capabilities"]["resumable"] is False
    assert b["usage"]["five_hour"]["used_percent"] == 10


async def test_list_lenders_includes_invited_names_that_have_not_connected(env, share):
    mgr, local, _ = env
    for name in ("carol", "alice"):
        text_of(await call(local, "issue_invite", {"name": name}))
    await two_lenders(mgr)
    rows = text_of(await call(local, "list_lenders", {}))
    assert [r["name"] for r in rows] == ["alice", "bob", "carol"]
    assert rows[2]["connected"] is False and rows[2]["ready"] is False and rows[2]["usage"]["five_hour"] is None
    res = await call(local, "send_task", {"prompt": "x", "lender": "carol"})
    assert res["isError"] and "接続されていません" in res["content"][0]["text"]


async def test_send_task_goes_to_the_named_lender(env):
    mgr, local, _ = env
    sent, _links = await two_lenders(mgr)
    r = text_of(await call(local, "send_task", {"prompt": "one", "lender": "alice"}))  # bob has more room
    assert r["lender"] == "alice" and starts(sent["alice"]) == [r["task_id"]] and not starts(sent["bob"])
    res = await call(local, "send_task", {"prompt": "two", "lender": "alice"})
    assert res["isError"] and "上限" in res["content"][0]["text"]
    res = await call(local, "send_task", {"prompt": "two", "lender": "carol"})
    assert res["isError"] and "接続されていません" in res["content"][0]["text"]
    assert not starts(sent["bob"]) and len(mgr.tasks) == 1


async def test_send_task_without_a_lender_picks_the_one_with_most_room(env):
    mgr, local, _ = env
    sent, _links = await two_lenders(mgr)
    r = text_of(await call(local, "send_task", {"prompt": "one"}))  # the answer says who got it
    assert r["lender"] == "bob" and starts(sent["bob"]) == [r["task_id"]] and not starts(sent["alice"])


async def test_continuation_can_only_go_back_to_the_same_lender(env):
    mgr, local, _ = env
    sent, links = await two_lenders(mgr)
    done = {}
    for name in ("alice", "bob"):
        done[name] = text_of(await call(local, "send_task", {"prompt": "one", "lender": name}))["task_id"]
        await mgr.on_agent_message(links[name], {"type": "task_state", "seq": 1, "task_id": done[name],
                                                 "state": "completed", "thread_id": f"th-{name}", "report": "ok"})
    # alice holds the session: it goes back to her, whoever has more room
    r = text_of(await call(local, "send_task", {"prompt": "more", "continue_task_id": done["alice"]}))
    assert r["lender"] == "alice" and sent["alice"][-1]["continue_thread_id"] == "th-alice"
    # naming someone else, or continuing at a lender with disposable sessions, is refused with advice
    for args in ({"continue_task_id": done["alice"], "lender": "bob"}, {"continue_task_id": done["bob"]}):
        res = await call(local, "send_task", {"prompt": "more", **args})
        assert res["isError"] and "前回の報告を添えて新しく依頼してください" in res["content"][0]["text"]
    assert len(starts(sent["bob"])) == 1 and len(mgr.tasks) == 3


async def test_get_usage_is_per_lender(env):
    mgr, local, _ = env
    await two_lenders(mgr)
    u = text_of(await call(local, "get_usage", {}))
    assert {n: v["five_hour"]["used_percent"] for n, v in u["lenders"].items()} == {"alice": 60, "bob": 10}
    assert u["lenders"]["alice"]["blocked_until"] is None and "thresholds" not in u
    mgr._lenders["alice"].limits = {"five_hour": 50}  # only the lender's own limit blocks
    u = text_of(await call(local, "get_usage", {}))
    assert u["lenders"]["alice"]["blocked_until"] and u["lenders"]["bob"]["blocked_until"] is None


async def test_undo_refused_for_every_task_in_progress(env, share):
    # Two lenders work at once: neither task may be undone while it runs.
    mgr, local, _ = env
    await join(mgr, "alice")
    await join(mgr, "bob")
    tids = [text_of(await call(local, "send_task", {"prompt": p}))["task_id"] for p in ("one", "two")]
    assert {mgr.tasks[t].lender for t in tids} == {"alice", "bob"}
    for tid in tids:
        res = await call(local, "undo_task", {"task_id": tid})
        assert res["isError"] and "作業中" in res["content"][0]["text"]


async def test_session_tools_take_a_lender(env):
    import asyncio
    mgr, local, _ = env
    sent = {"alice": [], "bob": []}
    links = {}
    for name in sent:
        async def send(m, name=name):
            sent[name].append(m)
            if m["type"] == "session_list":
                asyncio.get_running_loop().create_task(mgr.on_agent_message(
                    links[name], {"type": "reply", "req_id": m["req_id"], "ok": True, "data": [name]}))
        links[name] = mgr.attach(name, send)
    res = await call(local, "list_sessions", {})
    assert res["isError"] and "lender" in res["content"][0]["text"]
    assert text_of(await call(local, "list_sessions", {"lender": "bob"})) == ["bob"]
    assert not sent["alice"]


async def test_undo_task(env, share):
    mgr, local, _ = env
    from codex_bridge.host.tasks import TaskRecord
    mgr.tasks["t5"] = TaskRecord(task_id="t5", prompt="p", created=0, state="completed")
    share.task("t5")
    share.write.write_file("out/u.txt", "u")
    res = await call(local, "undo_task", {"task_id": "t5"})
    assert json.loads(res["content"][0]["text"])["restored"] == ["out/u.txt"]
    assert not (share.root / "out/u.txt").exists()


async def test_undo_task_reports_removed_share(env, share):
    from codex_bridge.host.policy_form import remove_folder
    from codex_bridge.host.tasks import TaskRecord

    mgr, local, _ = env
    mgr.tasks["t5"] = TaskRecord(task_id="t5", prompt="p", created=0, state="completed")
    share.add_folder(share.outside, "write")
    share.task("t5")
    target = share.outside / "new.txt"
    share.write.write_file(target.as_posix(), "changed")
    remove_folder(share.policy, share.outside.as_posix())

    result = await call(local, "undo_task", {"task_id": "t5"})
    assert result["isError"]
    assert result["content"][0]["text"] == f"書き込めない場所のファイルは元に戻せません（共有を外した・読むだけや見せないに変えた）: {target}"
    assert target.read_text(encoding="utf-8") == "changed"


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
    link = await join(mgr, "friend", send)
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
    await join(mgr, "friend")
    tid = json.loads((await call(local, "send_task", {"prompt": "x"}))["content"][0]["text"])["task_id"]
    asyncio.get_running_loop().call_later(0.5, lambda: share.approvals.request("command", "python x.py", tid))
    t0 = time.monotonic()
    s = json.loads((await call(local, "get_status", {"task_id": tid, "wait_sec": 10}))["content"][0]["text"])
    assert s["pending_approvals"] and time.monotonic() - t0 < 5


async def test_claude_set_folder_level_and_form(env, share):
    _, local, _ = env
    text_of(await call(local, "set_folder_level", {"path": "docs", "level": "write"}))
    assert share.policy.current().level_for("docs/a.md") == "write"
    form = text_of(await call(local, "get_policy", {}))["form"]
    assert {"path": (share.root / "docs").as_posix(), "level": "write"} in form["folders"]
    assert (await call(local, "set_folder_level", {"path": "docs", "level": "everything"}))["isError"]


async def test_claude_can_change_share_root(env, share, tmp_path):
    _, local, _ = env
    new = tmp_path / "elsewhere"
    new.mkdir()
    r = text_of(await call(local, "set_share_root", {"path": str(new)}))
    assert r["share_root"] == str(new.resolve()) and share.paths.root == new.resolve()


async def test_get_guide_explains_the_workflow(env):
    _, local, _ = env
    g = text_of(await call(local, "get_guide", {}))["guide"]
    for word in ("get_overview", "send_task", "wait_sec", "decide_approval", "get_result", "continue_task_id"):
        assert word in g


async def test_get_guide_covers_several_lenders(env):
    _, local, _ = env
    g = text_of(await call(local, "get_guide", {}))["guide"]
    for word in ("list_lenders", "lender", "Codex を貸している人", "余裕", "同じ人", "使い捨て",
                 "前回の報告を添えて", "blocked_until", "ready"):
        assert word in g, word


async def test_server_instructions_tell_agents_how_to_start(env):
    _, local, _ = env
    r = await local.post("/claude/mcp", headers=ACCEPT, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}})
    ins = r.json()["result"]["instructions"]
    assert "codex-bridge" in ins and "get_guide" in ins and "send_task" in ins


async def test_claude_adds_removes_and_switches_folders(env, share, tmp_path):
    _, local, _ = env
    extra = tmp_path / "extra-folder"
    extra.mkdir()
    (extra / "note.txt").write_text("hi", encoding="utf-8")
    text_of(await call(local, "add_shared_folder", {"path": str(extra), "level": "read"}))
    assert share.policy.current().level_for_abs(extra / "note.txt") == "read"
    o = text_of(await call(local, "get_overview", {}))
    assert extra.as_posix() in {f["path"] for f in o["folders"]}
    text_of(await call(local, "set_main_folder", {"path": str(extra)}))
    assert share.paths.root == extra.resolve()
    text_of(await call(local, "remove_shared_folder", {"path": str(extra)}))
    assert share.policy.current().level_for_abs(extra / "note.txt") is None


async def test_set_exec_mode_for_lender_and_inherit(env, share):
    from codex_bridge.host.policy import EXEC_MODES
    _, local, _ = env
    share.tokens.issue("alice")
    share.tokens.issue("bob")
    global_mode = share.policy.current().exec_mode
    for mode in (*EXEC_MODES, "inherit"):
        result = text_of(await call(local, "set_exec_mode", {"mode": mode, "lender": "alice"}))
        expected = None if mode == "inherit" else mode
        assert result == {"lender": "alice", "exec_mode": expected}
        assert share.policy.current().exec_mode == global_mode
        rows = text_of(await call(local, "list_lenders", {}))
        assert {r["name"]: r["exec_mode"] for r in rows} == {"alice": expected, "bob": None}
    for args, message in (({"mode": "off", "lender": "missing"}, "その名前の招待コードがありません: missing"),
                          ({"mode": "bad", "lender": "alice"}, "mode"),
                          ({"mode": "inherit"}, "lender を指定してください")):
        result = await call(local, "set_exec_mode", args)
        assert result["isError"] and message in result["content"][0]["text"]
    assert share.policy.current().lender_exec_modes == {}


async def test_get_policy_lists_lender_modes(env, share):
    _, local, _ = env
    modes = {"alice": "off", "bob": "all"}
    share.policy.update(lender_exec_modes=modes)
    result = text_of(await call(local, "get_policy", {}))
    assert result["lender_exec_modes"] == modes
    assert result["form"]["lender_exec_modes"] == modes
    assert text_of(await call(local, "get_overview", {}))["lender_exec_modes"] == modes
    text_of(await call(local, "set_folder_level", {"path": "docs", "level": "write"}))
    assert text_of(await call(local, "get_policy", {}))["lender_exec_modes"] == modes


async def test_overview_lists_every_active_task_and_keeps_first_for_compatibility(env):
    mgr, local, _ = env
    await join(mgr, "friend", protocol=5, capabilities={"max_parallel": 3})
    ids = [await mgr.start("first"), await mgr.start("second")]
    overview = text_of(await call(local, "get_overview", {}))
    assert overview["active_task_ids"] == ids and overview["current_task"] == ids[0]
