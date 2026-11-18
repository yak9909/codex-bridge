import json

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from codex_bridge.host.control import add_control_route
from codex_bridge.host.paths import ToolError
from codex_bridge.host.tasks import TaskManager, TaskRecord
from codex_bridge.host.tokens import TokenStore


@pytest.fixture
def env(tmp_path):
    mgr = TaskManager(tmp_path / "data", touched_fn=lambda t: [])
    tokens = TokenStore(tmp_path / "data")
    tok = tokens.issue("friend")
    app = FastAPI()
    add_control_route(app, mgr, tokens)
    return mgr, tokens, tok, TestClient(app)


def test_rejects_bad_token(env):
    mgr, tokens, tok, client = env
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/agent/ws", headers={"Authorization": "Bearer wrong"}) as ws:
            ws.receive_text()


def test_hello_gets_settings_and_events_are_acked(env):
    mgr, tokens, tok, client = env
    with client.websocket_connect("/agent/ws", headers={"Authorization": f"Bearer {tok}"}) as ws:
        ws.send_text(json.dumps({"type": "hello", "agent_version": "0.1", "codex_version": "x", "auth_mode": "chatgpt"}))
        assert json.loads(ws.receive_text())["type"] == "settings"
        assert mgr.agent_info()["connected"] is True
        assert [l["name"] for l in mgr.lenders()] == ["friend"]  # the lender is the invite name of the token
        ws.send_text(json.dumps({"type": "usage", "five_hour": {"used_percent": 10, "resets_at": 1}, "weekly": None, "ts": 0}))
        ws.send_text(json.dumps({"type": "event", "seq": 1, "task_id": "t0", "kind": "status", "data": {}}))
        assert json.loads(ws.receive_text()) == {"type": "ack", "seq": 1}
    assert mgr.agent_info()["connected"] is False
    assert mgr.usage()["five_hour"]["used_percent"] == 10


def test_numbers_json_cannot_carry_do_not_get_past_the_socket(env):
    # NaN, Infinity and 1e999 (which reads as infinity) in what an agent sends: replaced on receipt, so
    # that nothing kept from the message can break a page or a file later.
    mgr, tokens, tok, client = env
    mgr.tasks["t0"] = TaskRecord(task_id="t0", prompt="p", created=0, state="running", lender="friend")
    with client.websocket_connect("/agent/ws", headers={"Authorization": f"Bearer {tok}"}) as ws:
        ws.send_text('{"type": "hello", "agent_version": "0.2", "codex_version": NaN, "auth_mode": "chatgpt", '
                     '"protocol": 1e999, "current_task_id": "t0"}')
        assert json.loads(ws.receive_text())["type"] == "settings"
        ws.send_text('{"type": "event", "seq": 1, "task_id": "t0", "kind": "status", "data": {"text": NaN, "n": [1e999, -1e999]}}')
        assert json.loads(ws.receive_text()) == {"type": "ack", "seq": 1}
        ws.send_text('{"type": "task_state", "seq": 2, "task_id": "t0", "state": NaN, "report": Infinity}')
        assert json.loads(ws.receive_text()) == {"type": "ack", "seq": 2}
        ws.send_text('{"type": "usage", "five_hour": {"used_percent": 10, "resets_at": 1e999}, "weekly": null, "ts": -Infinity}')
        ws.send_text('{"type": "task_state", "seq": 3, "task_id": "t0", "state": "running", "resume_at": 1e999}')
        assert json.loads(ws.receive_text()) == {"type": "ack", "seq": 3}  # the connection is still alive
        assert mgr.status("t0")["state"] == "running" and mgr.result("t0")["report"] is None
    assert mgr.events("t0")[0]["data"] == {"text": None, "n": [None, None]}
    for value in (mgr.list_tasks(), mgr.result("t0"), mgr.status("t0"), mgr.events("t0"), mgr.lenders(), mgr.usage()):
        json.dumps(value, allow_nan=False)
    again = TaskManager(mgr.dir.parent, touched_fn=lambda t: [])  # and nothing of it was written to disk
    json.dumps([again.list_tasks(), again.events("t0")], allow_nan=False)


def test_each_token_connects_as_its_own_lender(env):
    mgr, tokens, tok, client = env
    tok2 = tokens.issue("second")
    with client.websocket_connect("/agent/ws", headers={"Authorization": f"Bearer {tok}"}) as ws1, \
            client.websocket_connect("/agent/ws", headers={"Authorization": f"Bearer {tok2}"}) as ws2:
        hello = {"type": "hello", "agent_version": "0.1", "codex_version": "x", "auth_mode": "chatgpt"}
        ws1.send_text(json.dumps(hello))
        ws2.send_text(json.dumps({**hello, "auth_mode": "apikey"}))
        assert json.loads(ws1.receive_text())["type"] == "settings"
        assert json.loads(ws2.receive_text())["type"] == "settings"
        assert {l["name"]: (l["connected"], l["auth_mode"]) for l in mgr.lenders()} == {
            "friend": (True, "chatgpt"), "second": (True, "apikey")}
    assert {l["name"]: l["connected"] for l in mgr.lenders()} == {"friend": False, "second": False}


def test_token_store_matches_a_token_to_its_name(tmp_path):
    ts = TokenStore(tmp_path)
    a, b = ts.issue("a"), ts.issue("b")
    assert ts.match(a) == "a" and ts.match(b) == "b"
    assert ts.match("wrong") is None and ts.match("") is None
    ts.revoke("a")
    assert ts.match(a) is None and TokenStore(tmp_path).match(b) == "b"


def test_revoked_token_cannot_connect(env):
    mgr, tokens, tok, client = env
    tokens.revoke("friend")
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/agent/ws", headers={"Authorization": f"Bearer {tok}"}) as ws:
            ws.receive_text()


def test_token_store_keeps_only_hashes(tmp_path):
    ts = TokenStore(tmp_path)
    tok = ts.issue("a")
    raw = (tmp_path / "tokens.json").read_text(encoding="utf-8")
    assert tok not in raw and len(ts.hashes()) == 1
    assert TokenStore(tmp_path).hashes() == ts.hashes()
    assert [t["name"] for t in ts.list()] == ["a"]


@pytest.mark.parametrize("reissue", [False, True])
def test_final_review_token_change_closes_connected_lender(env, reissue):
    mgr, tokens, tok, client = env
    with client.websocket_connect("/agent/ws", headers={"Authorization": f"Bearer {tok}"}) as ws:
        ws.send_json({"type": "hello"})
        assert ws.receive_json()["type"] == "settings"
        mgr.tasks["t1"] = TaskRecord("t1", "p", 0, state="running", lender="friend")
        if reissue:
            new = tokens.issue("friend")
        else:
            tokens.revoke("friend")
        # Check detachment first so a regression fails without blocking on receive.
        import asyncio
        import time
        deadline = time.monotonic() + 2
        while mgr.agent_info("friend")["connected"] and time.monotonic() < deadline:
            time.sleep(0.01)
        assert mgr.agent_info("friend")["connected"] is False
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
        assert mgr.tasks["t1"].state == "agent_offline"
        for name in (None, "friend"):
            with pytest.raises(ToolError, match="接続"):
                asyncio.run(mgr.start("must not send", lender=name))
    if reissue:
        assert tokens.match(tok) is None
        with client.websocket_connect("/agent/ws", headers={"Authorization": f"Bearer {new}"}) as ws:
            ws.send_json({"type": "hello", "current_task_id": None})
            assert ws.receive_json()["type"] == "settings"
            assert mgr.agent_info("friend")["connected"] is True
