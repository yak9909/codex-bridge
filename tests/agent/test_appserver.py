import asyncio

import pytest

from codex_bridge.agent.appserver import AppServerError
from codex_bridge.agent.usage import Window
from fake_helpers import Fake


@pytest.fixture
async def fake(tmp_path, request):
    f = Fake(tmp_path, getattr(request, "param", None), token="SECRET-TOKEN-XYZ")
    yield f
    await f.app.stop()


async def test_launch_args_env_and_thread_params(fake):
    await fake.app.start()
    tid = await fake.app.thread_start("前置き")
    assert tid == "th1"
    argv = fake.argv
    assert argv[0] == "app-server" and "--disable" in argv and "shell_tool" in argv
    assert 'mcp_servers.bridge.url="http://100.64.0.1:8765/codex/mcp"' in argv
    assert 'mcp_servers.bridge.bearer_token_env_var="CODEX_BRIDGE_TOKEN"' in argv
    assert "mcp_servers.bridge.tool_timeout_sec=120" in argv
    # Codex must not ask its own user before calling bridge tools: the host does the approving.
    assert 'mcp_servers.bridge.default_tools_approval_mode="approve"' in argv
    assert "SECRET-TOKEN-XYZ" not in " ".join(argv)
    assert fake.env["CODEX_BRIDGE_TOKEN"] == "SECRET-TOKEN-XYZ"
    p = fake.received("thread/start")
    assert p["sandbox"] == "read-only" and p["approvalPolicy"] == "never"
    assert p["developerInstructions"] == "前置き" and p["cwd"] == str(fake.app.workdir)
    assert fake.received("initialize")["clientInfo"]["name"] == "codex-bridge-agent"


async def test_turn_steer_interrupt_and_delete(fake):
    await fake.app.start()
    turn = await fake.app.turn_start("th1", "hello")
    assert turn == "u1"
    assert fake.received("turn/start")["input"] == [{"type": "text", "text": "hello"}]
    await fake.app.turn_steer("th1", "u1", "more")
    assert fake.received("turn/steer") == {"threadId": "th1", "expectedTurnId": "u1",
                                           "input": [{"type": "text", "text": "more"}]}
    await fake.app.turn_interrupt("th1", "u1")
    assert fake.received("turn/interrupt") == {"threadId": "th1", "turnId": "u1"}
    await fake.app.thread_delete("th1")
    assert fake.received("thread/delete") == {"threadId": "th1"}


async def test_rate_limits(fake):
    await fake.app.start()
    s = await fake.app.rate_limits()
    assert s.five_hour == Window(12, 1800000000) and s.weekly == Window(30, 1800500000)


async def test_stop_closes_rpc_input_when_windows_tree_kill_is_unavailable(fake, monkeypatch):
    from types import SimpleNamespace
    from codex_bridge.agent import appserver

    await fake.app.start()
    monkeypatch.setattr(appserver.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=1))
    await fake.app.stop()
    # The real subprocess must have exited before a caller starts copying the data, even if Windows
    # refuses taskkill. Closing the RPC input lets the server and a venv Python launcher end together.
    assert fake.app.proc.returncode is not None and not fake.app.running


@pytest.mark.parametrize("fake", [{"errors": {"thread/read": {"code": -32600, "message": "no such thread"}}}],
                         indirect=True)
async def test_error_response_raises(fake):
    await fake.app.start()
    with pytest.raises(AppServerError, match="no such thread"):
        await fake.app.thread_read("zz")


@pytest.mark.parametrize("fake", [{"after": {"turn/start": [
    {"method": "turn/started", "params": {"threadId": "th1", "turn": {"id": "u1"}}},
    {"id": "s1", "method": "item/commandExecution/requestApproval", "params": {"threadId": "th1"}},
    {"id": "s2", "method": "item/fileChange/requestApproval", "params": {}},
    {"id": "s3", "method": "execCommandApproval", "params": {}},
    {"id": "s4", "method": "applyPatchApproval", "params": {}},
    {"id": "s5", "method": "mcpServer/elicitation/request", "params": {"serverName": "bridge"}},
    {"id": "s6", "method": "mcpServer/elicitation/request", "params": {"serverName": "other"}},
    {"id": "s7", "method": "item/tool/requestUserInput", "params": {}},
    {"id": "s8", "method": "something/unknown", "params": {}},
]}}], indirect=True)
async def test_server_requests_are_answered(fake):
    await fake.app.start()
    await fake.app.turn_start("th1", "x")
    ev = await asyncio.wait_for(fake.app.events.get(), 5)
    assert ev["method"] == "turn/started"
    r = await fake.wait_for(lambda: {i: fake.responses_to(i) for i in
                                     ("s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8")}
                            if len(fake.responses_to("s8")) else None)
    assert r["s1"][0]["result"] == {"decision": "decline"}
    assert r["s2"][0]["result"] == {"decision": "decline"}
    assert r["s3"][0]["result"] == {"decision": "denied"}
    assert r["s4"][0]["result"] == {"decision": "denied"}
    assert r["s5"][0]["result"]["action"] == "accept"
    assert r["s6"][0]["result"]["action"] == "decline"
    assert r["s7"][0]["result"] == {"answers": {}}
    assert "error" in r["s8"][0]


async def test_account_mode(fake):
    await fake.app.start()
    assert await fake.app.auth_mode() == "chatgpt"


def test_launch_args_isolate_from_friends_codex_setup():
    from codex_bridge.agent.appserver import ISOLATED_FEATURES, launch_args
    argv = launch_args("http://h:1", disabled_mcp=["blender", "my server"])
    for f in ("shell_tool", "unified_exec", "computer_use", "browser_use", "apps", "plugins", "view_image"):
        assert f in ISOLATED_FEATURES
        i = argv.index(f)
        assert argv[i - 1] == "--disable"
    assert "mcp_servers.blender.enabled=false" in argv
    assert 'mcp_servers."my server".enabled=false' in argv
    assert not any("mcp_servers.bridge.enabled" in a for a in argv)


def test_code_mode_host_stays_enabled():
    # Real E2E (codex 0.167): with code_mode_host disabled, every MCP tool call failed
    # with "code-mode host is disabled". It must stay on.
    from codex_bridge.agent.appserver import ISOLATED_FEATURES, launch_args
    assert "code_mode_host" not in ISOLATED_FEATURES
    assert "code_mode_host" not in launch_args("http://h:1")


async def test_thread_start_asks_for_an_ephemeral_thread_only_when_told_to(fake):
    await fake.app.start()
    await fake.app.thread_start("a")
    assert "ephemeral" not in fake.received("thread/start")
    await fake.app.thread_start("b", ephemeral=True)
    assert fake.received("thread/start")["ephemeral"] is True


async def test_start_while_running_does_not_start_a_second_codex(fake):
    await fake.app.start()
    pid = fake.app.proc.pid
    await asyncio.gather(fake.app.start(), fake.app.start())
    assert fake.app.proc.pid == pid and len([l for l in fake._lines() if "argv" in l]) == 1


async def test_restart_uses_the_new_features_and_reports_no_crash_but_a_real_exit_is_reported(fake):
    await fake.app.start()
    await fake.app.restart(features={"web_search": True})
    assert fake.app.running and 'web_search="disabled"' not in [l for l in fake._lines() if "argv" in l][-1]["argv"]
    assert fake.app.events.empty()  # the planned stop of the old server is not an "exited" event
    fake.app.proc.kill()
    ev = await asyncio.wait_for(fake.app.events.get(), 5)
    assert ev["method"] == "_exited"
