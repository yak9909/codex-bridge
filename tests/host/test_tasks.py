import asyncio
import json

import pytest

from codex_bridge.common import protocol
from codex_bridge.host.paths import ToolError
from codex_bridge.host.tasks import TaskManager


class FakeAgent:
    def __init__(self, mgr: TaskManager, name: str = "friend"):
        self.mgr = mgr
        self.sent: list[dict] = []
        self.seq = 0
        self.link = mgr.attach(name, self.send)

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


# ---- what Codex is doing: new event kinds, and the half-written message (design 4.10, 6) ------------------
async def test_the_new_event_kinds_are_kept_as_events(tmp_path, mgr, agent):
    tid = await mgr.start("a")
    kinds = {"plan": {"steps": [{"step": "読む", "status": "inProgress"}]}, "reasoning_summary": {"text": "考え"},
             "tool_started": {"tool": "grep", "arguments": {"pattern": "x"}},
             "tool_finished": {"tool": "grep", "arguments": "{}", "duration_ms": 3, "success": True,
                               "result_summary": "ok"},
             "token_usage": {"input": 10, "output": 2}, "tool_call": {"tool": "grep", "status": "completed"}}
    for kind, data in kinds.items():
        await agent.push("event", task_id=tid, kind=kind, data=data)
    assert [(e["kind"], e["data"]) for e in mgr.events(tid)] == list(kinds.items())
    assert [e["kind"] for e in TaskManager(tmp_path / "data", touched_fn=lambda t: []).events(tid)] == list(kinds)


async def test_the_half_written_message_is_kept_in_memory_only(tmp_path, mgr, agent):
    tid = await mgr.start("a")
    seen = []
    mgr.on_change.append(lambda: seen.append(mgr.progress(tid)))
    assert mgr.progress(tid) is None
    await agent.push("event", task_id=tid, kind="progress", data={"text": "要点 1: 売"})
    await agent.push("event", task_id=tid, kind="progress", data={"text": "要点 1: 売上は"})  # only the latest counts
    assert mgr.progress(tid) == "要点 1: 売上は" and seen[-1] == "要点 1: 売上は"  # watchers are told
    assert mgr.events(tid) == [] and mgr.result(tid)["event_count"] == 0
    assert not (mgr.dir / f"{tid}.jsonl").exists() and "売上" not in (mgr.dir / f"{tid}.json").read_text(encoding="utf-8")
    assert TaskManager(tmp_path / "data", touched_fn=lambda t: []).progress(tid) is None
    assert agent.last("ack")["seq"] == agent.seq


async def test_the_half_written_message_goes_when_the_message_is_finished_or_the_task_ends(mgr, agent):
    tid = await mgr.start("a")
    await agent.push("event", task_id=tid, kind="progress", data={"text": "書きかけ"})
    await agent.push("event", task_id=tid, kind="agent_message", data={"text": "書き終えた"})
    assert mgr.progress(tid) is None and [e["kind"] for e in mgr.events(tid)] == ["agent_message"]
    await agent.push("event", task_id=tid, kind="progress", data={"text": "次の書きかけ"})
    await agent.push("task_state", task_id=tid, state="running", thread_id="th1")
    assert mgr.progress(tid) == "次の書きかけ"
    await agent.finish(tid)
    assert mgr.progress(tid) is None


@pytest.mark.parametrize("data", [None, "text", ["x"], {}, {"text": 5}, {"text": NAN}, {"text": None}])
async def test_a_half_written_message_that_is_not_text_is_ignored(mgr, agent, data):
    tid = await mgr.start("a")
    await agent.push("event", task_id=tid, kind="progress", data={"text": "ok"})
    await agent.push("event", task_id=tid, kind="progress", data=data)
    assert mgr.progress(tid) == "ok" and mgr.events(tid) == []
    as_json(mgr.progress(tid))


async def test_the_half_written_message_is_cut_to_a_size_the_page_can_hold(mgr, agent):
    tid = await mgr.start("a")
    await agent.push("event", task_id=tid, kind="progress", data={"text": "あ" * 100_000})
    assert len(mgr.progress(tid)) <= 20_000


async def test_the_half_written_message_of_another_lenders_task_is_ignored(mgr, agent):
    tid = await mgr.start("a")
    other = FakeAgent.__new__(FakeAgent)
    other.mgr, other.sent, other.seq = mgr, [], 0
    other.link = mgr.attach("someone", other.send)
    await other.push("hello", agent_version="0.1.0", codex_version="x", auth_mode="chatgpt")
    await other.push("event", task_id=tid, kind="progress", data={"text": "not yours"})
    assert mgr.progress(tid) is None


# ---- pre-approval by the lender (design 4.5, 6) ----------------------------------------------
def test_the_two_new_states_are_known_and_a_rejection_is_final():
    assert "awaiting_lender" in protocol.TASK_STATES and "rejected_by_lender" in protocol.TASK_STATES
    assert "rejected_by_lender" in protocol.FINAL_STATES and "awaiting_lender" not in protocol.FINAL_STATES


async def test_awaiting_lender_is_kept_and_counts_as_working(mgr, agent):
    tid = await mgr.start("a")
    await agent.push("task_state", task_id=tid, state="awaiting_lender")
    assert mgr.status(tid)["state"] == "awaiting_lender"
    assert mgr.current_task_id() == tid and mgr.is_active(tid)
    assert mgr.lenders()[0]["current_task_id"] == tid


async def test_a_new_task_is_refused_while_the_lender_has_one_awaiting_approval(mgr, agent):
    tid2 = await mgr.start("first")
    await agent.finish(tid2)  # a finished one with a session to continue
    tid = await mgr.start("a")
    await agent.push("task_state", task_id=tid, state="awaiting_lender")
    with pytest.raises(ToolError, match="作業中"):
        await mgr.start("b")  # to that lender by name
    with pytest.raises(ToolError, match="作業中"):
        await mgr.start("b", lender="friend")
    with pytest.raises(ToolError, match="作業中"):
        await mgr.start("b", continue_task_id=tid2)
    assert len([m for m in agent.sent if m["type"] == "task_start"]) == 2  # the two that were allowed


async def test_automatic_routing_skips_the_lender_awaiting_approval(mgr, agent):
    other = FakeAgent(mgr, "zed")
    await other.push("hello", agent_version="0.2", codex_version="x", auth_mode="chatgpt")
    first = await mgr.start("a", lender="friend")
    await agent.push("task_state", task_id=first, state="awaiting_lender")
    second = await mgr.start("b")
    assert mgr.tasks[second].lender == "zed"


async def test_a_rejection_is_final_carries_the_reason_and_frees_the_lender(mgr, agent):
    tid = await mgr.start("a")
    await agent.push("task_state", task_id=tid, state="awaiting_lender")
    await agent.push("task_state", task_id=tid, state="rejected_by_lender", report="拒否されました: 今は困る")
    r = mgr.result(tid)
    assert r["state"] == "rejected_by_lender" and r["report"] == "拒否されました: 今は困る" and r["finished"]
    assert mgr.current_task_id() is None and not mgr.is_active(tid)
    assert await mgr.start("b")  # the lender is free again
    with pytest.raises(ToolError, match="作業中ではありません"):
        await mgr.followup(tid, "x")


async def test_a_task_awaiting_approval_can_still_be_cancelled_but_not_given_more_instructions(mgr, agent):
    tid = await mgr.start("a")
    await agent.push("task_state", task_id=tid, state="awaiting_lender")
    await mgr.cancel(tid)
    assert agent.last("task_cancel")["task_id"] == tid
    with pytest.raises(ToolError, match="承認待ち"):
        await mgr.followup(tid, "also")
    assert not [m for m in agent.sent if m["type"] == "task_followup"]


async def test_a_pending_approval_survives_a_disconnect_and_the_resent_state(mgr, agent):
    tid = await mgr.start("a")
    await agent.push("task_state", task_id=tid, state="awaiting_lender")
    mgr.detach(agent.link)
    assert mgr.status(tid)["state"] == "agent_offline" and mgr.current_task_id() == tid
    agent2 = FakeAgent(mgr)
    agent2.seq = 100
    await agent2.push("hello", agent_version="x", codex_version="x", auth_mode="chatgpt", current_task_id=tid)
    assert mgr.status(tid)["state"] == "awaiting_lender"  # not failed, not running
    await agent2.push("task_state", task_id=tid, state="awaiting_lender")  # the agent says it again
    assert mgr.status(tid)["state"] == "awaiting_lender"
    await agent2.push("task_state", task_id=tid, state="running", thread_id="th1")
    assert mgr.status(tid)["state"] == "running"


async def test_a_pending_approval_is_still_pending_after_the_host_restarts(tmp_path, mgr, agent):
    tid = await mgr.start("a")
    await agent.push("task_state", task_id=tid, state="awaiting_lender")
    mgr2 = TaskManager(tmp_path / "data", touched_fn=lambda t: [])
    assert mgr2.status(tid)["state"] == "agent_offline"
    a2 = FakeAgent(mgr2)
    await a2.push("hello", agent_version="x", codex_version="x", auth_mode="chatgpt", current_task_id=tid)
    assert mgr2.status(tid)["state"] == "awaiting_lender"


# ---- a task that is over stays over; a hello about the settings is not about the task ----------
@pytest.mark.parametrize("final", ["completed", "failed", "cancelled", "rejected_by_lender"])
async def test_a_final_state_is_not_left_again(mgr, agent, final):
    tid = await mgr.start("a")
    await agent.push("task_state", task_id=tid, state=final, thread_id="th1", report="first word")
    stamp = mgr.result(tid)["finished"]
    for later in ("running", "queued", "awaiting_lender", "agent_offline", "completed", "failed"):
        if later == final:
            continue
        await agent.push("task_state", task_id=tid, state=later, thread_id="th2", report="late")
        r = mgr.result(tid)
        assert r["state"] == final and r["report"] == "first word" and r["thread_id"] == "th1"
        assert r["finished"] == stamp
    assert mgr.current_task_id() is None
    assert agent.last("ack")["seq"] == agent.seq  # still acknowledged, or the agent would send it for ever


async def test_a_failed_task_does_not_flicker_back_to_running_after_a_restart_of_the_host(tmp_path, mgr, agent):
    tid = await mgr.start("a")
    await agent.push("task_state", task_id=tid, state="failed", report="gave up")
    mgr2 = TaskManager(tmp_path / "data", touched_fn=lambda t: [])
    a2 = FakeAgent(mgr2)
    await a2.push("task_state", seq=3, task_id=tid, state="running")  # an old message, sent again by the agent
    assert mgr2.status(tid)["state"] == "failed" and mgr2.current_task_id() is None


async def test_hello_without_current_task_id_leaves_a_queued_task_alone(mgr, agent):
    # The agent says hello again when its settings change; if that lands just as a task starts, the task must
    # not be taken for one the agent lost, and the lender must not look free (a second task would be sent to it).
    tid = await mgr.start("a")
    await agent.push("hello", agent_version="x", codex_version="x", auth_mode="chatgpt",
                     protocol=2, capabilities={"resumable": False, "pre_approval": True})
    assert mgr.status(tid)["state"] == "queued" and mgr.current_task_id() == tid
    assert mgr.lenders()[0]["current_task_id"] == tid
    assert mgr.agent_info()["capabilities"] == {"resumable": False, "pre_approval": True, "model_select": False}  # it did take effect
    with pytest.raises(ToolError, match="作業中"):
        await mgr.start("b")
    await agent.push("task_state", task_id=tid, state="running", thread_id="th1")
    assert mgr.status(tid)["state"] == "running"


async def test_a_result_sent_again_before_hello_is_not_taken_away_by_the_hello_after_it(mgr, agent):
    # The task ended while the link was down; the agent sends what the host missed, then hello (no task any more).
    tid = await mgr.start("a")
    await agent.push("task_state", task_id=tid, state="running", thread_id="th1")
    mgr.detach(agent.link)
    agent2 = FakeAgent(mgr)
    await agent2.push("task_state", seq=50, task_id=tid, state="completed", thread_id="th1", report="できました")
    await agent2.push("hello", agent_version="x", codex_version="x", auth_mode="chatgpt", current_task_id=None)
    r = mgr.result(tid)
    assert r["state"] == "completed" and r["report"] == "できました"


async def test_hello_naming_a_task_whose_state_the_agent_already_said_leaves_it_there(mgr, agent):
    tid = await mgr.start("a")
    await agent.push("task_state", task_id=tid, state="awaiting_lender")
    mgr.detach(agent.link)
    agent2 = FakeAgent(mgr)
    await agent2.push("task_state", seq=50, task_id=tid, state="awaiting_lender")  # sent again, before hello
    await agent2.push("hello", agent_version="x", codex_version="x", auth_mode="chatgpt", current_task_id=tid)
    assert mgr.status(tid)["state"] == "awaiting_lender"  # not "running"


# ---- a v0.1 agent says hello first and sends what the host missed after it ----------------------------------
async def test_v01_order_hello_first_then_the_resent_result_still_ends_as_completed(mgr, agent):
    tid = await mgr.start("a")
    await agent.push("task_state", task_id=tid, state="running", thread_id="th1")
    mgr.detach(agent.link)  # the task ends while the link is down
    agent2 = FakeAgent(mgr)
    agent2.seq = 100
    await agent2.push("hello", agent_version="0.1.0", codex_version="x", auth_mode="chatgpt", current_task_id=None)
    assert mgr.status(tid)["state"] == "failed"  # all the host knows at this moment
    await agent2.push("task_state", task_id=tid, state="running", thread_id="th1")  # resent in the order sent
    assert mgr.status(tid)["state"] == "failed"
    await agent2.push("task_state", task_id=tid, state="completed", thread_id="th1", report="できました")
    r = mgr.result(tid)
    assert r["state"] == "completed" and r["report"] == "できました" and r["finished"]
    assert "再起動" not in r["report"] and mgr.current_task_id() is None
    await agent2.push("task_state", task_id=tid, state="failed", report="late")  # now it is a genuine final
    assert mgr.status(tid)["state"] == "completed"


@pytest.mark.parametrize("final", ["completed", "cancelled", "suspended_quota", "rejected_by_lender", "failed"])
async def test_any_final_state_from_the_agent_replaces_the_host_s_own_guess(mgr, agent, final):
    tid = await mgr.start("a")
    await agent.push("task_state", task_id=tid, state="running")
    mgr.detach(agent.link)
    agent2 = FakeAgent(mgr)
    agent2.seq = 100
    await agent2.push("hello", agent_version="0.1.0", codex_version="x", auth_mode="chatgpt", current_task_id=None)
    await agent2.push("task_state", task_id=tid, state=final, thread_id="th1", report="the real one")
    r = mgr.result(tid)
    assert r["state"] == final and r["report"] == "the real one"


async def test_the_host_s_guess_stays_when_the_agent_never_says_more_and_survives_a_restart(tmp_path, mgr, agent):
    tid = await mgr.start("a")
    await agent.push("task_state", task_id=tid, state="running")
    mgr.detach(agent.link)
    agent2 = FakeAgent(mgr)
    await agent2.push("hello", agent_version="0.1.0", codex_version="x", auth_mode="chatgpt", current_task_id=None)
    assert "再起動" in mgr.result(tid)["report"]
    mgr2 = TaskManager(tmp_path / "data", touched_fn=lambda t: [])  # the guess is still a guess after a restart
    a3 = FakeAgent(mgr2)
    await a3.push("task_state", seq=9, task_id=tid, state="completed", report="done after all")
    assert mgr2.status(tid)["state"] == "completed" and mgr2.result(tid)["report"] == "done after all"


async def test_a_failed_the_agent_reported_itself_is_not_a_guess(mgr, agent):
    tid = await mgr.start("a")
    await agent.push("task_state", task_id=tid, state="failed", report="Codex crashed")
    await agent.push("task_state", task_id=tid, state="completed", report="no")
    assert mgr.status(tid)["state"] == "failed" and mgr.result(tid)["report"] == "Codex crashed"


async def test_a_task_in_the_other_lenders_hello_is_not_guessed_failed_at_all(mgr, agent):
    tid = await mgr.start("a", lender="friend")
    other = FakeAgent(mgr, "zed")
    await other.push("hello", agent_version="0.1.0", codex_version="x", auth_mode="chatgpt", current_task_id=None)
    assert mgr.status(tid)["state"] == "queued"
