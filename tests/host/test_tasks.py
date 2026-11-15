import asyncio
import json

import pytest

from codex_bridge.common import protocol
from codex_bridge.host.paths import ToolError
from codex_bridge.host.tasks import TaskManager


class FakeAgent:
    def __init__(self, mgr: TaskManager):
        self.mgr = mgr
        self.sent: list[dict] = []
        self.seq = 0
        self.link = mgr.attach("friend", self.send)

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
async def agent(mgr):
    a = FakeAgent(mgr)  # a real agent says hello first thing; only then does the host give it tasks
    await a.push("hello", agent_version="0.1.0", codex_version="x", auth_mode="chatgpt")
    return a


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


# ---- what an agent sends about a task is checked before it is kept ---------------------------
# (review I1: one NaN in a state or an event kept the task list of the admin page at an error, also after a restart)
NAN, INF = float("nan"), float("inf")


def as_json(value):
    """Fails the way the admin API does when a value cannot be sent as JSON."""
    return json.loads(json.dumps(value, allow_nan=False))


def everything(mgr: TaskManager, tid: str):
    return as_json([mgr.list_tasks(), mgr.result(tid), mgr.status(tid), mgr.events(tid), mgr.lenders(), mgr.usage()])


@pytest.mark.parametrize("state", [NAN, INF, None, 5, True, "", "done", "RUNNING", ["running"], {"s": "running"}])
async def test_task_state_with_an_unknown_state_is_ignored(mgr, agent, state):
    tid = await mgr.start("a")
    await agent.push("task_state", task_id=tid, state="running", thread_id="th1")
    await agent.push("task_state", task_id=tid, state=state, thread_id="th2", report="not this")
    assert mgr.status(tid)["state"] == "running" and mgr.result(tid)["thread_id"] == "th1"
    assert mgr.result(tid)["report"] is None and mgr.current_task_id() == tid
    assert everything(mgr, tid)[0][0]["state"] == "running"
    assert agent.last("ack")["seq"] == agent.seq  # acknowledged all the same, or it would be sent again for ever
    assert "th2" not in (mgr.dir / f"{tid}.json").read_text(encoding="utf-8")


async def test_task_state_keeps_only_values_of_the_right_kind(mgr, agent):
    tid = await mgr.start("a")
    await agent.push("task_state", task_id=tid, state="running", thread_id="th1", report="so far")
    await agent.push("task_state", task_id=tid, state="suspended_quota", thread_id=NAN, report={"x": NAN}, resume_at=INF)
    r = mgr.result(tid)
    assert (r["state"], r["thread_id"], r["report"], r["resume_at"]) == ("suspended_quota", "th1", "so far", None)
    for bad in (NAN, "soon", 1e300, True, [1]):
        await agent.push("task_state", task_id=tid, state="suspended_quota", resume_at=bad)
        assert mgr.result(tid)["resume_at"] is None and mgr.status(tid)["resume_at"] is None
    await agent.push("task_state", task_id=tid, state="suspended_quota", resume_at=1_800_000_000)
    assert mgr.result(tid)["resume_at"].startswith("2027-01-1")
    everything(mgr, tid)


@pytest.mark.parametrize("task_id", [NAN, None, 5, ["t"], {"t": 1}])
async def test_messages_about_no_task_in_words_are_ignored(mgr, agent, task_id):
    tid = await mgr.start("a")
    await agent.push("event", task_id=task_id, kind="status", data={"text": "x"})
    await agent.push("task_state", task_id=task_id, state="completed", report="x")
    assert mgr.events(tid) == [] and mgr.status(tid)["state"] == "queued"


async def test_event_data_is_kept_without_the_numbers_json_cannot_carry(tmp_path, mgr, agent):
    tid = await mgr.start("a")
    await agent.push("event", task_id=tid, kind="status", data={"text": NAN, "more": [INF, {"deep": -INF}], "ok": 1.5})
    # the same from the wire: 1e999 is a number JSON allows, and it reads as infinity
    wire = ('{"type": "event", "seq": 99, "task_id": "%s", "kind": "token_usage", '
            '"data": {"input": 1e999, "output": NaN, "note": [-Infinity]}}' % tid)
    await mgr.on_agent_message(agent.link, protocol.decode(wire))
    assert [e["data"] for e in mgr.events(tid)] == [
        {"text": None, "more": [None, {"deep": None}], "ok": 1.5}, {"input": None, "output": None, "note": [None]}]
    everything(mgr, tid)
    on_disk = (mgr.dir / f"{tid}.jsonl").read_text(encoding="utf-8")
    assert "NaN" not in on_disk and "Infinity" not in on_disk
    again = TaskManager(tmp_path / "data", touched_fn=lambda t: [])
    assert [e["data"] for e in again.events(tid)] == [e["data"] for e in mgr.events(tid)]


@pytest.mark.parametrize("kind", [NAN, None, 5, "", ["status"], {"k": "status"}])
async def test_event_without_a_kind_in_words_is_dropped(mgr, agent, kind):
    tid = await mgr.start("a")
    await agent.push("event", task_id=tid, kind=kind, data={"text": "x"})
    assert mgr.events(tid) == [] and agent.last("ack")["seq"] == agent.seq


async def test_records_spoilt_before_these_checks_do_not_break_the_lists(tmp_path):
    # Written by a host that kept whatever an agent sent (NaN as the state, the report, the times),
    # or cut off while being written: the host must start, and every list must still be JSON.
    tasks = tmp_path / "data" / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "bad.json").write_text(
        '{"task_id": "bad", "prompt": "p", "created": 5.0, "state": NaN, "thread_id": NaN, "report": Infinity, '
        '"resume_at": "soon", "continue_task_id": null, "state_before_offline": NaN, "lender": "alice", '
        '"finished": NaN, "added_by_a_later_version": 1}', encoding="utf-8")
    (tasks / "bad.jsonl").write_text(
        '{"ts": 1.0, "kind": "status", "data": {"text": NaN}}\nnot json\n[1]\n'
        '{"ts": NaN, "kind": "agent_message", "data": {"text": "hi"}}\n', encoding="utf-8")
    (tasks / "times.json").write_text(
        '{"task_id": "times", "prompt": "p", "created": NaN, "state": "completed", "resume_at": Infinity}', encoding="utf-8")
    (tasks / "torn.json").write_text('{"task_id": "torn", "prom', encoding="utf-8")
    (tasks / "list.json").write_text("[1, 2]", encoding="utf-8")
    (tasks / "nameless.json").write_text('{"task_id": NaN, "prompt": "p", "created": 1.0, "state": "completed"}', encoding="utf-8")
    (tasks / "good.json").write_text(
        '{"task_id": "good", "prompt": "p", "created": 9.0, "state": "completed", "report": "ok", "thread_id": "th"}',
        encoding="utf-8")
    mgr = TaskManager(tmp_path / "data", touched_fn=lambda t: [])
    assert [t["task_id"] for t in as_json(mgr.list_tasks())] == ["good", "bad", "times"]  # newest first
    bad = as_json(mgr.result("bad"))
    assert bad["state"] == "failed" and "記録が壊れて" in bad["report"]  # its real state is not known any more
    assert (bad["thread_id"], bad["resume_at"], bad["finished"], bad["lender"]) == (None, None, None, "alice")
    assert as_json(mgr.events("bad")) == [{"ts": 1.0, "kind": "status", "data": {"text": None}},
                                          {"ts": None, "kind": "agent_message", "data": {"text": "hi"}}]
    assert as_json(mgr.result("times"))["created"] == 0 and mgr.result("times")["state"] == "completed"
    assert mgr.result("good")["report"] == "ok" and mgr.result("good")["thread_id"] == "th"
    for tid in ("good", "bad", "times"):
        everything(mgr, tid)
    assert (tasks / "torn.json").exists()  # nothing is deleted
    # and the host works on: a new task, and the answer of an agent to the spoilt one
    async def drop(m):
        pass
    link = mgr.attach("alice", drop)
    await mgr.on_agent_message(link, {"type": "hello", "agent_version": "0.2.0", "codex_version": "x", "auth_mode": "chatgpt"})
    await mgr.on_agent_message(link, {"type": "task_state", "seq": 1, "task_id": "bad", "state": "completed",
                                      "thread_id": "th9", "report": "done after all"})
    assert mgr.result("bad")["state"] == "completed" and mgr.result("bad")["report"] == "done after all"
    assert await mgr.start("next")
    assert '"state": "completed"' in (tasks / "bad.json").read_text(encoding="utf-8")
