"""Local and remote update waits use the usual task records and final notifications."""

import asyncio
import time

import pytest

from codex_bridge.agent.runner import AgentRunner, TaskSlot
from codex_bridge.agent.sessions import SessionRegistry
from codex_bridge.agent.usage import QuotaGuard
from codex_bridge.host import tasks
from codex_bridge.host.paths import ToolError
from codex_bridge.updater import SWITCH_WAIT


class Link:
    def __init__(self, manager, name):
        self.manager, self.name = manager, name
        self.messages = []
        self.link_id = manager.attach(name, self.send)

    async def send(self, message):
        self.messages.append(message)

    async def push(self, type_, **fields):
        await self.manager.on_agent_message(self.link_id, {"type": type_, **fields})

    def detach(self):
        self.manager.detach(self.link_id)

    @property
    def starts(self):
        return [m for m in self.messages if m["type"] == "task_start"]


@pytest.fixture
def manager(tmp_path):
    return tasks.TaskManager(tmp_path, lambda task_id: [])


async def test_named_request_stays_with_lender_through_restart(manager):
    alice, bob = Link(manager, "alice"), Link(manager, "bob")
    await alice.push("hello", protocol=5, update_waiting=True, agent_version="0.3.1")
    await bob.push("hello", protocol=4)
    pending = await manager.start("wait for alice", lender="alice")
    automatic = await manager.start("find someone free")
    assert bob.starts[-1]["task_id"] == automatic
    assert manager.status(pending)["state"] == "update_waiting"
    assert manager.status(pending)["message"] == "アップデート待ち"
    assert pending not in manager.active_task_ids()
    await manager.resume_update_waits()
    assert not alice.starts
    alice.detach()
    assert next(row for row in manager.lenders() if row["name"] == "alice")["update_waiting"]
    reconnecting = await manager.start("also wait for alice", lender="alice")
    await manager.resume_update_waits()
    assert len(bob.starts) == 1
    alice = Link(manager, "alice")
    await manager.resume_update_waits()
    assert not alice.starts
    await alice.push("hello", protocol=5, current_task_id=None, agent_version="0.3.1", capabilities={"max_parallel": 3})
    await manager.resume_update_waits()
    assert [m["task_id"] for m in alice.starts] == [pending, reconnecting]


async def test_lender_refuses_a_task_sent_during_announcement(manager, tmp_path):
    alice, bob = Link(manager, "alice"), Link(manager, "bob")
    await alice.push("hello", protocol=5, agent_version="0.3.1")
    await bob.push("hello", protocol=4)
    slot = TaskSlot()
    slot.update_waiting = True
    replies = []

    async def send(message):
        replies.append(message)
        await alice.push(message["type"], **{k: v for k, v in message.items() if k != "type"})

    runner = AgentRunner(object(), QuotaGuard({}), SessionRegistry(tmp_path / "sessions.json"), send, slot=slot)
    task = await manager.start("sent before borrower learns the state", lender="alice")
    await runner.handle(alice.starts[0])
    assert not runner.active_task_ids()
    assert replies[-1]["state"] == "rejected_by_lender"
    assert replies[-1]["report"] == SWITCH_WAIT
    assert manager.status(task)["state"] == "update_waiting"
    assert not bob.starts
    slot.update_waiting = False
    await runner.send_busy_status()
    await manager.resume_update_waits()
    assert [m["task_id"] for m in alice.starts] == [task, task]
    assert not bob.starts


async def test_automatic_request_refused_if_all_lenders_are_updating(manager):
    alice = Link(manager, "alice")
    await alice.push("hello", protocol=5, update_waiting=True)
    with pytest.raises(ToolError, match=SWITCH_WAIT):
        await manager.start("anyone")
    assert not alice.starts and not manager.tasks


async def test_continuation_waits_for_owner_and_keeps_thread(manager):
    alice, bob = Link(manager, "alice"), Link(manager, "bob")
    await alice.push("hello", protocol=5)
    await bob.push("hello", protocol=4)
    first = await manager.start("first", lender="alice")
    await alice.push("task_state", task_id=first, state="completed", thread_id="conversation")
    await alice.push("lender_status", update_waiting=True)
    alice.detach()
    following = await manager.start("continue", continue_task_id=first)
    assert manager.status(following)["lender"] == "alice"
    assert manager.status(following)["state"] == "update_waiting"
    alice = Link(manager, "alice")
    await alice.push("hello", protocol=5, agent_version="0.3.1")
    await manager.resume_update_waits()
    assert alice.starts[0]["continue_thread_id"] == "conversation"
    assert not bob.starts


async def test_local_wait_survives_restart_and_controls_keep_working(manager, tmp_path):
    alice = Link(manager, "alice")
    await alice.push("hello", protocol=4)
    active = await manager.start("running", lender="alice")
    manager.update_waiting = lambda: True
    deferred = await manager.start("send after restart", lender="alice", origin="codex", session="s")
    automatic = await manager.start("select after restart")
    to_cancel = await manager.start("cancel before sending")
    await manager.followup(active, "keep working")
    await manager.cancel(active)
    await manager.cancel(to_cancel)
    assert [m["type"] for m in alice.messages][-2:] == ["task_followup", "task_cancel"]
    assert manager.status(to_cancel)["state"] == "cancelled"
    await alice.push("task_state", task_id=active, state="cancelled")
    await manager.resume_update_waits()
    assert len(alice.starts) == 1

    restarted = tasks.TaskManager(tmp_path, lambda task_id: [])
    assert restarted.status(deferred)["state"] == "update_waiting"
    assert restarted.tasks[deferred].session == "s"
    alice = Link(restarted, "alice")
    await alice.push("hello", protocol=4)
    await restarted.resume_update_waits()
    assert alice.starts[0]["task_id"] == deferred
    await alice.push("task_state", task_id=deferred, state="completed")
    await restarted.resume_update_waits()
    assert alice.starts[-1]["task_id"] == automatic


@pytest.mark.parametrize("local", [True, False])
async def test_update_wait_timeout_notifies_failure(manager, monkeypatch, local, tmp_path):
    from codex_bridge.host.approvals import Approvals
    from codex_bridge.host.notify import Notifier
    from codex_bridge.host.policy import PolicyStore

    monkeypatch.setattr(tasks, "UPDATE_WAIT", 0)
    notifier = Notifier(manager, Approvals(PolicyStore(tmp_path / "policy.yaml")))
    alice, bob = Link(manager, "alice"), Link(manager, "bob")
    await alice.push("hello", update_waiting=True)
    await bob.push("hello")
    manager.update_waiting = lambda: local
    task = await manager.start("time out", lender="alice", origin="codex")
    await manager.resume_update_waits()
    result = manager.result(task)
    assert result["state"] == "failed"
    reason = tasks.LOCAL_UPDATE_TIMEOUT if local else tasks.REMOTE_UPDATE_TIMEOUT.format("alice")
    assert result["report"] == reason
    assert not alice.starts and not bob.starts
    notice = await notifier.wait(task_id=task, timeout=1)
    assert notice["notices"][0]["state"] == "failed"


async def test_offline_drain_grace_is_preserved_across_restart(manager, tmp_path):
    alice = Link(manager, "alice")
    await alice.push("hello", protocol=5)
    task = await manager.start("work", lender="alice")
    alice.detach()
    detached = manager.tasks[task].offline_since
    assert detached is not None and manager.blocks_update()
    old = time.time() - tasks.UPDATE_WAIT - 1
    manager.tasks[task].offline_since = old
    manager._save(manager.tasks[task])
    restarted = tasks.TaskManager(tmp_path, lambda task_id: [])
    assert restarted.tasks[task].offline_since == old
    assert restarted.active_task_ids() == [task]
    assert not restarted.blocks_update()
    alice = Link(restarted, "alice")
    await alice.push("hello", protocol=5, active_task_ids=[task])
    assert restarted.blocks_update()
    assert restarted.tasks[task].offline_since is None


async def test_legacy_offline_record_uses_stored_record_age(manager, tmp_path):
    alice = Link(manager, "alice")
    await alice.push("hello")
    task = await manager.start("work", lender="alice")
    alice.detach()
    record = manager.tasks[task]
    record.offline_since = None
    manager._save(record)
    import os
    old = time.time() - tasks.UPDATE_WAIT - 1
    os.utime(manager.dir / f"{task}.json", (old, old))
    restarted = tasks.TaskManager(tmp_path, lambda task_id: [])
    assert not restarted.blocks_update()


async def test_invalid_waiting_target_is_rejected_before_spooling(manager):
    manager.invited_names = lambda: {"alice"}
    manager.update_waiting = lambda: True
    with pytest.raises(ToolError, match="見つかりません"):
        await manager.start("unknown", lender="typo")
    alice = Link(manager, "alice")
    await alice.push("hello", protocol=5, capabilities={"model_select": True},
                     models=[{"model": "allowed", "efforts": ["high"]}])
    with pytest.raises(ToolError, match="指定のモデルと推論の強さ"):
        await manager.start("unsupported model", lender="alice", model="unavailable")
    assert not manager.tasks


async def test_last_routing_error_survives_restart_and_reports_on_expiry(manager, tmp_path):
    alice = Link(manager, "alice")
    await alice.push("hello")
    manager.update_waiting = lambda: True
    task = await manager.start("for alice", lender="alice")
    restarted = tasks.TaskManager(tmp_path, lambda task_id: [])
    await restarted.resume_update_waits()
    saved = tasks.TaskManager(tmp_path, lambda task_id: [])
    saved.tasks[task].local_update_deadline = time.time() - 1
    await saved.resume_update_waits()
    assert saved.result(task)["report"] == tasks.NOT_CONNECTED


async def test_update_wait_pump_logs_unexpected_exception_and_keeps_running(manager, monkeypatch):
    logs = []
    resumed = asyncio.Event()

    async def resume():
        if not logs:
            raise ValueError("unexpected wait error")
        resumed.set()

    manager.log = lambda level, line: logs.append((level, line))
    monkeypatch.setattr(manager, "resume_update_waits", resume)
    pump = asyncio.create_task(manager.run_update_waits())
    try:
        await asyncio.wait_for(resumed.wait(), 3)
    finally:
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)
    assert logs[0][0] == "error" and "unexpected wait error" in logs[0][1]
