import hashlib
import json
from contextlib import asynccontextmanager

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Mount

from codex_bridge.host.codex_mcp import HostServices, build_codex_mcp
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
    tokens = {hashlib.sha256(TOKEN.encode()).hexdigest()}
    services = HostServices(paths=share.paths, policy=share.policy, read=share.read, search=share.search,
                            write=share.write, runner=share.runner, approvals=share.approvals,
                            snapshots=share.snapshots, audit=share.audit, task_id_fn=lambda: share.task_id)
    ep = build_codex_mcp(services, lambda: tokens)
    app = Starlette(routes=[Mount("/codex", app=ep.asgi)])
    async with running(ep):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://100.64.0.1:8765") as c:
            c.tokens = tokens
            yield c


async def rpc(client, method: str, params: dict | None = None, rid: int = 2) -> dict:
    r = await client.post("/codex/mcp", headers=auth(TOKEN),
                          json={"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
    assert r.status_code == 200, r.text
    return r.json()


async def call(client, name: str, args: dict) -> dict:
    return (await rpc(client, "tools/call", {"name": name, "arguments": args}))["result"]


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
