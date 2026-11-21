import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
import stat
from types import SimpleNamespace

import httpx
import pytest
from mcp_types import CLIENT_CAPABILITIES_META_KEY, PROTOCOL_VERSION_META_KEY
from mcp_types.version import LATEST_MODERN_VERSION
from starlette.applications import Starlette
from starlette.routing import Mount

from codex_bridge.host.app import build_host, create_public_app
from codex_bridge.host.codex_mcp import HostServices, build_codex_mcp, current_lender
from codex_bridge.host.config import HostConfig
from codex_bridge.host.mcp_util import BearerAuth
from conftest import ORIGINAL_A, RG_PATH
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
    share.task("mcp-task")
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


async def test_final_review_mcp_whitespace_list_is_omitted_path(mcp):
    blank = await call(mcp, "list_dir", {"path": " \t "})
    omitted = await call(mcp, "list_dir", {})
    assert blank == omitted


async def test_tool_list_matches_spec(mcp):
    tools = (await rpc(mcp, "tools/list"))["result"]["tools"]
    assert {t["name"] for t in tools} == {
        "list_dir", "find_files", "grep", "stat", "read_file", "write_file", "edit_file",
        "apply_patch", "move_path", "delete_path", "run_command", "get_command_result"}
    assert all("相対" in t["description"] or t["name"] == "get_command_result" for t in tools)


async def test_instructions_explain_shared_folder_paths_and_discovery(mcp):
    instructions = (await mcp.post("/codex/mcp", json=INIT, headers=auth(TOKEN))).json()["result"]["instructions"]
    for term in ("複数", "メイン", "相対パス", "ほかの共有フォルダ", "絶対パス",
                 'list_dir("/")', "省略", "find_files", "grep", "すべて", "結果"):
        assert term in instructions


@pytest.mark.parametrize("name", ["list_dir", "find_files", "grep", "stat", "read_file", "write_file",
                                 "edit_file", "apply_patch", "move_path", "delete_path", "run_command"])
async def test_path_tool_descriptions_explain_absolute_paths_in_other_folders(mcp, name):
    tools = (await rpc(mcp, "tools/list"))["result"]["tools"]
    description = next(t["description"] for t in tools if t["name"] == name)
    for term in ("メイン", "相対パス", "ほかの共有フォルダ", "絶対パス"):
        assert term in description


@pytest.fixture
def shared_folders(share):
    share.add_folder(share.outside, "write")
    (share.outside / "extra.txt").write_text("needle in extra\n", encoding="utf-8", newline="\n")
    return share


@pytest.mark.parametrize("args", [{}, {"path": ""}, {"path": "/"}])
async def test_list_dir_entry_point_lists_shared_folders(mcp, shared_folders, args):
    res = await call(mcp, "list_dir", args)
    assert not res.get("isError"), res
    entries = json.loads(res["content"][0]["text"])
    folders = {e["path"]: e for e in entries}
    assert shared_folders.root.as_posix() in folders
    assert shared_folders.outside.as_posix() in folders
    assert folders[shared_folders.root.as_posix()]["main"] is True
    assert folders[shared_folders.outside.as_posix()]["main"] is False


async def test_list_dir_explicit_dot_lists_main_contents(mcp, shared_folders):
    res = await call(mcp, "list_dir", {"path": "."})
    assert not res.get("isError"), res
    entries = json.loads(res["content"][0]["text"])
    assert {e["name"] for e in entries} == {"raw", "docs", "out", "scripts"}


@pytest.mark.parametrize("name, pattern", [("find_files", "**/*.txt"), ("grep", "needle")])
@pytest.mark.parametrize("scope", [{}, {"path": ""}, {"path": "."}])
async def test_search_scope_covers_all_folders_unless_explicit_dot(mcp, shared_folders, name, pattern, scope):
    res = await call(mcp, name, {"pattern": pattern, **scope})
    assert not res.get("isError"), res
    result = json.loads(res["content"][0]["text"])
    paths = {m["path"] for m in result["matches"]} if name == "grep" else set(result)
    assert "raw/a.txt" in paths
    assert ((shared_folders.outside / "extra.txt").as_posix() in paths) == (scope.get("path") != ".")
    assert not any("secret" in p for p in paths)


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


@pytest.mark.parametrize("name", ["write_file", "edit_file", "delete_path", "move_path"])
@pytest.mark.parametrize("prior", [False, True])
async def test_permission_denied_is_reported_plainly(mcp, share, monkeypatch, name, prior):
    import codex_bridge.host.fs_write as writes
    target = share.root / "raw" / "a.txt"
    share.task("denied")
    if prior:
        share.write.write_file("raw/a.txt", "earlier successful write\n")
    original = target.read_bytes()
    before = share.snapshots.touched("denied")
    args = {"path": "raw/a.txt"}

    def denied(*unused, **kwargs):
        raise PermissionError(13, "Access is denied", str(target))

    if name in ("write_file", "edit_file"):
        monkeypatch.setattr(writes, "atomic_write_bytes", denied)
        args.update({"content": "changed"} if name == "write_file" else {"old": "line1" if not prior else "earlier", "new": "changed"})
    elif name == "move_path":
        monkeypatch.setattr(writes.shutil, "move", denied)
        args = {"src": "raw/a.txt", "dst": "out/moved.txt"}
    else:
        unlink = Path.unlink

        def deny_target(path, *args, **kwargs):
            if path == target:
                return denied()
            return unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", deny_target)
    result = await call(mcp, name, args)
    expected = "Windows がこの操作を断りました（管理者の権限が要る場所か、ほかのプログラムが使用中の可能性があります）: raw/a.txt"
    assert result.get("isError") and result["content"][0]["text"] == expected
    last = json.loads(share.audit.path.read_text(encoding="utf-8").splitlines()[-1])
    assert last["kind"] == "tool_call" and last["tool"] == name and last["ok"] is False
    assert last["error"] == expected and last["task_id"] == "denied"
    assert target.read_bytes() == original
    assert share.snapshots.touched("denied") == before
    assert not (share.root / "out" / "moved.txt").exists()
    monkeypatch.undo()
    if prior:
        assert share.snapshots.undo("denied") == ["raw/a.txt"]
        assert target.read_text(encoding="utf-8") == ORIGINAL_A


async def test_other_oserror_is_reported_and_audited(mcp, share, monkeypatch):
    def failed(*args):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("codex_bridge.host.fs_write.atomic_write_bytes", failed)
    result = await call(mcp, "write_file", {"path": "out/new.txt", "content": "x"})
    assert result.get("isError")
    assert result["content"][0]["text"] == "ファイルの操作に失敗しました: No space left on device"
    last = json.loads(share.audit.path.read_text(encoding="utf-8").splitlines()[-1])
    assert last["ok"] is False and last["tool"] == "write_file"
    assert share.snapshots.touched(share.task_id) == []


async def test_oserror_does_not_expose_internal_snapshot_path(mcp, share, monkeypatch):
    def failed(*args):
        raise OSError(28, "No space left on device", str(share.data_dir / "snapshots" / "private"))

    monkeypatch.setattr("codex_bridge.host.fs_write.atomic_write_bytes", failed)
    result = await call(mcp, "write_file", {"path": "out/new.txt", "content": "x"})
    assert result.get("isError")
    assert result["content"][0]["text"] == "ファイルの操作に失敗しました: No space left on device"
    assert str(share.data_dir) not in json.dumps(result)


async def test_move_permission_error_names_the_destination(mcp, share, monkeypatch):
    target = share.root / "out" / "moved.txt"

    def denied(*args):
        raise PermissionError(13, "Access is denied", str(target))

    monkeypatch.setattr("codex_bridge.host.fs_write.shutil.move", denied)
    result = await call(mcp, "move_path", {"src": "raw/a.txt", "dst": "out/moved.txt"})
    assert result.get("isError")
    assert result["content"][0]["text"].endswith(": out/moved.txt")
    assert share.snapshots.touched(share.task_id) == []


async def test_partial_delete_keeps_original_for_undo(mcp, share, monkeypatch):
    import codex_bridge.host.fs_write as writes
    folder = share.root / "out" / "partial"
    folder.mkdir()
    (folder / "first.txt").write_bytes(b"first")
    (folder / "second.txt").write_bytes(b"second")
    rmtree = writes.shutil.rmtree

    def partially_delete(path, *args, **kwargs):
        if Path(path) == folder:
            (folder / "first.txt").unlink()
            raise PermissionError(13, "Access is denied", str(folder / "second.txt"))
        return rmtree(path, *args, **kwargs)

    monkeypatch.setattr(writes.shutil, "rmtree", partially_delete)
    result = await call(mcp, "delete_path", {"path": "out/partial"})
    assert result.get("isError")
    assert share.snapshots.touched(share.task_id) == ["out/partial"]
    monkeypatch.undo()
    assert share.snapshots.undo(share.task_id) == ["out/partial"]
    assert (folder / "first.txt").read_bytes() == b"first"
    assert (folder / "second.txt").read_bytes() == b"second"


async def test_readonly_write_can_be_retried_after_permission_is_fixed(mcp, share):
    target = share.root / "raw" / "a.txt"
    target.chmod(stat.S_IREAD)
    try:
        result = await call(mcp, "write_file", {"path": "raw/a.txt", "content": "denied"})
        assert result.get("isError") and "Windows がこの操作を断りました" in result["content"][0]["text"]
        assert share.snapshots.touched(share.task_id) == []
    finally:
        target.chmod(stat.S_IREAD | stat.S_IWRITE)
    result = await call(mcp, "write_file", {"path": "raw/a.txt", "content": "allowed"})
    assert not result.get("isError"), result
    assert share.snapshots.undo(share.task_id) == ["raw/a.txt"]
    assert target.read_text(encoding="utf-8") == ORIGINAL_A


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
        await host.mgr.on_agent_message(link[who], {"type": "hello", "agent_version": "0.2.0", "codex_version": "x",
                                                    "auth_mode": "chatgpt"})
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
    res = await call(two.client, "write_file", {"path": "out/late.txt", "content": "x"}, token=two.token["bob"])
    assert res["isError"] and "どの依頼" in res["content"][0]["text"]
    assert two.host.snapshots.touched(two.task["alice"]) == []
    assert two.host.snapshots.touched(None) == []
    assert not (two.root / "out/late.txt").exists()


async def test_a_call_with_no_known_lender_belongs_to_no_task(two):
    # Fail closed: should the caller's name ever be missing, the call must not land in "any active task".
    assert current_lender.get() is None and two.host.mgr.current_task_id() in two.task.values()
    assert two.host.services.task_id_fn() is None
    token = current_lender.set("alice")
    try:
        assert two.host.services.task_id_fn() == two.task["alice"]
    finally:
        current_lender.reset(token)


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


@pytest.fixture
async def parallel(two):
    await two.host.mgr.on_agent_message(two.link["alice"], {
        "type": "hello", "protocol": 5, "capabilities": {"max_parallel": 2}})
    one = two.task["alice"]
    second = await two.host.mgr.start("second task", lender="alice")
    for task, thread in [(one, "thread-one"), (second, "thread-two")]:
        await two.host.mgr.on_agent_message(two.link["alice"], {
            "type": "task_state", "task_id": task, "thread_id": thread, "state": "running"})
    two.ids = [one, second]
    return two


async def thread_call(env, thread, name, args):
    params = {"name": name, "arguments": args}
    if thread is not None:
        params["_meta"] = {"threadId": thread, "sessionId": thread}
    return (await rpc(env.client, "tools/call", params, token=env.token["alice"]))["result"]


async def test_parallel_threads_write_and_undo_only_their_own_files(parallel):
    e = parallel
    results = await asyncio.gather(*(
        thread_call(e, thread, "write_file", {"path": f"out/{i}.txt", "content": thread})
        for i, thread in enumerate(("thread-one", "thread-two"))))
    assert all(not r.get("isError") for r in results)
    assert [e.host.mgr.result(t)["changed_files"] for t in e.ids] == [["out/0.txt"], ["out/1.txt"]]
    assert e.host.snapshots.touched(None) == []
    e.host.snapshots.undo(e.ids[0])
    assert not (e.root / "out/0.txt").exists()
    assert (e.root / "out/1.txt").read_text(encoding="utf-8") == "thread-two"


@pytest.mark.parametrize("thread", [None, "", "unmatched", 7, {"thread": "thread-one"}])
@pytest.mark.parametrize("name,args", [
    ("write_file", {"path": "out/ambiguous.txt", "content": "x"}),
    ("delete_path", {"path": "raw/a.txt"}),
    ("run_command", {"command": "echo ambiguous"}),
    ("apply_patch", {"patch": "*** Begin Patch\n*** Add File: out/ambiguous.txt\n+x\n*** End Patch\n"}),
])
async def test_parallel_mutations_without_an_identified_task_are_refused(parallel, thread, name, args):
    res = await thread_call(parallel, thread, name, args)
    assert res["isError"] and "どの依頼の操作か分からない" in res["content"][0]["text"]
    assert not parallel.host.approvals.pending() and not parallel.host.services.runner.jobs
    assert all(parallel.host.snapshots.touched(t) == [] for t in [None, *parallel.ids])
    read = await thread_call(parallel, thread, "read_file", {"path": "raw/a.txt"})
    assert not read.get("isError")


@pytest.mark.parametrize("thread", [None, "unmatched"])
async def test_one_remaining_task_keeps_legacy_fallback(parallel, thread):
    e = parallel
    await e.host.mgr.on_agent_message(e.link["alice"], {
        "type": "task_state", "task_id": e.ids[1], "state": "completed"})
    res = await thread_call(e, thread, "write_file", {"path": "out/fallback.txt", "content": "x"})
    if thread is None:
        assert not res.get("isError")
        assert e.host.snapshots.touched(e.ids[0]) == ["out/fallback.txt"]
    else:
        assert res["isError"] and "どの依頼" in res["content"][0]["text"]
        assert not e.host.snapshots.touched(e.ids[0])


async def test_parallel_delete_approvals_keep_their_thread_even_after_task_completion(parallel):
    e = parallel
    for i in range(2):
        (e.root / f"out/delete-{i}.txt").write_text(str(i), encoding="utf-8")
    deleting = [asyncio.create_task(thread_call(e, thread, "delete_path", {"path": f"out/delete-{i}.txt"}))
                for i, thread in enumerate(("thread-one", "thread-two"))]
    approvals = await pending(e, 2)
    assert {a.task_id for a in approvals} == set(e.ids)
    # The in-flight call keeps its original task after an await, even when it has since ended.
    await e.host.mgr.on_agent_message(e.link["alice"], {
        "type": "task_state", "task_id": e.ids[0], "state": "completed"})
    for approval in approvals:
        e.host.approvals.decide(approval.id, True)
    results = await asyncio.gather(*deleting)
    assert all(not res.get("isError") for res in results)
    assert [e.host.snapshots.touched(t) for t in e.ids] == [["out/delete-0.txt"], ["out/delete-1.txt"]]
    audit = [json.loads(line) for line in e.host.audit.path.read_text(encoding="utf-8").splitlines()]
    calls = [row for row in audit if row["kind"] == "tool_call" and row["tool"] == "delete_path"]
    assert {row["args"]["path"]: row["task_id"] for row in calls} == dict(zip(("out/delete-0.txt", "out/delete-1.txt"), e.ids))


async def test_parallel_command_approvals_and_results_do_not_mix(parallel):
    e = parallel
    running = [asyncio.create_task(thread_call(e, thread, "run_command", {"command": f"echo {i}"}))
               for i, thread in enumerate(("thread-one", "thread-two"))]
    approvals = await pending(e, 2)
    assert {a.summary: a.task_id for a in approvals} == dict(zip(("echo 0", "echo 1"), e.ids))
    for approval in approvals:
        e.host.approvals.decide(approval.id, approval.task_id == e.ids[0])
    results = await asyncio.gather(*running)
    jobs = [json.loads(res["content"][0]["text"]) for res in results]
    assert [job["status"] for job in jobs] == ["done", "denied"]
    wrong = await thread_call(e, "thread-two", "get_command_result", {"job_id": jobs[0]["job_id"]})
    assert wrong["isError"] and "ジョブが見つかりません" in wrong["content"][0]["text"]
    own = await thread_call(e, "thread-one", "get_command_result", {"job_id": jobs[0]["job_id"]})
    assert json.loads(own["content"][0]["text"])["stdout"].strip() == "0"


@pytest.mark.parametrize("tool,args", [
    ("write_file", {"path": "out/late.txt", "content": "late"}),
    ("delete_path", {"path": "out/late.txt"}),
    ("run_command", {"command": "echo late"}),
])
async def test_cancelled_thread_cannot_mutate_the_remaining_tasks_files(parallel, tool, args):
    e = parallel
    target = e.root / "out/late.txt"
    target.write_text("original", encoding="utf-8")
    await e.host.mgr.cancel(e.ids[0])
    await e.host.mgr.on_agent_message(e.link["alice"], {
        "type": "task_state", "task_id": e.ids[0], "state": "cancelled", "thread_id": "thread-one"})
    assert e.host.mgr.active_task_ids("alice") == [e.ids[1]]
    result = await thread_call(e, "thread-one", tool, args)
    assert result["isError"] and "どの依頼" in result["content"][0]["text"]
    assert target.read_text(encoding="utf-8") == "original"
    assert not e.host.snapshots.touched(e.ids[1]) and not e.host.snapshots.touched(None)
    assert not e.host.approvals.pending() and not e.host.services.runner.jobs


async def test_single_task_without_a_known_thread_keeps_startup_fallback(two):
    task = two.task["alice"]
    assert two.host.mgr.tasks[task].thread_id is None
    result = await thread_call(two, "new-thread-not-announced-yet", "write_file",
                               {"path": "out/startup.txt", "content": "x"})
    assert not result.get("isError")
    assert two.host.snapshots.touched(task) == ["out/startup.txt"]


@pytest.mark.parametrize("tool,args", [
    ("write_file", {"path": "out/late-startup.txt", "content": "late"}),
    ("delete_path", {"path": "out/late-startup.txt"}),
    ("run_command", {"command": "echo late"}),
])
async def test_ended_thread_cannot_mutate_a_queued_task_without_a_known_thread(two, tool, args):
    e = two
    first = e.task["alice"]
    await e.host.mgr.on_agent_message(e.link["alice"], {
        "type": "task_state", "task_id": first, "state": "running", "thread_id": "thread-1"})
    await e.host.mgr.cancel(first)
    await e.host.mgr.on_agent_message(e.link["alice"], {
        "type": "task_state", "task_id": first, "state": "cancelled", "thread_id": "thread-1"})
    second = await e.host.mgr.start("waiting for thread", lender="alice")
    assert e.host.mgr.tasks[second].state == "queued" and e.host.mgr.tasks[second].thread_id is None
    target = e.root / "out/late-startup.txt"
    target.write_text("original", encoding="utf-8")
    result = await thread_call(e, "thread-1", tool, args)
    assert result["isError"] and "どの依頼" in result["content"][0]["text"]
    assert target.read_text(encoding="utf-8") == "original"
    assert not e.host.snapshots.touched(second) and not e.host.snapshots.touched(None)
    assert not e.host.approvals.pending() and not e.host.services.runner.jobs
