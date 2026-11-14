import asyncio

import pytest

from codex_bridge.host.paths import ToolError
from codex_bridge.host.tasks import TaskManager


class FakeAgent:
    def __init__(self, mgr: TaskManager):
        self.mgr = mgr
        self.sent: list[dict] = []
        self.seq = 0
        self.link = mgr.attach(self.send)

    async def send(self, msg: dict) -> None:
        self.sent.append(msg)

    def last(self, type_: str) -> dict:
        return [m for m in self.sent if m["type"] == type_][-1]

    async def push(self, type_: str, **fields) -> None:
        if type_ in ("event", "task_state"):
            self.seq += 1
            fields.setdefault("seq", self.seq)
        await self.mgr.on_agent_message(self.link, {"type": type_, **fields})

    async def finish(self, task_id: str, thread_id: str = "th1", report: str = "done") -> None:
        await self.push("task_state", task_id=task_id, state="completed", thread_id=thread_id, report=report)


@pytest.fixture
def mgr(tmp_path):
    return TaskManager(tmp_path / "data", touched_fn=lambda tid: ["out/x.txt"] if tid else [])


@pytest.fixture
def agent(mgr):
    return FakeAgent(mgr)


def usage(five_hour=None, weekly=None, resets_at=1_800_000_000):
    w = lambda p: None if p is None else {"used_percent": p, "resets_at": resets_at}
    return {"five_hour": w(five_hour), "weekly": w(weekly), "ts": 0}


async def test_start_sends_task_and_tracks_state(mgr, agent):
    tid = await mgr.start("do it")
    m = agent.last("task_start")
    assert m["task_id"] == tid and m["prompt"] == "do it" and m["continue_thread_id"] is None
    assert mgr.status(tid)["state"] == "queued" and mgr.current_task_id() == tid
    await agent.push("task_state", task_id=tid, state="running", thread_id="th1")
    assert mgr.status(tid)["state"] == "running"


async def test_no_agent_is_clear_error(mgr):
    with pytest.raises(ToolError, match="接続されていません"):
        await mgr.start("a")


async def test_one_task_at_a_time(mgr, agent):
    await mgr.start("a")
    with pytest.raises(ToolError, match="作業中"):
        await mgr.start("b")


async def test_quota_blocks_new_tasks(mgr, agent):
    await agent.push("usage", **usage(five_hour=95))
    with pytest.raises(ToolError, match="再開可能"):
        await mgr.start("a")


async def test_quota_threshold_is_configurable(mgr, agent):
    await agent.push("usage", **usage(five_hour=95))
    await mgr.set_thresholds(five_hour=99, weekly=None)
    assert await mgr.start("a")
    assert agent.last("settings")["thresholds"] == {"five_hour": 99, "weekly": 95}


async def test_completed_task_result(mgr, agent):
    tid = await mgr.start("a")
    await agent.push("event", task_id=tid, kind="agent_message", data={"text": "hello"})
    await agent.finish(tid, report="all done")
    r = mgr.result(tid)
    assert r["state"] == "completed" and r["report"] == "all done" and r["changed_files"] == ["out/x.txt"]
    assert mgr.current_task_id() is None


async def test_suspended_quota_records_resume_time(mgr, agent):
    tid = await mgr.start("a")
    await agent.push("task_state", task_id=tid, state="suspended_quota", thread_id="th1",
                     report="途中まで", resume_at=1_800_000_000)
    s = mgr.status(tid)
    assert s["state"] == "suspended_quota" and s["resume_at"].startswith("2027-")
    assert mgr.current_task_id() is None


async def test_duplicate_seq_ignored_and_acked(mgr, agent):
    tid = await mgr.start("a")
    await agent.push("event", seq=7, task_id=tid, kind="agent_message", data={"text": "x"})
    await agent.push("event", seq=7, task_id=tid, kind="agent_message", data={"text": "x"})
    assert len([e for e in mgr.events(tid) if e["kind"] == "agent_message"]) == 1
    assert [m["seq"] for m in agent.sent if m["type"] == "ack"] == [7, 7]


async def test_disconnect_marks_agent_offline_and_reconnect_restores(mgr, agent):
    tid = await mgr.start("a")
    await agent.push("task_state", task_id=tid, state="running", thread_id="th1")
    mgr.detach(agent.link)
    assert mgr.status(tid)["state"] == "agent_offline"
    agent2 = FakeAgent(mgr)
    agent2.seq = 100
    await agent2.push("task_state", task_id=tid, state="running", thread_id="th1")
    assert mgr.status(tid)["state"] == "running"


async def test_continue_task_passes_thread_id(mgr, agent):
    t1 = await mgr.start("a")
    await agent.finish(t1, thread_id="th1")
    await mgr.start("b", continue_task_id=t1)
    assert agent.last("task_start")["continue_thread_id"] == "th1"


async def test_continue_unknown_task(mgr, agent):
    with pytest.raises(ToolError, match="見つかりません"):
        await mgr.start("b", continue_task_id="nope")


async def test_followup_and_cancel_forwarded(mgr, agent):
    tid = await mgr.start("a")
    await mgr.followup(tid, "also this")
    assert agent.last("task_followup") == {"type": "task_followup", "task_id": tid, "text": "also this"}
    await mgr.cancel(tid)
    assert agent.last("task_cancel")["task_id"] == tid


async def test_followup_on_finished_task_refused(mgr, agent):
    tid = await mgr.start("a")
    await agent.finish(tid)
    with pytest.raises(ToolError, match="作業中ではありません"):
        await mgr.followup(tid, "x")


async def test_session_request_roundtrip(mgr, agent):
    async def reply_later():
        await asyncio.sleep(0.05)
        req = agent.last("session_list")
        await agent.push("reply", req_id=req["req_id"], ok=True, data=[{"thread_id": "th1"}])
    asyncio.create_task(reply_later())
    assert await mgr.sessions_list() == [{"thread_id": "th1"}]


async def test_session_request_error(mgr, agent):
    async def reply_later():
        await asyncio.sleep(0.05)
        req = agent.last("session_delete")
        await agent.push("reply", req_id=req["req_id"], ok=False, error="このツールで作ったセッションではありません")
    asyncio.create_task(reply_later())
    with pytest.raises(ToolError, match="このツールで作った"):
        await mgr.session_delete("other")


async def test_tasks_survive_restart(tmp_path, mgr, agent):
    tid = await mgr.start("a")
    await agent.finish(tid, report="saved")
    mgr2 = TaskManager(tmp_path / "data", touched_fn=lambda tid: [])
    assert mgr2.result(tid)["report"] == "saved"


async def test_hello_triggers_settings(mgr, agent):
    await agent.push("hello", agent_version="0.1.0", codex_version="0.167.0", auth_mode="chatgpt")
    assert agent.last("settings")["log_visibility"] == "summary"
    assert mgr.agent_info()["auth_mode"] == "chatgpt"


async def test_restarted_agent_without_task_fails_orphan(mgr, agent):
    # Review I4: an agent that restarted no longer has the task; the host must not stay stuck.
    tid = await mgr.start("a")
    await agent.push("task_state", task_id=tid, state="running", thread_id="th1")
    mgr.detach(agent.link)
    agent2 = FakeAgent(mgr)
    await agent2.push("hello", agent_version="x", codex_version="x", auth_mode="chatgpt", current_task_id=None)
    assert mgr.status(tid)["state"] == "failed"
    assert await mgr.start("b")


async def test_reconnected_agent_with_task_restores_state(mgr, agent):
    tid = await mgr.start("a")
    await agent.push("task_state", task_id=tid, state="running", thread_id="th1")
    mgr.detach(agent.link)
    agent2 = FakeAgent(mgr)
    await agent2.push("hello", agent_version="x", codex_version="x", auth_mode="chatgpt", current_task_id=tid)
    assert mgr.status(tid)["state"] == "running"


async def test_usage_shows_friend_limits(mgr, agent):
    await agent.push("usage", **usage(five_hour=10), friend_limits={"five_hour": 70, "weekly": None, "force": True})
    assert mgr.usage()["friend_limits"] == {"five_hour": 70, "weekly": None, "force": True}
