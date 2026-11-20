"""Regressions through control-channel dispatch, with RPC replies deliberately withheld."""

import asyncio
from contextlib import asynccontextmanager
import json

import pytest

from codex_bridge.agent.appserver import AppServerError
from codex_bridge.agent.control_client import ControlClient
from codex_bridge.agent.runner import FOLLOWUP_ENDED
from codex_bridge.agent.settings import hello_fields
from codex_bridge.common import protocol
from codex_bridge.common.invite import Invite
from test_multi_task import App, begin, runner
from test_multi_task_fix1 import wait_until


class GatedApp(App):
    def __init__(self, stage):
        super().__init__()
        self.stage = stage
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.fail = False

    async def wait(self, stage):
        if stage == self.stage:
            self.entered.set()
            await self.release.wait()
            if self.fail:
                raise AppServerError("開始失敗のテスト")

    async def thread_start(self, *args, **kwargs):
        info = await super().thread_start(*args, **kwargs)
        await self.wait("thread")
        return info

    async def turn_start(self, *args, **kwargs):
        turn = await super().turn_start(*args, **kwargs)
        await self.wait("turn")
        return turn

    async def turn_steer(self, *args):
        await super().turn_steer(*args)
        await self.wait("steer")

    async def thread_read(self, thread_id):
        self.calls.append(("thread/read", thread_id))
        await self.wait("read")
        return {"thread": {"id": thread_id}}

    async def thread_delete(self, thread_id):
        await super().thread_delete(thread_id)
        await self.wait("delete")

    async def turn_interrupt(self, *args):
        await super().turn_interrupt(*args)
        await self.wait("interrupt")


class BufferedSocket:
    def __init__(self, frames, before_second=None):
        self.frames = iter(enumerate(frames))
        self.before_second = before_second
        self.drained = asyncio.Event()
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            index, frame = next(self.frames)
        except StopIteration:
            self.drained.set()
            await asyncio.Future()
        if index == 1 and self.before_second:
            await self.before_second()
        return protocol.encode(frame)

    async def send(self, raw):
        self.sent.append(json.loads(raw))


@asynccontextmanager
async def control(r, monkeypatch, frames, before_second=None):
    ws = BufferedSocket(frames, before_second)
    monkeypatch.setattr("codex_bridge.agent.control_client.websockets.connect", lambda *a, **kw: ws)
    client = ControlClient(Invite("http://host", "token", "friend"), r, lambda: hello_fields(r.settings))
    receiving = asyncio.create_task(client.run_forever())
    pump = asyncio.create_task(r._pump())
    try:
        yield ws
    finally:
        r.app.release.set()
        receiving.cancel()
        await asyncio.gather(receiving, return_exceptions=True)
        await r.shutdown()
        await r.stop()
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)


START = {"type": "task_start", "task_id": "task", "prompt": "original"}
MORE = {"type": "task_followup", "task_id": "task", "text": "more"}
CANCEL = {"type": "task_cancel", "task_id": "task"}


def status_messages(ws):
    return [m for m in ws.sent if m.get("kind") == "status"]


@pytest.mark.parametrize("stage", ["admission", "prepare", "thread", "turn", "turn_started"])
async def test_buffered_start_followup_is_delivered_once_after_original_turn(tmp_path, monkeypatch, stage):
    r, _ = runner(tmp_path)
    r.app = GatedApp("turn" if stage in {"admission", "turn_started"} else stage)

    async def prepare():
        await r.app.wait("prepare")
    r.prepare = prepare

    async def before_followup():
        await r.app.entered.wait()
        if stage == "turn_started":
            cur = r.currents["task"]
            await r.app.events.put({"method": "turn/started", "params": {
                "threadId": cur.thread_id, "turn": {"id": "turn-1"}}})
            await wait_until(lambda: cur.turn_id == "turn-1")

    before = None if stage == "admission" else before_followup
    async with control(r, monkeypatch, [START, MORE], before) as ws:
        await asyncio.wait_for(ws.drained.wait(), 2)
        await asyncio.wait_for(r.app.entered.wait(), 2)
        assert not any(c[0] == "turn/steer" for c in r.app.calls)
        assert len([c for c in r.app.calls if c[0] == "turn/start"]) <= 1
        assert (r.currents["task"].followups or r._queued_followups.get("task")) == ["more"]
        r.app.release.set()
        await wait_until(lambda: len([c for c in r.app.calls if c[0] == "turn/steer"]) == 1)
        assert [c for c in r.app.calls if c[0] in {"turn/start", "turn/steer"}] == [
            ("turn/start", "thread-1", "original"), ("turn/steer", "thread-1", "turn-1", "more")]
        assert not status_messages(ws)


async def test_buffered_start_cancel_always_cancels_the_admitted_reservation(tmp_path, monkeypatch):
    r, _ = runner(tmp_path)
    r.app = GatedApp("turn")
    async with control(r, monkeypatch, [START, CANCEL]) as ws:
        await asyncio.wait_for(ws.drained.wait(), 2)
        await wait_until(lambda: any(m.get("state") == "cancelled" for m in ws.sent))
        assert not r.active_task_ids() and not r._reservations
        assert not any(c[0] == "turn/start" for c in r.app.calls)



@pytest.mark.parametrize("approval_required", [False, True])
async def test_reservation_cancel_reports_undelivered_followup_before_final_state(
        tmp_path, monkeypatch, approval_required):
    r, _ = runner(tmp_path)
    r.app = GatedApp("turn")
    r.settings["pre_approval"]["followup"] = approval_required
    async with control(r, monkeypatch, [START, MORE, CANCEL]) as ws:
        await asyncio.wait_for(ws.drained.wait(), 2)
        assert not r.currents
        notices = [m for m in status_messages(ws) if m["data"]["text"] == FOLLOWUP_ENDED]
        assert len(notices) == 1
        final = next(m for m in ws.sent if m.get("state") == "cancelled")
        assert ws.sent.index(notices[0]) < ws.sent.index(final)
        assert not r.active_task_ids()
        assert not any(c[0] == "turn/start" for c in r.app.calls)


@pytest.mark.parametrize("stage", ["prepare", "thread", "turn"])
async def test_queued_followup_is_reported_when_cancel_ends_startup(tmp_path, monkeypatch, stage):
    r, _ = runner(tmp_path)
    r.app = GatedApp(stage)

    async def prepare():
        await r.app.wait("prepare")
    r.prepare = prepare
    async with control(r, monkeypatch, [START, MORE, CANCEL], r.app.entered.wait) as ws:
        await asyncio.wait_for(ws.drained.wait(), 2)
        final = next(m for m in ws.sent if m.get("state") == "cancelled")
        notices = status_messages(ws)
        assert [m["data"]["text"] for m in notices] == [FOLLOWUP_ENDED]
        assert ws.sent.index(notices[0]) < ws.sent.index(final)
        assert not r.active_task_ids()
        assert not any(c[0] == "turn/steer" for c in r.app.calls)



@pytest.mark.parametrize("approval_required", [False, True])
@pytest.mark.parametrize("window", ["final-send", "ephemeral-delete", "reservation-rejection"])
async def test_followup_during_task_end_is_reported_once(tmp_path, window, approval_required):
    r, sent = runner(tmp_path)
    r.settings["pre_approval"]["followup"] = approval_required
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause_end(*args):
        entered.set()
        await release.wait()

    async def fail_start(*args, **kwargs):
        raise AppServerError("開始失敗のテスト")

    async def send(message):
        sent.append(message)
        if message.get("state") in {"failed", "rejected_by_lender"}:
            await pause_end()

    if window == "reservation-rejection":
        r.settings["pre_approval"]["new_task"] = True
        await r.handle(START)
        r.send = send
        ending = r.decide_pending(False, task_id="task")
    else:
        if window == "ephemeral-delete":
            r.settings["disposable"] = True
            r.app.turn_start = fail_start
            r.app.thread_delete = pause_end
        else:
            r.app.thread_start = fail_start
            r.send = send
        ending = r.handle(START)

    task = asyncio.create_task(ending)
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert ("task" in r.currents) == (window == "final-send")
        assert "task" in r._reservations
        await asyncio.wait_for(r.receive(MORE), 2)
        assert not r._queued_followups
        assert not r.approval.pendings
        assert [m["data"]["text"] for m in sent if m.get("kind") == "status"] == [FOLLOWUP_ENDED]
    finally:
        release.set()
        await asyncio.wait_for(task, 2)

    assert not r.active_task_ids()
    assert not r._ending_tasks
    assert not r._queued_followups
    assert not r.approval.pendings
    assert [m["data"]["text"] for m in sent if m.get("kind") == "status"] == [FOLLOWUP_ENDED]


async def test_followups_stay_ordered_while_steer_waits_and_cancel_is_still_received(tmp_path, monkeypatch):
    r, _ = runner(tmp_path)
    r.app = GatedApp("steer")
    await begin(r, "task")
    frames = [MORE, {**MORE, "text": "second"}, CANCEL]
    async with control(r, monkeypatch, frames, r.app.entered.wait) as ws:
        await asyncio.wait_for(ws.drained.wait(), 2)
        assert r.currents["task"].cancelling
        assert [c[-1] for c in r.app.calls if c[0] == "turn/steer"] == ["more"]
        await r.app.events.put({"method": "turn/completed", "params": {
            "threadId": "thread-1", "turn": {"id": "turn-1", "status": "interrupted"}}})
        await wait_until(lambda: any(m.get("state") == "cancelled" for m in ws.sent))
        assert len(status_messages(ws)) == 1  # The second instruction was never delivered.
        assert len([c for c in r.app.calls if c[0] == "turn/start"]) == 1


@pytest.mark.parametrize("stage", ["prepare", "read", "delete"])
async def test_slow_session_rpc_does_not_block_same_connection_pump_or_cancel(tmp_path, monkeypatch, stage):
    r, _ = runner(tmp_path)
    r.app = GatedApp(stage)
    await begin(r, "task")
    r.sessions.add("saved", "old")

    async def prepare():
        await r.app.wait("prepare")
    r.prepare = prepare
    operation = {"type": "session_delete" if stage == "delete" else "session_read",
                 "req_id": "session", "thread_id": "saved"}
    async with control(r, monkeypatch, [operation, CANCEL], r.app.entered.wait) as ws:
        await asyncio.wait_for(ws.drained.wait(), 2)
        assert not r.app.release.is_set() and r._session_operations == 1
        assert not r._lock.locked()
        await r.app.events.put({"method": "item/agentMessage/delta", "params": {
            "threadId": "thread-1", "turnId": "turn-1", "itemId": "message", "delta": "進みました"}})
        await r.app.events.put({"method": "turn/completed", "params": {
            "threadId": "thread-1", "turn": {"id": "turn-1", "status": "interrupted"}}})
        await wait_until(lambda: any(m.get("state") == "cancelled" for m in ws.sent))
        assert any(m.get("kind") == "progress" for m in ws.sent)
        assert not any(m.get("req_id") == "session" for m in ws.sent)
        r.app.release.set()
        await wait_until(lambda: any(m.get("req_id") == "session" and m["ok"] for m in ws.sent))
        await wait_until(lambda: r._session_operations == 0)


async def test_slow_interrupt_does_not_block_control_intake_or_pump(tmp_path, monkeypatch):
    r, _ = runner(tmp_path)
    r.app = GatedApp("interrupt")
    await begin(r, "task")
    await begin(r, "other")
    async with control(r, monkeypatch, [CANCEL, {"type": "session_list", "req_id": "list"}]) as ws:
        await asyncio.wait_for(ws.drained.wait(), 2)
        await asyncio.wait_for(r.app.entered.wait(), 2)
        assert not r._lock.locked()
        assert any(m.get("req_id") == "list" and m["ok"] for m in ws.sent)
        for thread, turn, state in [("thread-1", "turn-1", "interrupted"), ("thread-2", "turn-2", "completed")]:
            await r.app.events.put({"method": "turn/completed", "params": {
                "threadId": thread, "turn": {"id": turn, "status": state}}})
        await wait_until(lambda: not r.currents)
        assert any(m.get("state") == "cancelled" and m["task_id"] == "task" for m in ws.sent)
        assert any(m.get("state") == "completed" and m["task_id"] == "other" for m in ws.sent)


async def test_buffered_session_delete_marks_thread_before_following_continuation(tmp_path, monkeypatch):
    r, _ = runner(tmp_path)
    r.app = GatedApp("delete")
    r.sessions.add("saved", "old")
    frames = [{"type": "session_delete", "req_id": "delete", "thread_id": "saved"},
              {**START, "continue_thread_id": "saved"}]
    async with control(r, monkeypatch, frames) as ws:
        await asyncio.wait_for(ws.drained.wait(), 2)
        assert any(m.get("state") == "rejected_by_lender" and "削除中" in m["report"] for m in ws.sent)
        assert not r.active_task_ids()
        r.app.release.set()
        await wait_until(lambda: any(m.get("req_id") == "delete" and m["ok"] for m in ws.sent))
        assert not r.sessions.owns("saved")


async def test_failed_interrupt_still_reports_cancellation(tmp_path, monkeypatch):
    r, _ = runner(tmp_path)
    r.app = GatedApp(None)
    await begin(r, "task")

    async def fail(*args):
        raise OSError("interrupt failed")
    r.app.turn_interrupt = fail
    async with control(r, monkeypatch, [CANCEL]) as ws:
        await asyncio.wait_for(ws.drained.wait(), 2)
        await wait_until(lambda: any(m.get("state") == "cancelled" for m in ws.sent))
        assert not r.active_task_ids()


@pytest.mark.parametrize("stage", ["prepare", "thread", "turn"])
async def test_startup_failure_reports_each_queued_instruction(tmp_path, monkeypatch, stage):
    r, _ = runner(tmp_path)
    r.app = GatedApp(stage)
    r.app.fail = True

    async def prepare():
        await r.app.wait("prepare")
    r.prepare = prepare
    async with control(r, monkeypatch, [START, MORE], r.app.entered.wait) as ws:
        await asyncio.wait_for(ws.drained.wait(), 2)
        r.app.release.set()
        await wait_until(lambda: any(m.get("state") == "failed" for m in ws.sent))
        await wait_until(lambda: bool(status_messages(ws)))
        assert len(status_messages(ws)) == 1 and not r.active_task_ids()
        assert not any(c[0] == "turn/steer" for c in r.app.calls)


@pytest.mark.parametrize("state", ["completed", "failed"])
async def test_task_finishing_before_start_reply_reports_undelivered_instruction(tmp_path, monkeypatch, state):
    r, _ = runner(tmp_path)
    r.app = GatedApp("turn")
    async with control(r, monkeypatch, [START, MORE], r.app.entered.wait) as ws:
        await asyncio.wait_for(ws.drained.wait(), 2)
        await r.app.events.put({"method": "turn/completed", "params": {
            "threadId": "thread-1", "turn": {"id": "turn-1", "status": state}}})
        await wait_until(lambda: not r.active_task_ids())
        assert any(m.get("state") == state for m in ws.sent)
        assert len(status_messages(ws)) == 1
        r.app.release.set()
        await wait_until(lambda: not r._incoming)
        assert not any(c[0] == "turn/steer" for c in r.app.calls)
        assert len([c for c in r.app.calls if c[0] == "turn/start"]) == 1


async def test_cancel_during_started_event_before_start_reply_is_reported_immediately(tmp_path, monkeypatch):
    r, _ = runner(tmp_path)
    r.app = GatedApp("turn")

    async def before_cancel():
        await r.app.entered.wait()
        await r.app.events.put({"method": "turn/started", "params": {
            "threadId": "thread-1", "turn": {"id": "turn-1"}}})
        await wait_until(lambda: r.currents["task"].turn_id == "turn-1")

    async with control(r, monkeypatch, [START, CANCEL], before_cancel) as ws:
        await asyncio.wait_for(ws.drained.wait(), 2)
        assert any(m.get("state") == "cancelled" for m in ws.sent)
        assert not r.active_task_ids() and not r.app.release.is_set()
        r.app.release.set()
        await wait_until(lambda: ("turn/interrupt", "thread-1", "turn-1") in r.app.calls)
