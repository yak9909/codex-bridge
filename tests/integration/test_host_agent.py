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


# ---- pre-approval by the lender, over the real channel (design 4.5) ---------------------------------------
def pre_approval(runner, on=True):
    runner.apply_settings({"limits": {"five_hour": 80, "weekly": 80, "force": False},
                           "features": {"web_search": False, "image_generation": False, "multi_agent": False,
                                        "goals": False},
                           "pre_approval": {"enabled": on, "timeout_min": 30}, "disposable": False,
                           "workdir": None, "log_retention_days": 30})


@pytest.mark.parametrize("live", [{"after": {"turn/start": [TURN_STARTED, MSG, DONE]}}], indirect=True)
async def test_a_pending_approval_is_not_lost_when_the_connection_drops_and_comes_back(live):
    mgr, runner, client = live
    pre_approval(runner)
    tid = await mgr.start("x")
    await until(lambda: mgr.status(tid)["state"] == "awaiting_lender")
    assert mgr.current_task_id() == tid
    since = mgr.agent_info()["since"]
    await client.drop()
    await until(lambda: mgr.agent_info()["since"] != since and mgr.agent_info()["ready"])  # connected again, hello said
    await asyncio.sleep(0.2)  # whatever the agent says again after hello has arrived
    assert mgr.status(tid)["state"] == "awaiting_lender"  # not failed, and not lost
    assert mgr.current_task_id() == tid
    with pytest.raises(Exception, match="作業中"):
        await mgr.start("y")  # the lender still counts as working
    await runner.decide_pending(True)
    await until(lambda: mgr.status(tid)["state"] == "completed")
    assert mgr.result(tid)["report"] == "完了"


@pytest.mark.parametrize("live", [{"after": {"turn/start": [TURN_STARTED, MSG, DONE]}}], indirect=True)
async def test_a_rejection_reaches_the_host_with_its_reason_and_frees_the_lender(live):
    mgr, runner, client = live
    pre_approval(runner)
    tid = await mgr.start("x")
    await until(lambda: mgr.status(tid)["state"] == "awaiting_lender")
    await runner.decide_pending(False, "今日は使いません")
    await until(lambda: mgr.status(tid)["state"] == "rejected_by_lender")
    r = mgr.result(tid)
    assert "今日は使いません" in r["report"] and r["finished"] and mgr.current_task_id() is None
    pre_approval(runner, on=False)
    assert await mgr.start("again")


@pytest.mark.parametrize("live", [{"after": {"turn/start": [TURN_STARTED, MSG, DONE]}}], indirect=True)
async def test_when_the_exe_ends_the_borrower_is_told_the_request_was_rejected(live):
    mgr, runner, client = live
    pre_approval(runner)
    tid = await mgr.start("x")
    await until(lambda: mgr.status(tid)["state"] == "awaiting_lender")
    await runner.shutdown()
    await until(lambda: mgr.status(tid)["state"] == "rejected_by_lender")
    assert "終了" in mgr.result(tid)["report"]


@pytest.mark.parametrize("live", [{"after": {"turn/start": [TURN_STARTED, MSG, DONE]}}], indirect=True)
async def test_a_hello_about_changed_settings_while_a_task_starts_does_not_fail_the_task(live):
    mgr, runner, client = live
    seen: list[str] = []
    mgr.on_change.append(lambda: seen.extend(t.state for t in mgr.tasks.values()))
    tid = await mgr.start("x")
    await client.refresh_hello()  # the task is on its way: the agent has not heard of it yet
    await until(lambda: mgr.status(tid)["state"] == "completed")
    assert "failed" not in seen
    assert mgr.agent_info()["ready"]


@pytest.mark.parametrize("live", [{"after": {"turn/start": [TURN_STARTED],
                                             "turn/steer": [MSG, DONE]}}], indirect=True)
async def test_a_v01_agent_that_says_hello_before_resending_still_gets_its_result_through(live):
    mgr, runner, client = live

    async def v01_order():  # a v0.1 exe: hello first, then what the host missed
        await client.send_hello()
        await runner.resend_unacked()
    client.on_connected = v01_order
    tid = await mgr.start("x")
    await until(lambda: mgr.status(tid)["state"] == "running")
    await asyncio.sleep(0.2)
    await client.drop()
    await until(lambda: mgr.status(tid)["state"] == "agent_offline")
    await runner.handle({"type": "task_followup", "task_id": tid, "text": "続けて"})  # ends while the link is down
    await until(lambda: mgr.status(tid)["state"] == "completed")
    assert mgr.result(tid)["report"] == "完了" and mgr.current_task_id() is None
