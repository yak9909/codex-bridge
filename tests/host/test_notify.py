import asyncio
import json
from types import SimpleNamespace

import pytest

from codex_bridge.host.approvals import Approvals
from codex_bridge.host.main import stop_servers
from codex_bridge.host.notify import CODEX_MAX_AGE, REPORT_MAX, Notifier
from codex_bridge.host.policy import PolicyStore
from codex_bridge.host.tasks import TaskManager, iso_local


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
    return TaskManager(tmp_path / "data", touched_fn=lambda tid: [])


@pytest.fixture
def approvals(tmp_path):
    return Approvals(PolicyStore(tmp_path / "policy.yaml"))


@pytest.fixture
def notifier(mgr, approvals):
    return Notifier(mgr, approvals)


@pytest.fixture
async def agent(mgr):
    a = FakeAgent(mgr)
    await a.push("hello", protocol=3, capabilities={"model_select": True},
                 models=[{"model": "m1", "efforts": ["low", "high"], "is_default": True}])
    return a


async def test_final_notice_once(mgr, agent, notifier):
    tid = await mgr.start("work", model="m1", effort="low")
    waits = [asyncio.create_task(notifier.wait(task_id=tid, timeout=1)) for _ in range(2)]
    await asyncio.sleep(0)
    assert notifier.waiting(tid) == 2
    await agent.finish(tid)
    results = await asyncio.wait_for(asyncio.gather(*waits), 1)
    assert sorted(r["status"] for r in results) == ["none", "notice"]
    notice = next(r for r in results if r["status"] == "notice")["notices"][0]
    assert notice == {"kind": "final", "task_id": tid, "lender": "friend", "model": "m1",
                      "effort": "low", "state": "completed", "report": "done",
                      "report_truncated": False, "resume_at": None}
    assert mgr.result(tid)["state"] == "completed"
    assert json.loads((mgr.dir / f"{tid}.json").read_text(encoding="utf-8"))["notified_final"] is True


async def test_wakes_promptly_on_change(mgr, agent, notifier):
    tid = await mgr.start("work")
    wait = asyncio.create_task(notifier.wait(task_id=tid, timeout=30))
    await asyncio.sleep(0.05)
    await agent.finish(tid)
    assert (await asyncio.wait_for(wait, 1))["status"] == "notice"


async def test_approval_notice_carries_full_summary_and_cwd(mgr, agent, approvals, notifier):
    tid = await mgr.start("work", model="m1", effort="low")
    await agent.push("event", task_id=tid, kind="model", data={"model": "actual", "effort": "high"})
    wait = asyncio.create_task(notifier.wait(task_id=tid, timeout=30))
    await asyncio.sleep(0)
    summary = "npm test -- --long " + "x" * 3000
    approval = approvals.request("command", summary, tid, cwd="web")
    result = await asyncio.wait_for(wait, 1)
    assert result == {"status": "notice", "notices": [
        {"kind": "approval", "task_id": tid, "lender": "friend", "model": "actual", "effort": "high",
         "approval": {"id": approval.id, "kind": "command", "summary": summary, "cwd": "web"}}]}


async def test_approval_notified_once_then_waits_again(mgr, agent, approvals, notifier):
    tid = await mgr.start("work")
    approvals.request("command", "npm test", tid)
    assert (await notifier.wait(task_id=tid))["status"] == "notice"
    assert await notifier.wait(task_id=tid, timeout=0.2) == {"status": "timeout", "notices": []}


async def test_timeout_status(mgr, agent, notifier):
    tid = await mgr.start("work")
    assert await notifier.wait(task_id=tid, timeout=0.02) == {"status": "timeout", "notices": []}


async def test_report_truncated(mgr, agent, notifier):
    tid = await mgr.start("work", model="m1", effort="low")
    await agent.push("event", task_id=tid, kind="model", data={"model": "actual", "effort": "high"})
    await agent.push("task_state", task_id=tid, state="suspended_quota", report="x" * 2500,
                     resume_at=1800000000)
    notice = (await notifier.wait(task_id=tid))["notices"][0]
    assert notice["report"] == "x" * REPORT_MAX == "x" * 2000
    assert notice["report_truncated"] is True
    assert (notice["model"], notice["effort"]) == ("actual", "high")
    assert notice["resume_at"] == iso_local(1800000000)
    assert len(mgr.result(tid)["report"]) == 2500


async def test_unknown_task_is_none(notifier):
    assert await notifier.wait(task_id="unknown") == {"status": "none", "notices": []}


async def test_codex_none_when_nothing_to_wait_for(mgr, agent, notifier):
    assert await notifier.wait(origin="codex") == {"status": "none", "notices": []}
    await mgr.start("claude work", origin="claude")
    assert (await notifier.wait(origin="codex"))["status"] == "none"


async def test_codex_ignores_offline_awaiting_and_old(mgr, approvals):
    now = 1800000000
    notifier = Notifier(mgr, approvals, clock=lambda: now)
    for i, state in enumerate(("agent_offline", "awaiting_lender", "running")):
        agent = FakeAgent(mgr, f"friend{i}")
        await agent.push("hello")
        tid = await mgr.start("work", lender=f"friend{i}", origin="codex")
        mgr.tasks[tid].created = now - (25 * 3600 if state == "running" else 0)
        await agent.push("task_state", task_id=tid, state=state)
    assert await notifier.wait(origin="codex") == {"status": "none", "notices": []}


async def test_codex_returns_pending_final_immediately(mgr, agent, notifier):
    tid = await mgr.start("work", origin="codex")
    await agent.finish(tid)
    result = await notifier.wait(origin="codex")
    assert result["status"] == "notice" and result["notices"][0]["task_id"] == tid
    assert (await notifier.wait(origin="codex"))["status"] == "none"


async def test_codex_ignores_old_untaken_final(mgr, agent, approvals):
    tid = await mgr.start("work", origin="codex")
    notifier = Notifier(mgr, approvals, clock=lambda: mgr.tasks[tid].created + 25 * 3600)
    await agent.finish(tid)
    assert await notifier.wait(origin="codex") == {"status": "none", "notices": []}
    assert mgr.tasks[tid].notified_final is False


async def test_codex_session_filter(mgr, agent, notifier):
    # F4 is META sessionId: the hook and MCP caller share this session value.
    tid = await mgr.start("work", origin="codex", session="s1")
    assert (await notifier.wait(origin="codex", session="s2"))["status"] == "none"
    await agent.finish(tid)
    assert (await notifier.wait(origin="codex", session="s2"))["status"] == "none"
    result = await notifier.wait(origin="codex", session="s1")
    assert result["status"] == "notice" and result["notices"][0]["task_id"] == tid


async def test_codex_sessionless_task_matches_any_session(mgr, agent, notifier):
    tid = await mgr.start("work", origin="codex")
    await agent.finish(tid)
    result = await notifier.wait(origin="codex", session="s1", timeout=0.1)
    assert result["status"] == "notice" and result["notices"][0]["task_id"] == tid


async def test_new_codex_wait_supersedes_same_session(mgr, agent, notifier):
    tid = await mgr.start("work", origin="codex", session="s1")
    first = asyncio.create_task(notifier.wait(origin="codex", session="s1", timeout=10))
    await asyncio.sleep(0)
    second = asyncio.create_task(notifier.wait(origin="codex", session="s1", timeout=10))
    try:
        assert await asyncio.wait_for(asyncio.shield(first), 1) == {"status": "none", "notices": []}
        assert not second.done() and not mgr.tasks[tid].notified_final
        await agent.finish(tid)
        result = await asyncio.wait_for(second, 1)
        assert result["status"] == "notice" and result["notices"][0]["task_id"] == tid
    finally:
        for task in (first, second):
            task.cancel()
        await asyncio.gather(first, second, return_exceptions=True)


async def test_any_unwatched_skips_watched_tasks(mgr, agent, notifier):
    a = await mgr.start("A", origin="claude", lender="friend")
    other = FakeAgent(mgr, "other")
    await other.push("hello")
    b = await mgr.start("B", origin="claude", lender="other")
    watched = asyncio.create_task(notifier.wait(task_id=a, timeout=30))
    await asyncio.sleep(0)
    unwatched = asyncio.create_task(notifier.wait(origin="claude", any_unwatched=True, timeout=0.3))
    await asyncio.sleep(0)
    assert notifier.waiting(a) == notifier.waiting(b) == 1
    await agent.finish(a)
    assert (await asyncio.wait_for(watched, 1))["status"] == "notice"
    assert (await asyncio.wait_for(unwatched, 1))["status"] == "timeout"
    await other.finish(b)
    result = await notifier.wait(origin="claude", any_unwatched=True)
    assert [n["task_id"] for n in result["notices"]] == [b]
    assert (await notifier.wait(origin="claude", any_unwatched=True))["status"] == "none"


async def test_waiter_count_released_after_wait(mgr, agent, notifier):
    tid = await mgr.start("work")
    assert (await notifier.wait(task_id=tid, timeout=0.01))["status"] == "timeout"
    assert notifier.waiting(tid) == 0
    wait = asyncio.create_task(notifier.wait(origin="claude", any_unwatched=True))
    await asyncio.sleep(0)
    assert notifier.waiting(tid) == 1  # legacy origin=None is included
    wait.cancel()
    with pytest.raises(asyncio.CancelledError):
        await wait
    assert notifier.waiting(tid) == 0


async def test_disconnected_waiter_does_not_consume(mgr, agent, notifier):
    tid = await mgr.start("work")
    approval = notifier.approvals.request("command", "npm test", tid)
    await agent.finish(tid)

    async def disconnected():
        return True

    assert await notifier.wait(task_id=tid, disconnected=disconnected) == {"status": "gone", "notices": []}
    assert notifier.waiting(tid) == 0 and mgr.tasks[tid].notified_final is False
    result = await notifier.wait(task_id=tid)
    assert result["status"] == "notice"
    assert [n["kind"] for n in result["notices"]] == ["approval", "final"]
    assert result["notices"][0]["approval"]["id"] == approval.id


async def test_close_wakes_waiters(mgr, agent, notifier):
    tid = await mgr.start("work")
    wait = asyncio.create_task(notifier.wait(task_id=tid, timeout=30))
    await asyncio.sleep(0)
    notifier.close()
    closing = {"status": "closing", "notices": []}
    assert await asyncio.wait_for(wait, 1) == closing
    assert notifier.waiting(tid) == 0
    assert await notifier.wait(task_id=tid) == closing
    assert await notifier.wait(task_id="unknown") == closing


async def test_restart_does_not_renotify_old_finals(tmp_path, mgr, agent, notifier, approvals):
    tid = await mgr.start("work")
    await agent.finish(tid)
    restarted = TaskManager(tmp_path / "data", touched_fn=lambda tid: [])
    assert (await Notifier(restarted, approvals).wait(task_id=tid))["status"] == "none"


async def test_stop_servers_closes_notifier(mgr, agent, notifier):
    tid = await mgr.start("work")
    wait = asyncio.create_task(notifier.wait(task_id=tid, timeout=30))
    await asyncio.sleep(0)
    servers = [SimpleNamespace(should_exit=False), SimpleNamespace(should_exit=False)]
    stop_servers(SimpleNamespace(notifier=notifier), *servers)
    assert all(server.should_exit for server in servers)
    assert await asyncio.wait_for(wait, 1) == {"status": "closing", "notices": []}


async def test_disconnect_during_wait_releases_count(mgr, agent, notifier, monkeypatch):
    monkeypatch.setattr("codex_bridge.host.notify.DISCONNECT_CHECK", 0.01)
    tid = await mgr.start("work")
    gone = False

    async def disconnected():
        return gone

    wait = asyncio.create_task(notifier.wait(task_id=tid, disconnected=disconnected, timeout=30))
    await asyncio.sleep(0)
    assert notifier.waiting(tid) == 1
    gone = True
    assert await asyncio.wait_for(wait, 1) == {"status": "gone", "notices": []}
    assert notifier.waiting(tid) == 0
    await agent.finish(tid)
    assert (await notifier.wait(task_id=tid))["status"] == "notice"


async def test_codex_recomputes_targets_on_wake(mgr, agent, notifier):
    tid = await mgr.start("work", origin="codex")
    wait = asyncio.create_task(notifier.wait(origin="codex", timeout=30))
    await asyncio.sleep(0)
    await agent.push("task_state", task_id=tid, state="awaiting_lender")
    assert await asyncio.wait_for(wait, 1) == {"status": "none", "notices": []}


async def test_codex_pending_approval_and_age_boundary(mgr, agent, approvals):
    tid = await mgr.start("work", origin="codex")
    notifier = Notifier(mgr, approvals, clock=lambda: mgr.tasks[tid].created + CODEX_MAX_AGE)
    await agent.push("task_state", task_id=tid, state="agent_offline")
    approval = approvals.request("command", "npm test", tid)
    result = await notifier.wait(origin="codex")
    assert result["notices"][0]["approval"]["id"] == approval.id
    assert (await notifier.wait(origin="codex"))["status"] == "none"


async def test_codex_targets_recomputed_only_on_change(mgr, agent, approvals, monkeypatch):
    monkeypatch.setattr("codex_bridge.host.notify.DISCONNECT_CHECK", 0.01)
    tid = await mgr.start("work", origin="codex")
    now = mgr.tasks[tid].created
    notifier = Notifier(mgr, approvals, clock=lambda: now)

    async def disconnected():
        return False

    wait = asyncio.create_task(notifier.wait(origin="codex", timeout=30, disconnected=disconnected))
    try:
        await asyncio.sleep(0)
        now += 25 * 3600
        await asyncio.sleep(0.05)
        assert not wait.done()  # the disconnect timer must not poll task eligibility
        notifier.changed()
        assert await asyncio.wait_for(wait, 1) == {"status": "none", "notices": []}
    finally:
        if not wait.done():
            wait.cancel()
            with pytest.raises(asyncio.CancelledError):
                await wait
