import json

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from codex_bridge.host.control import add_control_route
from codex_bridge.host.tasks import TaskManager
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
