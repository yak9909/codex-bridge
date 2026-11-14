import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from mcp_types import CLIENT_CAPABILITIES_META_KEY, PROTOCOL_VERSION_META_KEY
from mcp_types.version import LATEST_MODERN_VERSION
from starlette.applications import Starlette
from starlette.routing import Mount

from codex_bridge.host.app import build_host, create_public_app
from codex_bridge.host.codex_mcp import HostServices, build_codex_mcp
from codex_bridge.host.config import HostConfig
from codex_bridge.host.mcp_util import BearerAuth
from conftest import RG_PATH
from mcp_helpers import running

TOKEN = "tok-123"
ACCEPT = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "test", "version": "0"}}}


def auth(tok: str) -> dict:
    return {**ACCEPT, "Authorization": f"Bearer {tok}"}


@pytest.fixture
async def mcp(share):
    tokens = {TOKEN: "friend"}  # token -> invite name
    services = HostServices(paths=share.paths, policy=share.policy, read=share.read, search=share.search,
                            write=share.write, runner=share.runner, approvals=share.approvals,
                            snapshots=share.snapshots, audit=share.audit, task_id_fn=lambda: share.task_id)
    ep = build_codex_mcp(services, tokens.get)
    app = Starlette(routes=[Mount("/codex", app=ep.asgi)])
    async with running(ep):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://100.64.0.1:8765") as c:
            c.tokens = tokens
            yield c


async def rpc(client, method: str, params: dict | None = None, rid: int = 2, token: str = TOKEN) -> dict:
    r = await client.post("/codex/mcp", headers=auth(token),
                          json={"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
    assert r.status_code == 200, r.text
    return r.json()


async def call(client, name: str, args: dict, token: str = TOKEN) -> dict:
    return (await rpc(client, "tools/call", {"name": name, "arguments": args}, token=token))["result"]


async def test_requires_bearer_token(mcp):
    assert (await mcp.post("/codex/mcp", json=INIT, headers=ACCEPT)).status_code == 401
    assert (await mcp.post("/codex/mcp", json=INIT, headers=auth("wrong"))).status_code == 401
    assert (await mcp.post("/codex/mcp", json=INIT, headers=auth(TOKEN))).status_code == 200


async def test_revoked_token_rejected_immediately(mcp):
    mcp.tokens.clear()
    assert (await mcp.post("/codex/mcp", json=INIT, headers=auth(TOKEN))).status_code == 401


async def test_tool_list_matches_spec(mcp):
    tools = (await rpc(mcp, "tools/list"))["result"]["tools"]
    assert {t["name"] for t in tools} == {
        "list_dir", "find_files", "grep", "stat", "read_file", "write_file", "edit_file",
        "apply_patch", "move_path", "delete_path", "run_command", "get_command_result"}
    assert all("相対" in t["description"] or t["name"] == "get_command_result" for t in tools)


async def test_tool_success_returns_json_text(mcp):
    res = await call(mcp, "read_file", {"path": "raw/a.txt"})
    assert not res.get("isError") and json.loads(res["content"][0]["text"])["text"].startswith("line1")


async def test_tool_error_is_mcp_error_without_paths(mcp, share):
    res = await call(mcp, "read_file", {"path": "raw/secret/k.txt"})
    assert res["isError"] and res["content"][0]["text"] == "not found: raw/secret/k.txt"


async def test_every_call_is_audited(mcp, share):
    await call(mcp, "list_dir", {"path": "raw"})
    last = json.loads((share.data_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert last["kind"] == "tool_call" and last["tool"] == "list_dir" and last["ok"] is True


async def test_write_content_not_dumped_in_audit(mcp, share):
    await call(mcp, "write_file", {"path": "out/big.txt", "content": "z" * 10000})
    log = (share.data_dir / "audit.jsonl").read_text(encoding="utf-8")
    assert "z" * 1000 not in log


async def test_apply_patch_and_run_command_via_mcp(mcp, share):
    res = await call(mcp, "apply_patch", {"patch": "*** Begin Patch\n*** Add File: out/p.txt\n+p\n*** End Patch\n"})
    assert json.loads(res["content"][0]["text"])["changed"] == ["out/p.txt"]
    share.set_policy(exec_mode="all")
    import sys
    res = await call(mcp, "run_command", {"command": f'"{sys.executable}" -c "print(5)"'})
    assert json.loads(res["content"][0]["text"])["stdout"].strip() == "5"


async def test_paused_returns_error(mcp, share):
    share.set_policy(paused=True)
    res = await call(mcp, "list_dir", {"path": "."})
    assert res["isError"] and "停止中" in res["content"][0]["text"]


# ---- several lenders: a call belongs to the task of the lender whose token it carries ----
LENDERS = ("alice", "bob")


@pytest.fixture
async def two(share):
    """The whole host, with alice and bob connected and each working on a task of their own."""
    host = build_host(HostConfig(share_root=share.root, data_dir=share.data_dir, public_bind="100.64.0.1:8765",
                                 local_port=8770, rg_path=RG_PATH))

    async def send(msg: dict) -> None:
        pass

    token, link, task = {}, {}, {}
    for who in LENDERS:
        token[who] = host.tokens.issue(who)
        link[who] = host.mgr.attach(who, send)
        task[who] = await host.mgr.start(f"work for {who}", lender=who)
    async with running(host.codex_ep):
        transport = httpx.ASGITransport(app=create_public_app(host))
        async with httpx.AsyncClient(transport=transport, base_url="http://100.64.0.1:8765") as c:
            yield SimpleNamespace(host=host, client=c, token=token, link=link, task=task, root=share.root)


async def call_as(two, who: str, name: str, args: dict) -> dict:
    res = await call(two.client, name, args, token=two.token[who])
    assert not res.get("isError"), res
    return json.loads(res["content"][0]["text"])


async def pending(two, count: int) -> list:
    for _ in range(250):
        if len(items := two.host.approvals.pending()) == count:
            return items
        await asyncio.sleep(0.02)
    raise AssertionError(f"expected {count} pending approvals, got {two.host.approvals.pending()}")


async def test_concurrent_writes_are_saved_under_each_lenders_own_task(two):
    calls = [(who, f"out/{who}{i}.txt") for i in range(5) for who in LENDERS]  # alice, bob, alice, bob, ...
    await asyncio.gather(*(call_as(two, who, "write_file", {"path": path, "content": who}) for who, path in calls))
    for who in LENDERS:
        assert sorted(two.host.snapshots.touched(two.task[who])) == [f"out/{who}{i}.txt" for i in range(5)]
    assert two.host.snapshots.touched(None) == []
    # the records name the same task
    owner = {path: two.task[who] for who, path in calls}
    log = [json.loads(line) for line in two.host.audit.path.read_text(encoding="utf-8").splitlines()]
    changes = [r for r in log if r["kind"] == "file_change"]
    tool_calls = [r for r in log if r["kind"] == "tool_call"]
    assert len(changes) == len(tool_calls) == len(calls)
    assert all(r["task_id"] == owner[r["path"]] for r in changes)
    assert all(r["task_id"] == owner[r["args"]["path"]] for r in tool_calls)


async def test_delete_approval_and_the_delete_after_it_stay_with_the_caller(two):
    for who in LENDERS:
        (two.root / "out" / f"{who}.txt").write_text(who, encoding="utf-8")
    deletes = [asyncio.create_task(call_as(two, who, "delete_path", {"path": f"out/{who}.txt"})) for who in LENDERS]
    waiting = await pending(two, 2)
    assert {a.summary: a.task_id for a in waiting} == {f"削除: out/{who}.txt": two.task[who] for who in LENDERS}
    for a in waiting:
        two.host.approvals.decide(a.id, True)
    assert [r["status"] for r in await asyncio.gather(*deletes)] == ["done", "done"]
    for who in LENDERS:  # the original is saved only after the wait, and still under the caller's task
        assert two.host.snapshots.touched(two.task[who]) == [f"out/{who}.txt"]


async def test_command_approvals_name_the_callers_task(two):
    runs = [asyncio.create_task(call_as(two, who, "run_command", {"command": f"echo {who}"})) for who in LENDERS]
    waiting = await pending(two, 2)
    expected = {f"echo {who}": two.task[who] for who in LENDERS}
    assert {a.summary: a.task_id for a in waiting} == expected
    for a in waiting:
        two.host.approvals.decide(a.id, False)
    assert [r["status"] for r in await asyncio.gather(*runs)] == ["denied", "denied"]
    assert {j.command: j.task_id for j in two.host.services.runner.jobs.values()} == expected


async def test_command_result_is_visible_only_to_the_lender_that_started_it(two):
    two.host.policy.update(exec_mode="all")
    job_id = (await call_as(two, "alice", "run_command", {"command": "echo alice"}))["job_id"]
    # to bob, alice's job looks exactly like a job that does not exist
    answers = {jid: await call(two.client, "get_command_result", {"job_id": jid}, token=two.token["bob"])
               for jid in (job_id, "j999")}
    for jid, res in answers.items():
        assert res["isError"] and res["content"][0]["text"] == f"ジョブが見つかりません: {jid}"
    mine = await call_as(two, "alice", "get_command_result", {"job_id": job_id})
    assert mine["status"] == "done" and mine["stdout"].strip() == "alice"


async def test_lender_without_a_task_does_not_write_into_another_lenders_task(two):
    await two.host.mgr.on_agent_message(
        two.link["bob"], {"type": "task_state", "task_id": two.task["bob"], "state": "completed"})
    await call_as(two, "bob", "write_file", {"path": "out/late.txt", "content": "x"})
    assert two.host.snapshots.touched(two.task["alice"]) == []
    assert two.host.snapshots.touched(None) == ["out/late.txt"]


async def test_calls_in_the_2026_envelope_are_tied_to_the_caller_too(two):
    """The MCP SDK serves the 2026-07-28 revision on a path of its own; the lender must be known there as well."""
    async def write(who: str) -> None:
        params = {"name": "write_file", "arguments": {"path": f"out/{who}.txt", "content": who},
                  "_meta": {PROTOCOL_VERSION_META_KEY: LATEST_MODERN_VERSION, CLIENT_CAPABILITIES_META_KEY: {}}}
        r = await two.client.post(
            "/codex/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": params},
            headers={**auth(two.token[who]), "MCP-Protocol-Version": LATEST_MODERN_VERSION,
                     "Mcp-Method": "tools/call", "Mcp-Name": "write_file"})
        assert r.status_code == 200 and not r.json()["result"].get("isError"), r.text

    await asyncio.gather(*(write(who) for who in LENDERS))
    for who in LENDERS:
        assert two.host.snapshots.touched(two.task[who]) == [f"out/{who}.txt"]


async def test_bearer_auth_puts_the_matched_name_in_the_request_state():
    seen = []

    async def inner(scope, receive, send) -> None:
        seen.append(scope["state"])

    shared = {"kept": 1}  # e.g. the server's lifespan state: requests must not write into it
    guard = BearerAuth(inner, {"tok-a": "alice", "tok-b": "bob"}.get)
    for tok in ("tok-a", "tok-b"):
        await guard({"type": "http", "headers": [(b"authorization", f"Bearer {tok}".encode())], "state": shared},
                    None, None)
    assert seen == [{"kept": 1, "lender": "alice"}, {"kept": 1, "lender": "bob"}]
    assert shared == {"kept": 1}
