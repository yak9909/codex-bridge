"""Parallel tasks across the host and lender, using the real control handlers and task stores."""

import asyncio
from types import SimpleNamespace as NS

import pytest

from codex_bridge.agent.runner import AgentRunner, TaskSlot
from codex_bridge.agent.sessions import SessionRegistry
from codex_bridge.agent.settings import defaults, hello_fields
from codex_bridge.agent.usage import QuotaGuard, UsageSnapshot, Window, STOP_TEXT
from codex_bridge.agent.worklog import WorkLog
from codex_bridge.host.tasks import TaskManager
from codex_bridge.host.paths import ToolError


class App:
    def __init__(self):
        self.calls = []
        self.events = asyncio.Queue()
        self.next_thread = 0
        self.next_turn = 0
        self.before_turn = None

    async def thread_start(self, instructions, ephemeral=False, model=None):
        self.next_thread += 1
        self.calls.append(("thread/start", self.next_thread))
        return {"id": f"thread-{self.next_thread}", "model": model}

    async def thread_resume(self, thread_id, instructions, model=None):
        self.calls.append(("thread/resume", thread_id))
        return {"id": thread_id, "model": model}

    async def turn_start(self, thread_id, text, **kwargs):
        if self.before_turn:
            self.before_turn(thread_id)
        self.next_turn += 1
        self.calls.append(("turn/start", thread_id, text))
        return f"turn-{self.next_turn}"

    async def turn_steer(self, thread_id, turn_id, text):
        self.calls.append(("turn/steer", thread_id, turn_id, text))

    async def turn_interrupt(self, thread_id, turn_id):
        self.calls.append(("turn/interrupt", thread_id, turn_id))

    async def thread_delete(self, thread_id):
        self.calls.append(("thread/delete", thread_id))


def runner(folder, *, slot=None):
    sent = []

    async def send(message):
        sent.append(message)

    r = AgentRunner(App(), QuotaGuard({}), SessionRegistry(folder / "sessions.json"), send,
                    slot=slot, worklog=WorkLog(folder / "log"))
    r.apply_settings({**defaults(), "max_parallel": 2})
    return r, sent


async def event(r, task, method, **params):
    cur = r.currents[task]
    if method in ("turn/started", "turn/completed"):
        params["turn"] = {"id": cur.turn_id, **params.get("turn", {})}
    await r._on_event({"method": method, "params": {"threadId": cur.thread_id, **params}})


async def begin(r, task, **fields):
    await r.handle({"type": "task_start", "task_id": task, "prompt": task, **fields})


@pytest.fixture
async def linked(tmp_path):
    mgr = TaskManager(tmp_path / "host", touched_fn=lambda t: [])
    r, sent = runner(tmp_path / "agent")
    link = mgr.attach("alice", r.handle)

    async def send(message):
        sent.append(message)
        await mgr.on_agent_message(link, message)

    r.send = send
    await mgr.on_agent_message(link, {"type": "hello", **hello_fields(r.settings), "active_task_ids": []})

    def before_turn(thread):
        matches = [t for t in mgr.tasks.values() if t.thread_id == thread and t.state == "running"]
        assert len(matches) == 1 and matches[0].state == "running"

    r.app.before_turn = before_turn
    return NS(mgr=mgr, runner=r, sent=sent, link=link)


async def test_host_and_lender_run_two_tasks_and_refuse_above_capacity(linked):
    e = linked
    one = await e.mgr.start("one", lender="alice")
    two = await e.mgr.start("two", lender="alice")
    assert set(e.runner.currents) == {one, two}
    assert [e.mgr.tasks[t].state for t in (one, two)] == ["running", "running"]
    row = e.mgr.lenders()[0]
    assert row["active_task_ids"] == [one, two] and row["current_task_id"] == one and row["max_parallel"] == 2
    assert e.runner.app.next_thread == 2
    with pytest.raises(ToolError, match="作業中"):
        await e.mgr.start("three", lender="alice")
    await begin(e.runner, "over-capacity")
    assert e.sent[-1]["state"] == "rejected_by_lender" and "上限" in e.sent[-1]["report"]
    assert set(e.runner.currents) == {one, two}


async def test_followup_cancel_completion_and_usage_are_separate(linked):
    e = linked
    one, two = await e.mgr.start("one"), await e.mgr.start("two")
    r = e.runner
    await event(r, one, "turn/started")
    await event(r, two, "turn/started")
    await event(r, one, "thread/tokenUsage/updated", tokenUsage={"last": {"inputTokens": 11, "outputTokens": 3}})
    await event(r, two, "thread/tokenUsage/updated", tokenUsage={"last": {"inputTokens": 22, "outputTokens": 4}})
    await event(r, two, "item/completed", item={"type": "agentMessage", "text": "second report"})
    await e.mgr.followup(one, "first instruction")
    assert r.app.calls[-1] == ("turn/steer", "thread-1", "turn-1", "first instruction")
    await e.mgr.cancel(one)
    assert r.currents[one].cancelling and not r.currents[two].cancelling
    assert r.app.calls[-1] == ("turn/interrupt", "thread-1", "turn-1")
    await event(r, one, "turn/completed", turn={"status": "interrupted"})
    assert e.mgr.tasks[one].state == "cancelled" and e.mgr.tasks[two].state == "running"
    assert set(r.currents) == {two}
    await event(r, two, "turn/completed", turn={"status": "completed"})
    assert e.mgr.result(two)["report"] == "second report"
    tokens = {m["task_id"]: m["data"] for m in e.sent if m.get("kind") == "token_usage"}
    assert tokens == {one: {"input": 11, "output": 3}, two: {"input": 22, "output": 4}}


async def test_continuation_of_a_busy_thread_is_rejected_on_both_sides(linked):
    e = linked
    one = await e.mgr.start("one")
    with pytest.raises(ToolError, match="会話は作業中"):
        await e.mgr.start("again", continue_task_id=one)
    await begin(e.runner, "duplicate-thread", continue_thread_id="thread-1")
    assert e.sent[-1]["state"] == "rejected_by_lender"
    assert e.runner.app.next_thread == 1 and set(e.runner.currents) == {one}
    await event(e.runner, one, "turn/completed", turn={"status": "completed"})
    two = await e.mgr.start("continue", continue_task_id=one)
    with pytest.raises(ToolError, match="会話は作業中"):
        await e.mgr.start("duplicate continuation", continue_task_id=one)
    assert e.mgr.tasks[two].thread_id == "thread-1"


@pytest.mark.parametrize("force", [False, True])
async def test_quota_stops_every_running_task(tmp_path, force):
    r, sent = runner(tmp_path)
    r.apply_settings({**r.settings, "limits": {"five_hour": 50, "weekly": None, "force": force}})
    await begin(r, "one")
    await begin(r, "two")
    for task in list(r.currents):
        await event(r, task, "turn/started")
    await r._on_usage(UsageSnapshot(Window(60, 2000000000), None))
    method = "turn/interrupt" if force else "turn/steer"
    stops = [c for c in r.app.calls if c[0] == method]
    assert {c[1] for c in stops} == {"thread-1", "thread-2"}
    if not force:
        assert all(c[-1] == STOP_TEXT for c in stops)
    await event(r, "one", "turn/completed", turn={"status": "interrupted" if force else "completed"})
    assert set(r.currents) == {"two"}
    await event(r, "two", "turn/completed", turn={"status": "interrupted" if force else "completed"})
    assert {m["task_id"] for m in sent if m.get("state") == "suspended_quota"} == {"one", "two"}


async def test_capacity_is_global_and_pending_approvals_are_independent(tmp_path):
    slot = TaskSlot()
    a, a_sent = runner(tmp_path / "a", slot=slot)
    b, b_sent = runner(tmp_path / "b", slot=slot)
    for r in (a, b):
        r.settings["pre_approval"]["new_task"] = True
    await asyncio.gather(begin(a, "one"), begin(b, "two"))
    assert slot.count == 2
    await begin(a, "three")
    assert a_sent[-1]["state"] == "rejected_by_lender"
    p = a.pending_approvals[0]
    assert not await a.decide_pending(True, task_id="one", created=p["created"] - 1)
    assert await a.decide_pending(True, task_id="one", created=p["created"])
    assert set(a.currents) == {"one"} and b.pending_approvals[0]["task_id"] == "two"
    assert await b.decide_pending(False, "使いません", task_id="two", created=b.pending_approvals[0]["created"])
    assert "使いません" in next(m for m in reversed(b_sent) if m["type"] == "task_state")["report"]
    assert slot.count == 1
    await begin(a, "three")
    assert {p["task_id"] for p in a.pending_approvals} == {"three"}


async def test_multiple_pending_on_one_runner_can_be_decided_separately(tmp_path):
    r, sent = runner(tmp_path)
    r.settings["pre_approval"]["new_task"] = True
    await begin(r, "one")
    await begin(r, "two")
    assert not await r.decide_pending(True)  # no implicit first decision when ambiguous
    assert await r.decide_pending(False, task_id="two")
    assert await r.decide_pending(True, task_id="one")
    assert set(r.currents) == {"one"} and not r.pending_approvals
    assert [m["task_id"] for m in sent if m.get("state") == "running"] == ["one"]


async def test_reconnect_keeps_both_tasks_and_settings_hello_does_not_reconcile(linked):
    e = linked
    ids = [await e.mgr.start("one"), await e.mgr.start("two")]
    e.mgr.detach(e.link)
    link = e.mgr.attach("alice", e.runner.handle)
    await e.mgr.on_agent_message(link, {"type": "hello", **hello_fields(e.runner.settings),
                                       "current_task_id": ids[0], "active_task_ids": ids})
    assert e.mgr.active_task_ids("alice") == ids
    assert all(e.mgr.tasks[t].state == "running" for t in ids)
    await e.mgr.on_agent_message(link, {"type": "hello", **hello_fields(e.runner.settings)})
    assert e.mgr.active_task_ids("alice") == ids


@pytest.mark.parametrize("version,capabilities", [(4, {}), (4, {"max_parallel": 8}), (5, {})])
async def test_older_lender_still_has_capacity_one(tmp_path, version, capabilities):
    mgr = TaskManager(tmp_path, touched_fn=lambda t: [])

    async def send(message):
        pass

    link = mgr.attach("old", send)
    await mgr.on_agent_message(link, {"type": "hello", "protocol": version, "capabilities": capabilities})
    await mgr.start("one")
    assert mgr.lenders()[0]["max_parallel"] == 1
    with pytest.raises(ToolError, match="作業中"):
        await mgr.start("two")


async def test_old_host_message_shapes_work_with_new_runner(tmp_path):
    r, sent = runner(tmp_path)
    await r.handle({"type": "settings", "thresholds": {}, "log_visibility": "summary"})
    await begin(r, "legacy", continue_thread_id=None)
    await r.handle({"type": "task_followup", "task_id": "legacy", "text": "more"})
    await r.handle({"type": "task_cancel", "task_id": "legacy"})
    assert [c[0] for c in r.app.calls] == ["thread/start", "turn/start", "turn/steer", "turn/interrupt"]


async def test_routing_prefers_fewer_tasks_then_headroom(tmp_path):
    mgr = TaskManager(tmp_path, touched_fn=lambda t: [])

    async def send(message):
        pass

    for name, used in [("alice", 10), ("bob", 30)]:
        link = mgr.attach(name, send)
        await mgr.on_agent_message(link, {"type": "hello", "protocol": 5, "capabilities": {"max_parallel": 3}})
        await mgr.on_agent_message(link, {"type": "usage", "five_hour": {"used_percent": used, "resets_at": 2000000000}})
    ids = [await mgr.start(str(i)) for i in range(4)]
    assert [mgr.tasks[t].lender for t in ids] == ["alice", "bob", "alice", "bob"]


async def test_pending_followups_and_late_events_stay_with_their_task(tmp_path):
    r, sent = runner(tmp_path)
    await begin(r, "one")
    await begin(r, "two")
    r.settings["pre_approval"]["followup"] = True
    for task in ("one", "two"):
        await r.handle({"type": "task_followup", "task_id": task, "text": f"follow {task}"})
    assert len(r.pending_approvals) == 2
    await event(r, "one", "turn/completed", turn={"status": "completed"})
    assert [p["task_id"] for p in r.pending_approvals] == ["two"]
    assert not await r.decide_pending(True, task_id="one")
    await r._on_event({"method": "turn/completed", "params": {
        "threadId": "thread-2", "turn": {"id": "stale-turn", "status": "completed"}}})
    assert "two" in r.currents
    assert await r.decide_pending(True, task_id="two")
    assert r.app.calls[-1] == ("turn/steer", "thread-2", "turn-2", "follow two")


async def test_quota_broadcast_reaches_tasks_on_every_connection(tmp_path):
    slot = TaskSlot()
    a, _ = runner(tmp_path / "a", slot=slot)
    b, _ = runner(tmp_path / "b", slot=slot)
    for r in (a, b):
        await begin(r, "same-id")
        await event(r, "same-id", "turn/started")
    await slot.usage(UsageSnapshot(Window(90, 2000000000), None))
    assert all(any(c[0] == "turn/steer" and c[-1] == STOP_TEXT for c in r.app.calls) for r in (a, b))


async def test_tray_counts_all_pending_requests(tmp_path):
    from codex_bridge.agent.main import AgentRole
    from codex_bridge.resident import Resident

    r, _ = runner(tmp_path)
    r.settings["pre_approval"]["new_task"] = True
    await begin(r, "one")
    await begin(r, "two")
    role = object.__new__(AgentRole)
    role.runtime = NS(rows=lambda: [{"connected": True}], runners={"one": r})
    assert role.counts() == (1, 1, 2)
    resident = Resident(applog=None, agent=role, host_role=NS(counts=lambda: (1, 1, 1)),
                        config_path=tmp_path / "host.yaml")
    assert resident.status_line() == "貸す人 1/1・依頼主 1/1・承認待ち 3"


async def test_lowering_capacity_keeps_admitted_tasks_and_refuses_new_ones(tmp_path):
    r, sent = runner(tmp_path)
    r.settings["pre_approval"]["new_task"] = True
    await begin(r, "one")
    await begin(r, "two")
    r.apply_settings({**r.settings, "max_parallel": 1})
    assert await r.decide_pending(True, task_id="one")
    assert await r.decide_pending(True, task_id="two")
    assert set(r.currents) == {"one", "two"}
    await begin(r, "three")
    assert next(m for m in reversed(sent) if m["type"] == "task_state")["state"] == "rejected_by_lender"


async def test_rejecting_one_of_two_followups_restores_only_its_task(linked):
    e = linked
    ids = [await e.mgr.start("first"), await e.mgr.start("second")]
    e.runner.settings["pre_approval"]["followup"] = True
    for task in ids:
        await e.mgr.followup(task, "additional instruction")
    assert all(e.mgr.tasks[t].state == "awaiting_lender" for t in ids)
    assert await e.runner.decide_pending(False, "最初の依頼の理由", task_id=ids[0])
    assert [e.mgr.tasks[t].state for t in ids] == ["running", "awaiting_lender"]
    status = [m for m in e.sent if m.get("kind") == "status"][-1]
    assert status["task_id"] == ids[0] and "最初の依頼の理由" in status["data"]["text"]
    assert [p["task_id"] for p in e.runner.pending_approvals] == [ids[1]]


@pytest.mark.parametrize("operation", ["expire", "shutdown"])
async def test_quota_during_pending_cleanup_does_not_mix_or_lose_requests(tmp_path, operation):
    r, sent = runner(tmp_path)
    r.settings["pre_approval"]["new_task"] = True
    await begin(r, "one")
    await begin(r, "two")

    async def send(message):
        sent.append(message)
        if message.get("task_id") == "one" and message.get("state") == "rejected_by_lender":
            await r._on_usage(UsageSnapshot(Window(90, 2000000000), None))

    r.send = send
    r.clock = lambda: 1e20
    await getattr(r.approval, operation)()
    states = {m["task_id"]: m["state"] for m in sent if m["type"] == "task_state"}
    assert states == {"one": "rejected_by_lender", "two": "suspended_quota"}
    assert not r.active_task_ids()
