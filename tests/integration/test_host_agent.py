"""Real host control channel over a real WebSocket; agent uses the fake app-server."""

import asyncio

import pytest
import uvicorn
from fastapi import FastAPI

from codex_bridge.agent.control_client import ControlClient
from codex_bridge.agent.runner import AgentRunner
from codex_bridge.agent.sessions import SessionRegistry
from codex_bridge.agent.usage import QuotaGuard
from codex_bridge.common.invite import Invite
from codex_bridge.host.control import add_control_route
from codex_bridge.host.tasks import TaskManager
from codex_bridge.host.tokens import TokenStore
from fake_helpers import Fake

TURN_STARTED = {"method": "turn/started", "params": {"threadId": "th1", "turn": {"id": "u1"}}}
MSG = {"method": "item/completed", "params": {"threadId": "th1", "turnId": "u1", "completedAtMs": 0,
                                              "item": {"type": "agentMessage", "id": "m", "text": "完了"}}}
DONE = {"method": "turn/completed", "params": {"threadId": "th1", "turn": {"id": "u1", "status": "completed"}}}


async def until(pred, timeout=10):
    for _ in range(int(timeout / 0.05)):
        if pred():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("timed out")


@pytest.fixture
async def live(tmp_path, request):
    mgr = TaskManager(tmp_path / "host", touched_fn=lambda t: [])
    tokens = TokenStore(tmp_path / "host")
    tok = tokens.issue("friend")
    app = FastAPI()
    add_control_route(app, mgr, tokens)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    serve = asyncio.create_task(server.serve())
    await until(lambda: server.started)
    port = server.servers[0].sockets[0].getsockname()[1]

    fake = Fake(tmp_path, request.param)
    await fake.app.start()
    runner = AgentRunner(fake.app, QuotaGuard({"five_hour": 90, "weekly": 95}),
                         SessionRegistry(tmp_path / "sessions.json"), send=None, usage_interval=3600)
    client = ControlClient(Invite(f"http://127.0.0.1:{port}", tok, "friend"), runner,
                           hello={"agent_version": "test", "codex_version": "fake", "auth_mode": "chatgpt"},
                           initial_backoff=0.2)
    await runner.start()
    run = asyncio.create_task(client.run_forever())
    await until(lambda: mgr.agent_info().get("connected") and mgr.agent_info().get("auth_mode"))
    yield mgr, runner, client
    run.cancel()
    await runner.stop()
    await fake.app.stop()
    server.should_exit = True
    await serve


@pytest.mark.parametrize("live", [{"after": {"turn/start": [TURN_STARTED, MSG, DONE]}}], indirect=True)
async def test_full_round_trip(live):
    mgr, runner, client = live
    tid = await mgr.start("x")
    await until(lambda: mgr.status(tid)["state"] == "completed")
    assert mgr.result(tid)["report"] == "完了" and mgr.result(tid)["thread_id"] == "th1"
    assert mgr.usage()["five_hour"]["used_percent"] == 12


@pytest.mark.parametrize("live", [{"after": {"turn/start": [TURN_STARTED],
                                             "turn/steer": [MSG, DONE]}}], indirect=True)
async def test_reconnect_resends_unacked_events(live):
    mgr, runner, client = live
    tid = await mgr.start("x")
    await until(lambda: mgr.status(tid)["state"] == "running")
    await asyncio.sleep(0.2)
    await client.drop()
    await until(lambda: mgr.status(tid)["state"] == "agent_offline")
    # The turn finishes while the host cannot be reached.
    await runner.handle({"type": "task_followup", "task_id": tid, "text": "続けて"})
    await until(lambda: mgr.status(tid)["state"] == "completed")
    assert [e["kind"] for e in mgr.events(tid)].count("agent_message") == 1
    assert mgr.result(tid)["report"] == "完了"
