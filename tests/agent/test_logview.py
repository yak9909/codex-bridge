from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from codex_bridge.agent.logview import LogStore
from codex_bridge.agent.runner import AgentRunner
from codex_bridge.agent.sessions import SessionRegistry
from codex_bridge.agent.ui import create_ui_app
from codex_bridge.agent.usage import QuotaGuard
from codex_bridge.agent.worklog import WorkLog


@pytest.fixture
def store(tmp_path):
    s = LogStore(tmp_path / "logs")
    s.add({"type": "task_prompt", "task_id": "t1", "prompt": "解析して"})
    s.add({"type": "event", "seq": 1, "task_id": "t1", "kind": "tool_call",
           "data": {"tool": "read_file", "arguments": '{"path": "raw/a.txt"}'}})
    s.add({"type": "event", "seq": 2, "task_id": "t1", "kind": "agent_message", "data": {"text": "中身は…"}})
    s.add({"type": "task_state", "seq": 3, "task_id": "t1", "state": "completed", "report": "終わり"})
    s.add({"type": "final_report", "task_id": "t1", "text": "終わり"})
    return s


@pytest.fixture
async def client(store):
    _, c = settings_app(store.dir.parent)
    async with c:
        yield c


async def test_logview_respects_visibility(client, store):
    store.set_visibility("summary")
    assert [e["kind"] for e in store.visible()] == ["task_prompt", "final_report"]
    store.set_visibility("files")
    kinds = [e["kind"] for e in store.visible()]
    assert kinds == ["task_prompt", "tool_call", "final_report"]
    store.set_visibility("all")
    kinds = [e["kind"] for e in store.visible()]
    assert "agent_message" in kinds and "state" in kinds


async def test_settings_message_sets_visibility(store):
    store.add({"type": "settings", "log_visibility": "all"})
    assert store.visibility == "all"


async def test_page_served(client):
    r = await client.get("/")
    assert r.status_code == 200 and "codex-bridge" in r.text


def test_store_persists_to_disk(tmp_path, store):
    assert len(LogStore(tmp_path / "logs").entries) == len(store.entries)


def settings_app(tmp_path, **kw):
    from codex_bridge.agent.settings import SettingsStore
    store = SettingsStore(tmp_path / "settings.json")
    runner = AgentRunner(SimpleNamespace(workdir=tmp_path / "workdir"), QuotaGuard({}),
                         SessionRegistry(tmp_path / "sessions.json"), send=None,
                         worklog=WorkLog(tmp_path / "logs" / "tasks"))
    app = create_ui_app(runner, store, SimpleNamespace(), versions={}, data_root=tmp_path, **kw)
    t = httpx.ASGITransport(app=app, client=("127.0.0.1", 1))
    return store, httpx.AsyncClient(transport=t, base_url="http://127.0.0.1:8766", headers={"X-Codex-Bridge": "1"})


async def test_limits_api_defaults_save_and_notify(tmp_path):
    seen = []

    async def changed(s):
        seen.append(s)
    store, client = settings_app(tmp_path, on_settings=changed)
    async with client as c:
        assert (await c.get("/api/settings")).json()["settings"]["limits"] == {"five_hour": 80, "weekly": 80, "force": False}
        r = await c.put("/api/settings", json={"limits": {"five_hour": 70, "weekly": None, "force": True}})
        assert r.status_code == 200 and r.json()["limits"] == {"five_hour": 70, "weekly": None, "force": True}
        assert seen[-1]["limits"] == {"five_hour": 70, "weekly": None, "force": True}
        # The new page sends numbers/null; v0.1's persisted limits are migrated by SettingsStore.
        r = await c.put("/api/settings", json={"limits": {"five_hour": 65, "weekly": None, "force": False}})
        assert r.status_code == 200 and seen[-1]["limits"] == {"five_hour": 65, "weekly": None, "force": False}
        assert (await c.put("/api/settings", json={"limits": {"five_hour": 150}})).status_code == 400
        assert (await c.put("/api/settings", json={"limits": {"five_hour": "abc"}})).status_code == 400
        assert (await c.put("/api/settings", json={"limits": {"five_hour": 70}}, headers={"Host": "evil:1"})).status_code == 403
    assert store.get()["limits"] == {"five_hour": 65, "weekly": None, "force": False}


async def test_settings_api_reads_patches_notifies_and_refuses_bad_values(tmp_path):
    seen = []

    async def changed(s):
        seen.append(s)
    store, client = settings_app(tmp_path, on_settings=changed)
    async with client as c:
        got = (await c.get("/api/settings")).json()
        assert got["settings"]["features"] == {"web_search": False, "image_generation": False,
                                                "multi_agent": False, "goals": False}
        assert got["feature_info"]["multi_agent"]["best_effort"] is True  # for the label on the screen
        r = await c.put("/api/settings", json={"disposable": True, "features": {"goals": True}})
        assert r.status_code == 200 and r.json()["disposable"] is True and r.json()["limits"]["five_hour"] == 80
        assert seen[-1] == r.json() and store.get() == r.json()
        r = await c.put("/api/settings", json={"disposable": "yes"})
        assert r.status_code == 400 and len(seen) == 1 and store.get()["disposable"] is True
        for host in ("evil:1",):
            assert (await c.put("/api/settings", json={"disposable": False}, headers={"Host": host})).status_code == 403
            assert (await c.get("/api/settings", headers={"Host": host})).status_code == 403
    assert store.get()["disposable"] is True


async def test_data_location_api_moves_through_the_callback_and_reports_why_not(tmp_path):
    moved = []

    async def move(path):
        if "bad" in str(path):
            raise ValueError("空のフォルダを指定してください")
        moved.append(path)
        return []
    _, client = settings_app(tmp_path, on_move=move)
    async with client as c:
        r = await c.post("/api/settings/data-root", json={"path": "D:/new place"})
        assert r.status_code == 200 and r.json()["moved"] is True and moved[0] == Path("D:/new place")
        # A successful move ends writes on this process until it restarts; use a fresh app for failure cases.
    _, client = settings_app(tmp_path / "retry", on_move=move)
    async with client as c:
        r = await c.post("/api/settings/data-root", json={"path": "D:/bad"})
        assert r.status_code == 400 and "空のフォルダ" in r.json()["detail"]
        assert (await c.post("/api/settings/data-root", json={"path": ""})).status_code == 400
        assert (await c.post("/api/settings/data-root", json={})).status_code == 422
        assert (await c.post("/api/settings/data-root", json={"path": "D:/x"}, headers={"Host": "evil:1"})).status_code == 403
    _, plain = settings_app(tmp_path / "other")
    async with plain as c:
        assert (await c.post("/api/settings/data-root", json={"path": "D:/x"})).status_code == 503


def test_v02_tool_events_keep_showing_as_tool_calls_and_the_rest_adds_no_rows(tmp_path):
    # the friend's page of v0.1 lists tool calls (under "files"); v0.2 sends tool_finished instead of tool_call.
    # The new kinds belong to the work log, not to this list.
    s = LogStore(tmp_path / "logs")
    s.set_visibility("files")
    for kind, data in [("tool_started", {"tool": "read_file", "arguments": {"path": "a.txt"}}),
                       ("tool_finished", {"tool": "read_file", "arguments": {"path": "a.txt"}, "success": True}),
                       ("plan", {"steps": []}), ("token_usage", {"input": 1, "output": 2}),
                       ("reasoning_summary", {"text": "考え"})]:
        s.add({"type": "event", "seq": 1, "task_id": "t1", "kind": kind, "data": data})
    assert [(e["kind"], e["text"]) for e in s.visible()] == [("tool_call", 'read_file {"path": "a.txt"}')]
    assert len(s.entries) == 1
