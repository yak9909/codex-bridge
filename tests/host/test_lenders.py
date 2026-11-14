import asyncio
import time

import pytest

from codex_bridge.host.lenders import Lender, pick_lender
from codex_bridge.host.paths import ToolError
from codex_bridge.host.tasks import TaskManager

TH = {"five_hour": 80, "weekly": 80}
RETRY_ADVICE = "前回の報告を添えて新しく依頼してください"
NOT_READY = "まだ準備ができていません"


def win(percent):
    return None if percent is None else {"used_percent": percent, "resets_at": int(time.time()) + 3600}


def usage(five_hour=None, weekly=None):
    return {"five_hour": win(five_hour), "weekly": win(weekly), "ts": 0}


def lender(name, five_hour=None, weekly=None, **kw):
    kw.setdefault("connected", True)
    kw.setdefault("ready", True)
    return Lender(name=name, usage=usage(five_hour, weekly), **kw)


# ---- pick_lender (design §3.3) ------------------------------------------
def test_pick_most_headroom():
    a, b = lender("A", five_hour=70), lender("B", five_hour=20)
    assert pick_lender([a, b], TH) is b
    assert pick_lender([b, a], TH) is b


def test_pick_counts_the_tighter_window():
    # A has lots of 5-hour room but little weekly room; the smaller one decides.
    a, b = lender("A", five_hour=5, weekly=75), lender("B", five_hour=40, weekly=40)
    assert pick_lender([a, b], TH) is b


def test_pick_uses_lower_of_host_and_friend_limits():
    # A: own limit 40 -> room 10. B: host limit 80 -> room 30.
    a = lender("A", five_hour=30, limits={"five_hour": 40, "weekly": None, "force": False})
    b = lender("B", five_hour=50)
    assert pick_lender([a, b], TH) is b
    # A lender's own limit never raises the host's: C room is 80-60=20, not 100-60=40.
    c = lender("C", five_hour=60, limits={"five_hour": 100, "weekly": 100, "force": False})
    assert pick_lender([c, b], TH) is b


def test_pick_skips_busy_offline_blocked():
    busy = lender("busy", five_hour=0, current_task_id="t1")
    offline = lender("offline", five_hour=0, connected=False)
    blocked = lender("blocked", five_hour=85)
    blocked_by_own_limit = lender("own", five_hour=50, limits={"five_hour": 40, "weekly": None, "force": False})
    no_hello_yet = lender("new", five_hour=0, ready=False)
    ok = lender("ok", five_hour=79)
    assert pick_lender([busy, offline, blocked, blocked_by_own_limit, no_hello_yet, ok], TH) is ok
    assert pick_lender([busy, offline, blocked, blocked_by_own_limit, no_hello_yet], TH) is None
    assert pick_lender([], TH) is None


def test_unknown_usage_goes_last():
    unknown, known = lender("unknown"), lender("known", five_hour=79)
    assert pick_lender([unknown, known], TH) is known
    assert pick_lender([unknown], TH) is unknown


def test_same_headroom_prefers_least_recently_assigned():
    a, b = lender("A", five_hour=30, last_assigned=200.0), lender("B", five_hour=30, last_assigned=100.0)
    assert pick_lender([a, b], TH) is b


# ---- TaskManager with several lenders -----------------------------------
HELLO = {"agent_version": "0.1.0", "codex_version": "x", "auth_mode": "chatgpt"}


class FakeAgent:
    def __init__(self, mgr: TaskManager, name: str):
        self.mgr = mgr
        self.name = name
        self.sent: list[dict] = []
        self.link = mgr.attach(name, self.send)

    @classmethod
    async def join(cls, mgr: TaskManager, name: str, **hello) -> "FakeAgent":
        """Connect and say hello, as a real agent does first thing: only then is the lender given tasks."""
        agent = cls(mgr, name)
        await agent.push("hello", **{**HELLO, **hello})
        return agent

    async def send(self, msg: dict) -> None:
        self.sent.append(msg)

    def all(self, type_: str) -> list[dict]:
        return [m for m in self.sent if m["type"] == type_]

    def last(self, type_: str) -> dict:
        return self.all(type_)[-1]

    async def push(self, type_: str, **fields) -> None:
        await self.mgr.on_agent_message(self.link, {"type": type_, **fields})

    async def finish(self, task_id: str, seq: int, thread_id: str = "th1", report: str = "done") -> None:
        await self.push("task_state", seq=seq, task_id=task_id, state="completed", thread_id=thread_id, report=report)


@pytest.fixture
def mgr(tmp_path):
    m = TaskManager(tmp_path / "data", touched_fn=lambda tid: [])
    m.settings["thresholds"] = dict(TH)
    return m


async def test_two_lenders_work_in_parallel(mgr):
    alice, bob = await FakeAgent.join(mgr, "alice"), await FakeAgent.join(mgr, "bob")
    await alice.push("usage", **usage(five_hour=10))
    await bob.push("usage", **usage(five_hour=50))
    t1 = await mgr.start("one")
    t2 = await mgr.start("two")
    assert alice.last("task_start")["task_id"] == t1 and bob.last("task_start")["task_id"] == t2
    assert mgr.tasks[t1].lender == "alice" and mgr.tasks[t2].lender == "bob"
    assert mgr.current_task_id("alice") == t1 and mgr.current_task_id("bob") == t2
    assert mgr.current_task_id() in (t1, t2)
    with pytest.raises(ToolError, match="作業中"):
        await mgr.start("three")

    await mgr.followup(t2, "more")
    assert bob.last("task_followup")["task_id"] == t2 and not alice.all("task_followup")
    await mgr.cancel(t1)
    assert alice.last("task_cancel")["task_id"] == t1 and not bob.all("task_cancel")

    await alice.finish(t1, seq=1)
    assert mgr.current_task_id("alice") is None and mgr.current_task_id("bob") == t2
    t3 = await mgr.start("three")
    assert mgr.tasks[t3].lender == "alice"

    mgr.detach(bob.link)  # only bob's task goes offline
    assert mgr.status(t2)["state"] == "agent_offline" and mgr.status(t3)["state"] == "queued"


async def test_explicit_lender_is_used(mgr):
    alice, bob = await FakeAgent.join(mgr, "alice"), await FakeAgent.join(mgr, "bob")
    await alice.push("usage", **usage(five_hour=10))
    await bob.push("usage", **usage(five_hour=50))
    tid = await mgr.start("one", lender="bob")
    assert bob.last("task_start")["task_id"] == tid and not alice.all("task_start")
    with pytest.raises(ToolError, match="作業中"):
        await mgr.start("two", lender="bob")
    with pytest.raises(ToolError, match="接続されていません"):
        await mgr.start("two", lender="carol")
    mgr.detach(alice.link)
    with pytest.raises(ToolError, match="接続されていません"):
        await mgr.start("two", lender="alice")


async def test_explicit_lender_over_its_limit_is_refused(mgr):
    alice, bob = await FakeAgent.join(mgr, "alice"), await FakeAgent.join(mgr, "bob")
    await alice.push("usage", **usage(five_hour=10))
    await bob.push("usage", **usage(five_hour=95))
    with pytest.raises(ToolError, match="再開可能"):
        await mgr.start("one", lender="bob")
    assert mgr.tasks[await mgr.start("one")].lender == "alice"  # automatic routing skips bob
    with pytest.raises(ToolError, match="再開可能"):  # alice works, bob is over: say when bob comes back
        await mgr.start("two")


async def test_same_name_reconnect_replaces_the_link(mgr):
    old = await FakeAgent.join(mgr, "alice")
    new = await FakeAgent.join(mgr, "alice")
    tid = await mgr.start("one")
    assert new.last("task_start")["task_id"] == tid and not old.all("task_start")
    mgr.detach(old.link)  # the stale connection closing must not take the new one down
    assert mgr.status(tid)["state"] == "queued" and len(mgr.lenders()) == 1


async def test_seq_dedupe_is_per_lender(mgr):
    alice, bob = await FakeAgent.join(mgr, "alice"), await FakeAgent.join(mgr, "bob")
    t1 = await mgr.start("one", lender="alice")
    t2 = await mgr.start("two", lender="bob")
    await alice.push("event", seq=7, task_id=t1, kind="agent_message", data={"text": "a"})
    await bob.push("event", seq=7, task_id=t2, kind="agent_message", data={"text": "b"})
    await bob.push("event", seq=7, task_id=t2, kind="agent_message", data={"text": "b"})
    assert [e["data"]["text"] for e in mgr.events(t1)] == ["a"]
    assert [e["data"]["text"] for e in mgr.events(t2)] == ["b"]
    assert [m["seq"] for m in alice.all("ack")] == [7] and [m["seq"] for m in bob.all("ack")] == [7, 7]


async def test_messages_about_another_lenders_task_are_ignored(mgr):
    alice, bob = await FakeAgent.join(mgr, "alice"), await FakeAgent.join(mgr, "bob")
    t1 = await mgr.start("one", lender="alice")
    await bob.push("event", seq=1, task_id=t1, kind="agent_message", data={"text": "not mine"})
    await bob.push("task_state", seq=2, task_id=t1, state="completed", thread_id="thB", report="hijack")
    assert mgr.events(t1) == [] and mgr.status(t1)["state"] == "queued" and mgr.result(t1)["report"] is None


async def test_continue_goes_to_same_lender(mgr):
    alice, bob = await FakeAgent.join(mgr, "alice"), await FakeAgent.join(mgr, "bob")
    await alice.push("usage", **usage(five_hour=60))
    await bob.push("usage", **usage(five_hour=10))
    t1 = await mgr.start("one", lender="alice")
    await alice.finish(t1, seq=1, thread_id="thA")
    t2 = await mgr.start("more", continue_task_id=t1)  # bob has more room, but the session lives at alice
    assert alice.last("task_start")["task_id"] == t2 and alice.last("task_start")["continue_thread_id"] == "thA"
    assert not bob.all("task_start") and mgr.tasks[t2].lender == "alice"

    with pytest.raises(ToolError, match=RETRY_ADVICE):  # alice is working
        await mgr.start("again", continue_task_id=t1)
    await alice.finish(t2, seq=2, thread_id="thA")
    with pytest.raises(ToolError, match=RETRY_ADVICE):  # someone else was named
        await mgr.start("again", continue_task_id=t1, lender="bob")
    mgr.detach(alice.link)
    with pytest.raises(ToolError, match=RETRY_ADVICE):  # alice is gone
        await mgr.start("again", continue_task_id=t1)
    assert not bob.all("task_start")


async def test_continue_refused_for_disposable(mgr):
    alice = FakeAgent(mgr, "alice")
    await alice.push("hello", agent_version="0.2.0", codex_version="x", auth_mode="chatgpt", protocol=2,
                     capabilities={"resumable": False, "pre_approval": False}, current_task_id=None)
    t1 = await mgr.start("one")
    await alice.finish(t1, seq=1, thread_id="thA")
    with pytest.raises(ToolError, match=RETRY_ADVICE):
        await mgr.start("more", continue_task_id=t1)
    assert len(alice.all("task_start")) == 1 and len(mgr.tasks) == 1
    assert await mgr.start("fresh")  # a new request is still fine


async def test_v1_agent_without_capabilities_is_resumable(mgr):
    alice = FakeAgent(mgr, "alice")
    await alice.push("hello", agent_version="0.1.0", codex_version="x", auth_mode="chatgpt", current_task_id=None)
    info = mgr.lenders()[0]
    assert info["name"] == "alice" and info["protocol"] == 1
    assert info["capabilities"] == {"resumable": True, "pre_approval": False}
    t1 = await mgr.start("one")
    await alice.finish(t1, seq=1, thread_id="thA")
    await mgr.start("more", continue_task_id=t1)
    assert alice.last("task_start")["continue_thread_id"] == "thA"


async def test_hello_v2_records_capabilities_and_limits(mgr):
    alice = FakeAgent(mgr, "alice")
    await alice.push("hello", agent_version="0.2.0", codex_version="x", auth_mode="chatgpt", protocol=2,
                     capabilities={"resumable": True, "pre_approval": True},
                     limits={"five_hour": 50, "weekly": 60, "force": False}, current_task_id=None)
    info = mgr.lenders()[0]
    assert info["protocol"] == 2 and info["capabilities"] == {"resumable": True, "pre_approval": True}
    assert info["limits"] == {"five_hour": 50, "weekly": 60, "force": False}
    await alice.push("usage", **usage(five_hour=55))  # over alice's own limit, under the host's
    with pytest.raises(ToolError, match="再開可能"):
        await mgr.start("one")


async def test_usage_and_lenders_are_per_lender(mgr):
    assert mgr.lenders() == [] and mgr.usage()["lenders"] == {} and mgr.agent_info() == {"connected": False}
    alice, bob = await FakeAgent.join(mgr, "alice"), await FakeAgent.join(mgr, "bob")
    await alice.push("usage", **usage(five_hour=10))
    await bob.push("usage", **usage(five_hour=95))
    u = mgr.usage()
    assert u["lenders"]["alice"]["five_hour"]["used_percent"] == 10 and u["lenders"]["alice"]["blocked_until"] is None
    assert u["lenders"]["bob"]["five_hour"]["used_percent"] == 95 and u["lenders"]["bob"]["blocked_until"]
    assert u["five_hour"]["used_percent"] == 95 and u["thresholds"] == TH  # top level: the latest connection
    tid = await mgr.start("one")
    rows = {r["name"]: r for r in mgr.lenders()}
    assert rows["alice"]["connected"] and rows["alice"]["current_task_id"] == tid
    assert rows["bob"]["current_task_id"] is None and rows["bob"]["usage"]["five_hour"]["used_percent"] == 95
    mgr.detach(bob.link)
    assert mgr.agent_info()["name"] == "alice" and mgr.agent_info()["connected"] is True
    assert {r["name"]: r["connected"] for r in mgr.lenders()} == {"alice": True, "bob": False}


async def test_reconnect_reconciles_only_that_lenders_tasks(mgr):
    alice, bob = await FakeAgent.join(mgr, "alice"), await FakeAgent.join(mgr, "bob")
    t1 = await mgr.start("one", lender="alice")
    t2 = await mgr.start("two", lender="bob")
    mgr.detach(bob.link)
    bob2 = FakeAgent(mgr, "bob")
    await bob2.push("hello", agent_version="x", codex_version="x", auth_mode="chatgpt", current_task_id=None)
    assert mgr.status(t2)["state"] == "failed" and mgr.status(t1)["state"] == "queued"
    assert mgr.current_task_id("bob") is None and mgr.current_task_id("alice") == t1


@pytest.mark.parametrize("still_running, expected", [(True, "running"), (False, "failed")])
async def test_task_left_over_from_v01_is_settled_by_the_first_hello(tmp_path, still_running, expected):
    # Upgrade with a task in flight: the record has no lender, and must not stay "agent_offline" forever.
    tasks = tmp_path / "data" / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "old.json").write_text(
        '{"task_id": "old", "prompt": "p", "created": 1.0, "state": "running", "thread_id": "th0"}', encoding="utf-8")
    mgr = TaskManager(tmp_path / "data", touched_fn=lambda t: [])
    assert mgr.status("old")["state"] == "agent_offline"
    alice = FakeAgent(mgr, "alice")
    await alice.push("hello", agent_version="0.1.0", codex_version="x", auth_mode="chatgpt",
                     current_task_id="old" if still_running else None)
    assert mgr.status("old")["state"] == expected and mgr.tasks["old"].lender == "alice"
    assert mgr.current_task_id("alice") == ("old" if still_running else None)


async def test_session_requests_need_a_lender_when_several_are_connected(mgr):
    alice = FakeAgent(mgr, "alice")

    def answer(agent, type_, data):
        async def later():
            await asyncio.sleep(0.05)
            await agent.push("reply", req_id=agent.last(type_)["req_id"], ok=True, data=data)
        asyncio.create_task(later())

    answer(alice, "session_list", ["a1"])
    assert await mgr.sessions_list() == ["a1"]  # only one lender: no name needed
    bob = FakeAgent(mgr, "bob")
    with pytest.raises(ToolError, match="lender"):
        await mgr.sessions_list()
    answer(bob, "session_read", {"thread_id": "b1"})
    assert await mgr.session_read("b1", lender="bob") == {"thread_id": "b1"}
    assert not alice.all("session_read")
    with pytest.raises(ToolError, match="接続されていません"):
        await mgr.session_delete("x", lender="carol")


async def test_lender_survives_restart_and_old_records_load(tmp_path, mgr):
    alice = await FakeAgent.join(mgr, "alice")
    tid = await mgr.start("one")
    await alice.finish(tid, seq=1)
    (tmp_path / "data" / "tasks" / "old.json").write_text(
        '{"task_id": "old", "prompt": "p", "created": 1.0, "state": "completed", "thread_id": "th0"}', encoding="utf-8")
    mgr2 = TaskManager(tmp_path / "data", touched_fn=lambda t: [])
    assert mgr2.tasks[tid].lender == "alice" and mgr2.tasks["old"].lender is None
    rows = {r["task_id"]: r for r in mgr2.list_tasks()}
    assert rows[tid]["lender"] == "alice" and rows["old"]["lender"] is None
    # a v0.1 record has no lender: its continuation goes to the only connected one, once that one said hello
    solo = FakeAgent(mgr2, "alice")
    with pytest.raises(ToolError, match=NOT_READY):
        await mgr2.start("more", continue_task_id="old")
    assert not solo.all("task_start") and "old" in mgr2.tasks and len(mgr2.tasks) == 2
    await solo.push("hello", **HELLO, current_task_id=None)
    await mgr2.start("more", continue_task_id="old")
    assert solo.last("task_start")["continue_thread_id"] == "th0"


# ---- a lender that connected but has not said hello yet: what it can do is not known ----
async def test_no_task_goes_to_a_lender_before_its_hello(mgr):
    alice = FakeAgent(mgr, "alice")
    row = mgr.lenders()[0]
    assert row["connected"] is True and row["ready"] is False
    with pytest.raises(ToolError, match=NOT_READY):
        await mgr.start("one")
    with pytest.raises(ToolError, match=NOT_READY):
        await mgr.start("one", lender="alice")
    assert not alice.all("task_start") and mgr.tasks == {}
    await alice.push("hello", **HELLO, current_task_id=None)
    assert mgr.lenders()[0]["ready"] is True
    tid = await mgr.start("one")
    assert alice.last("task_start")["task_id"] == tid


async def test_automatic_routing_skips_a_lender_before_its_hello(mgr):
    alice = FakeAgent(mgr, "alice")  # has more room, but no hello yet
    bob = await FakeAgent.join(mgr, "bob")
    await alice.push("usage", **usage(five_hour=0))
    await bob.push("usage", **usage(five_hour=70))
    tid = await mgr.start("one")
    assert mgr.tasks[tid].lender == "bob" and not alice.all("task_start")
    with pytest.raises(ToolError, match="作業中"):  # bob works; alice still cannot be chosen
        await mgr.start("two")


async def test_continuation_waits_for_the_hello_of_a_new_connection(mgr):
    # While away the lender may have switched to disposable sessions: only the new hello tells.
    alice = await FakeAgent.join(mgr, "alice")
    t1 = await mgr.start("one")
    await alice.finish(t1, seq=1, thread_id="thA")
    mgr.detach(alice.link)
    again = FakeAgent(mgr, "alice")
    assert mgr.lenders()[0]["ready"] is False
    with pytest.raises(ToolError, match=NOT_READY):
        await mgr.start("more", continue_task_id=t1)
    assert not again.all("task_start")
    await again.push("hello", **HELLO, protocol=2, capabilities={"resumable": False, "pre_approval": False},
                     current_task_id=None)
    with pytest.raises(ToolError, match=RETRY_ADVICE):
        await mgr.start("more", continue_task_id=t1)
    assert not again.all("task_start") and len(mgr.tasks) == 1
