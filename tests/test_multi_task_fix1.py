"""Regressions for task identity, admission reservations, and independent app-server event pumps."""

import asyncio
import json

import pytest

from codex_bridge.agent.appserver import AppServerError
from codex_bridge.agent.control_client import ControlClient
from codex_bridge.agent.runner import TaskSlot
from codex_bridge.agent.settings import hello_fields
from codex_bridge.agent.usage import UsageSnapshot, Window
from codex_bridge.common.invite import Invite
from codex_bridge.host.paths import ToolError
from codex_bridge.host.tasks import TaskManager
from test_multi_task import App, begin, runner


async def wait_until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.005)


class WaitingApp(App):
    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.resume = asyncio.Event()

    async def turn_start(self, thread_id, text, **kwargs):
        if text == "slow":
            self.entered.set()
            await self.resume.wait()
        return await super().turn_start(thread_id, text, **kwargs)


@pytest.mark.parametrize("same_runner", [False, True])
@pytest.mark.parametrize("approval", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
async def test_waiting_start_does_not_block_other_task_pump_or_cancel(tmp_path, same_runner, approval, cancel):
    slot = TaskSlot()
    fast, fast_sent = runner(tmp_path / "fast", slot=slot)
    fast.app = WaitingApp()
    slow = fast if same_runner else runner(tmp_path / "slow", slot=slot)[0]
    if slow is not fast:
        slow.app = WaitingApp()
    all_runners = [fast] if same_runner else [fast, slow]
    pumps = [asyncio.create_task(r._pump()) for r in all_runners]
    starting = None
    try:
        await begin(fast, "existing")
        current = fast.currents["existing"]
        thread, turn = current.thread_id, current.turn_id
        if approval:
            slow.settings["pre_approval"]["new_task"] = True
            await begin(slow, "slow")
            starting = asyncio.create_task(slow.decide_pending(True, task_id="slow"))
        else:
            starting = asyncio.create_task(begin(slow, "slow"))
        await asyncio.wait_for(slow.app.entered.wait(), 2)
        assert slot.count == 2 and not starting.done()

        async def capacity_lock_is_available():
            async with slot.lock:
                assert slot.count == 2

        await asyncio.wait_for(capacity_lock_is_available(), 0.5)
        await asyncio.wait_for(begin(fast, "over-capacity"), 0.5)
        assert slot.count == 2 and "over-capacity" not in fast.active_task_ids()
        await fast.app.events.put({"method": "item/agentMessage/delta", "params": {
            "threadId": thread, "turnId": turn, "itemId": "message", "delta": "進みました"}})
        await wait_until(lambda: any(m.get("kind") == "progress" and m["task_id"] == "existing" for m in fast_sent))
        if cancel:
            await asyncio.wait_for(fast.handle({"type": "task_cancel", "task_id": "existing"}), 0.5)
            assert ("turn/interrupt", thread, turn) in fast.app.calls
        await fast.app.events.put({"method": "turn/completed", "params": {
            "threadId": thread, "turn": {"id": turn, "status": "interrupted" if cancel else "completed"}}})
        expected = "cancelled" if cancel else "completed"
        await wait_until(lambda: any(m.get("task_id") == "existing" and m.get("state") == expected for m in fast_sent))
        assert "existing" not in fast.currents and not starting.done()
        assert slot.count == 1
    finally:
        slow.app.resume.set()
        if starting:
            await asyncio.wait_for(asyncio.gather(starting, return_exceptions=True), 2)
        for r in all_runners:
            await r.shutdown()
        for pump in pumps:
            pump.cancel()
        await asyncio.gather(*pumps, return_exceptions=True)
    assert slot.count == 0


@pytest.mark.parametrize("stage", ["prepare", "thread_start", "turn_start"])
async def test_failed_start_always_returns_the_global_reservation(tmp_path, stage):
    slot = TaskSlot()
    failed, sent = runner(tmp_path / "failed", slot=slot)
    next_runner, _ = runner(tmp_path / "next", slot=slot)
    slot.max_parallel = 1

    async def fail(*args, **kwargs):
        raise AppServerError("開始失敗のテスト")

    if stage == "prepare":
        failed.prepare = fail
    else:
        setattr(failed.app, stage, fail)
    await begin(failed, "failed")
    assert any(m.get("state") == "failed" for m in sent)
    assert slot.count == 0 and not failed._reservations and not failed.active_task_ids()
    await begin(next_runner, "next")
    assert next_runner.active_task_ids() == ["next"] and slot.count == 1
    await next_runner.shutdown()
    assert slot.count == 0


@pytest.mark.parametrize("same_runner", [False, True])
async def test_cancel_during_start_releases_reservation_and_interrupts_late_turn(tmp_path, same_runner):
    slot = TaskSlot()
    r, sent = runner(tmp_path / "r", slot=slot)
    r.app = WaitingApp()
    starting = asyncio.create_task(begin(r, "slow"))
    try:
        await asyncio.wait_for(r.app.entered.wait(), 2)
        thread = r.currents["slow"].thread_id
        await asyncio.wait_for(r.handle({"type": "task_cancel", "task_id": "slow"}), 0.5)
        assert slot.count == 0 and not r._reservations
        other = r if same_runner else runner(tmp_path / "other", slot=slot)[0]
        await begin(other, "other")
        r.app.resume.set()
        await asyncio.wait_for(starting, 2)
        assert any(call[0] == "turn/interrupt" and call[1] == thread for call in r.app.calls)
        assert any(m.get("state") == "cancelled" and m["task_id"] == "slow" for m in sent)
        assert other.active_task_ids() == ["other"]
        await other.shutdown()
    finally:
        r.app.resume.set()
        await asyncio.gather(starting, return_exceptions=True)
        await r.shutdown()


async def test_enabled_changes_reach_every_active_guard_and_cancel_old_stop_timers(tmp_path):
    r, sent = runner(tmp_path)
    await begin(r, "one")
    await begin(r, "two")
    for current in r.currents.values():
        r._guard_for(current).on_turn_started(current.turn_id)
    await r._on_usage(UsageSnapshot(Window(90, 2000000000), None))
    assert all(r._guard_for(c).stopping for c in r.currents.values())
    for current in r.currents.values():
        current.suspend_at = 2000000000
    r.guard.enabled = False
    assert all(not r._guard_for(c).enabled and not r._guard_for(c).tick() for c in r.currents.values())
    assert all(not r._guard_for(c).stopping and c.suspend_at is None for c in r.currents.values())
    before = len(r.app.calls)
    await r._on_usage(UsageSnapshot(Window(95, 2000000000), None))
    assert len(r.app.calls) == before
    r.set_usage_enabled(True)
    await r._on_usage(UsageSnapshot(Window(95, 2000000000), None))
    assert len([call for call in r.app.calls[before:] if call[0] == "turn/steer"]) == 2


def protocol4_reader(raw):
    """Read the protocol-4 fields; its hello reconciliation knows only current_task_id."""
    message = json.loads(raw)
    assert message["type"] in {"hello", "event", "usage", "task_state", "reply", "lender_status", "lender_models"}
    message.pop("active_task_ids", None)
    if "capabilities" in message:
        message["capabilities"] = {key: value for key, value in message["capabilities"].items()
                                   if key in {"resumable", "pre_approval", "model_select"}}
    if isinstance(message.get("lender_view"), dict):
        message["lender_view"].pop("max_parallel", None)
    return message


async def test_protocol4_host_reads_new_lenders_hello_models_and_results(tmp_path):
    r, _ = runner(tmp_path / "agent")
    host = TaskManager(tmp_path / "host", touched_fn=lambda _: [])
    raw_messages = []
    link = host.attach("friend", r.handle)

    class Ws:
        async def send(self, raw):
            raw_messages.append(json.loads(raw))
            await host.on_agent_message(link, protocol4_reader(raw))

    client = ControlClient(Invite("http://host", "token", "friend"), r,
                           lambda: hello_fields(r.settings))
    client.ws = Ws()
    await client.on_connected()
    await r.send_models()
    hello = next(m for m in raw_messages if m["type"] == "hello")
    assert hello["protocol"] == 5 and hello["capabilities"]["max_parallel"] == 2
    assert host.lenders()[0]["max_parallel"] == 1
    task = await host.start("one")
    with pytest.raises(ToolError, match="上限"):
        await host.start("two")
    pump = asyncio.create_task(r._pump())
    try:
        cur = r.currents[task]
        await r.app.events.put({"method": "item/completed", "params": {
            "threadId": cur.thread_id, "turnId": cur.turn_id, "item": {"type": "agentMessage", "text": "完了"}}})
        await r.app.events.put({"method": "turn/completed", "params": {
            "threadId": cur.thread_id, "turn": {"id": cur.turn_id, "status": "completed"}}})
        await wait_until(lambda: host.tasks[task].state == "completed")
        assert host.result(task)["report"] == "完了"
    finally:
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)


async def test_shutdown_during_approval_handover_cannot_start_or_leak_a_reservation(tmp_path):
    slot = TaskSlot()
    r, sent = runner(tmp_path, slot=slot)
    r.settings["pre_approval"]["new_task"] = True
    await begin(r, "pending")
    async with r._lock:
        approving = asyncio.create_task(r.decide_pending(True, task_id="pending"))
        await asyncio.sleep(0)
        closing = asyncio.create_task(r.shutdown())
        await asyncio.sleep(0)
    await asyncio.wait_for(asyncio.gather(approving, closing), 2)
    assert slot.count == 0 and not r._reservations and not r.currents and not r.pending_approvals
    assert not r.app.calls


async def test_cancelling_startup_coroutine_returns_the_global_reservation(tmp_path):
    slot = TaskSlot()
    r, sent = runner(tmp_path, slot=slot)
    r.app = WaitingApp()
    starting = asyncio.create_task(begin(r, "slow"))
    await asyncio.wait_for(r.app.entered.wait(), 2)
    starting.cancel()
    await asyncio.gather(starting, return_exceptions=True)
    assert slot.count == 0 and not r._reservations and not r.currents
    assert any(m.get("state") == "failed" for m in sent)
