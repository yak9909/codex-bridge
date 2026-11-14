import httpx
import pytest

from codex_bridge.agent.logview import LogStore, create_logview_app


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
    t = httpx.ASGITransport(app=create_logview_app(store), client=("127.0.0.1", 1))
    async with httpx.AsyncClient(transport=t, base_url="http://127.0.0.1:8766") as c:
        yield c


async def test_logview_respects_visibility(client, store):
    store.set_visibility("summary")
    page = (await client.get("/api/events")).json()
    assert [e["kind"] for e in page["events"]] == ["task_prompt", "final_report"]
    store.set_visibility("files")
    kinds = [e["kind"] for e in (await client.get("/api/events")).json()["events"]]
    assert kinds == ["task_prompt", "tool_call", "final_report"]
    store.set_visibility("all")
    kinds = [e["kind"] for e in (await client.get("/api/events")).json()["events"]]
    assert "agent_message" in kinds and "state" in kinds


async def test_settings_message_sets_visibility(store):
    store.add({"type": "settings", "log_visibility": "all"})
    assert store.visibility == "all"


async def test_page_served(client):
    r = await client.get("/")
    assert r.status_code == 200 and "codex-bridge" in r.text


def test_store_persists_to_disk(tmp_path, store):
    assert len(LogStore(tmp_path / "logs").entries) == len(store.entries)
