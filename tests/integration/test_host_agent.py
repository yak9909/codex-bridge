"""Real host control channel over a real WebSocket; agent uses the fake app-server."""

import asyncio
import json

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from starlette.websockets import WebSocket

from codex_bridge.agent.control_client import ControlClient
from codex_bridge.agent.runner import AgentRunner
from codex_bridge.agent.sessions import SessionRegistry
from codex_bridge.agent.usage import QuotaGuard
from codex_bridge.common.invite import Invite
from codex_bridge.host.control import add_control_route
from codex_bridge.host.tasks import TaskManager
from codex_bridge.host.tokens import TokenStore
from fake_helpers import Fake
from codex_bridge.common.invite import make_invite

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
async def multiple(tmp_path, request):
    from codex_bridge.agent.main import AgentConnections
    managers, servers, serving, codes, messages, fakes = [], [], [], [], [], {}
    for i in range(2):
        home = tmp_path / f"host-{i}"
        mgr = TaskManager(home, touched_fn=lambda t: [])
        token = TokenStore(home)
        code_token = token.issue("friend")
        app = FastAPI()
        add_control_route(app, mgr, token)
        observed = []
        original = mgr.on_agent_message

        async def observe(link, message, original=original, observed=observed):
            observed.append(message)
            await original(link, message)

        mgr.on_agent_message = observe
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
        serving.append(asyncio.create_task(server.serve()))
        await until(lambda: server.started)
        port = server.servers[0].sockets[0].getsockname()[1]
        codes.append(make_invite(f"http://127.0.0.1:{port}", code_token, "friend"))
        managers.append(mgr)
        servers.append(server)
        messages.append(observed)

    def factory(connection, settings):
        root = tmp_path / ("fake-" + connection.id)
        root.mkdir()
        fake = fakes[connection.id] = Fake(root, {
            "after": {"turn/start": [TURN_STARTED], "turn/steer": [MSG, DONE]},
            **getattr(request, "param", {}),
        })
        fake.app.features = settings["features"]
        return fake.app

    runtime = AgentConnections(tmp_path / "agent", app_factory=factory, versions={"auth_mode": "unknown"},
                               initial_backoff=0.1)
    connections = [await runtime.add(code) for code in codes]
    await runtime.start()
    try:
        await until(lambda: all(mgr.agent_info().get("ready") for mgr in managers))
        yield runtime, connections, managers, messages
    finally:
        await runtime.stop()
        for server in servers:
            server.should_exit = True
        await asyncio.gather(*serving)


async def test_add_two_connections_and_both_connect(multiple):
    runtime, connections, managers, messages = multiple
    assert len(runtime.store.list()) == 2
    assert all(any(m["type"] == "hello" for m in stream) for stream in messages)
    assert sum(r.app.running for r in runtime.runners.values()) <= 1


async def test_startup_reports_auth_and_usage_to_every_host_without_a_task(multiple):
    runtime, connections, managers, messages = multiple
    await until(lambda: all(mgr.usage()["five_hour"] is not None for mgr in managers), timeout=2)
    assert all(mgr.agent_info()["auth_mode"] == "chatgpt" for mgr in managers)
    assert all(mgr.usage()["five_hour"]["used_percent"] == 12 for mgr in managers)
    assert all(any(m["type"] == "usage" for m in stream) for stream in messages)
    assert runtime.slot.last_usage is not None
    assert all(r.last_usage == runtime.slot.last_usage for r in runtime.runners.values())
    assert sum(r.app.running for r in runtime.runners.values()) == 1
    assert all(not mgr.tasks for mgr in managers) and runtime.slot.owner is None
    from codex_bridge.agent.ui import create_ui_app
    app = create_ui_app(None, runtime.settings, None, versions=runtime.versions, data_root=runtime.home,
                        connections=runtime)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8766") as http:
        overview = (await http.get("/api/overview")).json()
    assert overview["usage"]["five_hour"]["used_percent"] == 12
    assert overview["auth_mode"] == "chatgpt" and overview["state"] == "idle"


def model_list_count(tmp_path):
    return sum(json.loads(line).get("received", {}).get("method") == "model/list"
               for record in tmp_path.glob("fake-*/record.jsonl")
               for line in record.read_text(encoding="utf-8").splitlines())


async def test_catalog_loaded_once_and_offer_reaches_host(multiple, tmp_path):
    runtime, connections, managers, messages = multiple
    await runtime._usage_probe
    await until(lambda: all(any(m["type"] == "lender_models" for m in stream) for stream in messages))
    for stream in messages:
        offers = [m for m in stream if m["type"] == "lender_models"]
        assert len(offers) == 1
        assert offers[0]["models"][0]["model"] == "gpt-fake"
        assert offers[0]["max_effort"] == "xhigh" and "seq" not in offers[0]
        assert offers[0]["models"][0]["efforts"] == ["low", "medium", "high", "xhigh"]
    assert all(runner.catalog is runtime.catalog for runner in runtime.runners.values())
    assert model_list_count(tmp_path) == 1
    await runtime.runners[connections[1].id].prepare()
    assert model_list_count(tmp_path) == 1


async def test_settings_change_refetches_catalog(multiple, tmp_path):
    runtime, connections, managers, messages = multiple
    await runtime._usage_probe
    assert model_list_count(tmp_path) == 1
    catalog = runtime.catalog
    await runtime.update_settings(connections[0].id, {"allowed_models": [], "max_effort": "high"})
    from codex_bridge.agent.ui import create_ui_app
    app = create_ui_app(None, runtime.settings, None, versions=runtime.versions, data_root=runtime.home,
                        connections=runtime)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8766") as http:
        response = await http.get(f"/api/connections/{connections[0].id}/settings")
    assert response.status_code == 200 and response.json()["models"] == catalog
    assert runtime.catalog is catalog
    assert model_list_count(tmp_path) == 1
    await until(lambda: [m for m in messages[0] if m["type"] == "hello"][-1].get("models") == [])
    hello = [m for m in messages[0] if m["type"] == "hello"][-1]
    assert hello["max_effort"] == "high"
    await runtime.runners[connections[0].id].prepare()
    assert model_list_count(tmp_path) == 2
    assert all(runner.catalog is runtime.catalog for runner in runtime.runners.values())
    await until(lambda: any(m["type"] == "lender_models" and m["models"] == [] for m in messages[0]))
    offer = [m for m in messages[0] if m["type"] == "lender_models"][-1]
    assert offer["models"] == [] and offer["max_effort"] == "high"
    other = [m for m in messages[1] if m["type"] == "lender_models"][-1]
    assert other["models"][0]["model"] == "gpt-fake" and other["max_effort"] == "xhigh"


@pytest.mark.parametrize("multiple,expected", [
    ({"responses": {
        "model/list": {"data": [
            {"model": "m1", "isDefault": True, "defaultReasoningEffort": "medium",
             "supportedReasoningEfforts": [{"reasoningEffort": e} for e in ("medium", "high")]},
            {"model": "m2", "isDefault": False, "defaultReasoningEffort": "medium",
             "supportedReasoningEfforts": [{"reasoningEffort": e} for e in ("medium", "high")]},
        ]},
        "config/read": {"config": {"model": "m2", "model_reasoning_effort": "high"}, "origins": {}},
        "thread/start": {"thread": {"id": "th1"}, "model": "m2", "reasoningEffort": "high"},
    }}, ("m2", "high")),
    ({"errors": {"config/read": {"code": -32601, "message": "unsupported"}}}, ("gpt-fake", "medium")),
], indirect=["multiple"])
async def test_config_default_or_read_failure_reaches_turn_start(multiple, tmp_path, expected):
    runtime, connections, managers, messages = multiple
    await runtime._usage_probe
    logs = []
    runtime.on_log = logs.append
    # Exercise config/read again while observing that its failure adds no log.
    await runtime._load_catalog(runtime.runners[connections[0].id].app)
    assert not logs
    tid = await managers[0].start("work")
    await until(lambda: managers[0].status(tid)["state"] == "running")
    received = [json.loads(line)["received"]
                for record in tmp_path.glob("fake-*/record.jsonl")
                for line in record.read_text(encoding="utf-8").splitlines()
                if "received" in json.loads(line)]
    assert any(m.get("method") == "config/read" and m["params"] == {} for m in received)
    thread = next(m["params"] for m in received if m.get("method") == "thread/start")
    assert thread["model"] == expected[0]
    turn = next(m["params"] for m in received if m.get("method") == "turn/start")
    assert (turn["model"], turn["effort"]) == expected


@pytest.mark.parametrize("pending", [False, True])
async def test_only_one_task_at_a_time(multiple, pending):
    runtime, connections, (a, b), (_, b_messages) = multiple
    ar, br = (runtime.runners[c.id] for c in connections)
    await runtime.update_settings(connections[0].id, {"pre_approval": {"enabled": pending, "timeout_min": None}})
    tid = await a.start("work")
    await until(lambda: a.status(tid)["state"] == ("awaiting_lender" if pending else "running"))
    await until(lambda: any(m["type"] == "lender_status" and m["busy_elsewhere"] for m in b_messages))
    # B's host would now refuse routing, so inject a request that was already in flight.
    await br.handle({"type": "task_start", "task_id": "in-flight", "prompt": "other"})
    await br.handle({"type": "task_followup", "task_id": "followup-flight", "text": "other"})
    await until(lambda: len([m for m in b_messages if m.get("state") == "rejected_by_lender"]) == 2)
    rejected = [m for m in b_messages if m.get("state") == "rejected_by_lender"]
    assert all(m["report"] == "貸している人は、ほかの依頼主の作業をしています。あとでもう一度頼むか、ほかの人に頼んでください。" for m in rejected)
    if pending:
        client = runtime.clients[connections[0].id]
        since = a.agent_info()["since"]
        await client.drop()
        await until(lambda: a.agent_info()["since"] != since and a.agent_info()["ready"])
        assert a.status(tid)["state"] == "awaiting_lender"
        await ar.decide_pending(False)
    else:
        await ar.handle({"type": "task_followup", "task_id": tid, "text": "finish"})
        await until(lambda: a.status(tid)["state"] == "completed")
    await until(lambda: [m for m in b_messages if m["type"] == "lender_status"][-1]["busy_elsewhere"] is False)
    assert b.lenders()[0]["busy_elsewhere"] is False


async def test_rejected_invite_is_reported(tmp_path):
    from codex_bridge.agent.main import AgentConnections
    app = FastAPI()

    @app.websocket("/agent/ws")
    async def socket(ws: WebSocket):
        if ws.headers.get("authorization") != "Bearer valid":
            await ws.close()  # before accept: HTTP 403
            return
        await ws.accept()
        from starlette.websockets import WebSocketDisconnect
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    serve = asyncio.create_task(server.serve())
    await until(lambda: server.started)
    port = server.servers[0].sockets[0].getsockname()[1]

    def factory(connection, settings):
        root = tmp_path / ("fake-" + connection.id)
        root.mkdir(exist_ok=True)
        return Fake(root).app

    runtime = AgentConnections(tmp_path / "agent", app_factory=factory, versions={}, initial_backoff=0.1)
    await runtime.start()
    try:
        connection = await runtime.add(make_invite(f"http://127.0.0.1:{port}", "bad", "friend"))
        await until(lambda: runtime.rows()[0]["invite_problem"] == "rejected")
        await runtime.replace_invite(connection.id, make_invite(f"http://127.0.0.1:{port}", "valid", "friend"))
        await until(lambda: runtime.rows()[0]["connected"])
        assert runtime.rows()[0]["invite_problem"] is None
    finally:
        await runtime.stop()
        server.should_exit = True
        await serve


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
                           "pre_approval": {"new_task": on, "followup": on, "timeout_min": 30}, "disposable": False,
                           "workdir": None, "log_retention_days": 30,
                           "allowed_models": None, "max_effort": "xhigh"})


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
