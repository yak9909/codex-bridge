"""Pre-approval by the lender (design 4.5, 6): with it on, a new task and a follow-up are held back until the
lender decides, and the borrower is told so (awaiting_lender / rejected_by_lender)."""

import asyncio
import copy

import pytest

from codex_bridge.agent.control_client import ControlClient
from codex_bridge.agent.runner import AgentRunner
from codex_bridge.agent.sessions import SessionRegistry
from codex_bridge.agent.usage import QuotaGuard
from codex_bridge.common import protocol
from codex_bridge.common.invite import Invite
from fake_helpers import Fake
from fakes import notifications as n

DEFAULTS = {
    "limits": {"five_hour": 80, "weekly": 80, "force": False},
    "features": {"web_search": False, "image_generation": False, "multi_agent": False, "goals": False},
    "pre_approval": {"enabled": False, "timeout_min": 30},
    "disposable": False, "workdir": None, "log_retention_days": 30,
}
RUNNING = {"after": {"turn/start": [n.turn_started()], "turn/steer": [n.agent_completed("追加も済み"), n.turn_completed()],
                     "turn/interrupt": [n.turn_completed("interrupted")]}}
DONE = {"after": {"turn/start": [n.turn_started(), n.agent_completed("できました"), n.turn_completed()]}}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Env:
    def __init__(self, tmp_path, script, on=True, timeout_min=30):
        self.fake = Fake(tmp_path, script)
        self.sent: list[dict] = []
        self.logged: list[dict] = []
        self.clock = Clock()

        async def send(m):
            self.sent.append(m)
        self.runner = AgentRunner(self.fake.app, QuotaGuard({"five_hour": 90, "weekly": 95}),
                                  SessionRegistry(tmp_path / "sessions.json"), send, usage_interval=3600,
                                  tick_interval=3600, on_log=self.logged.append, clock=self.clock)
        self.setting(on, timeout_min)

    def setting(self, on=True, timeout_min=30):
        s = copy.deepcopy(DEFAULTS)
        s["pre_approval"] = {"enabled": on, "timeout_min": timeout_min}
        self.runner.apply_settings(s)

    def states(self):
        return [m["state"] for m in self.sent if m["type"] == "task_state"]

    def last_state(self, task_id=None):
        return [m for m in self.sent if m["type"] == "task_state" and task_id in (None, m["task_id"])][-1]

    async def wait_state(self, state, timeout=5):
        for _ in range(int(timeout / 0.05)):
            found = [m for m in self.sent if m["type"] == "task_state" and m["state"] == state]
            if found:
                return found[-1]
            await asyncio.sleep(0.05)
        raise AssertionError(f"no {state}: {self.states()}")

    async def start(self, task_id="t1", prompt="ファイルを直して"):
        await self.runner.handle({"type": "task_start", "task_id": task_id, "prompt": prompt,
                                  "continue_thread_id": None})

    async def followup(self, text="こうもして", task_id="t1"):
        await self.runner.handle({"type": "task_followup", "task_id": task_id, "text": text})

    async def running_task(self):
        """A task the lender has approved and that is working now (the turn does not end by itself)."""
        await self.start()
        await self.runner.decide_pending(True)
        await self.wait_state("running")
        await self.fake.wait_for(lambda: self.runner.cur and self.runner.cur.turn_id)


@pytest.fixture
async def env(tmp_path, request):
    e = Env(tmp_path, getattr(request, "param", RUNNING))
    await e.fake.app.start()
    await e.runner.start()
    yield e
    await e.runner.stop()
    await e.fake.app.stop()


# ---- off: nothing changes ---------------------------------------------------------------------------
@pytest.mark.parametrize("env", [DONE], indirect=True)
async def test_with_pre_approval_off_a_task_starts_at_once_as_before(env):
    env.setting(on=False)
    await env.start()
    await env.wait_state("completed")
    assert "awaiting_lender" not in env.states() and env.runner.pending_approval is None
    assert env.fake.received("thread/start") is not None


async def test_decide_pending_with_nothing_pending_does_nothing(env):
    assert await env.runner.decide_pending(True) is False
    assert await env.runner.decide_pending(False, "x") is False
    assert env.sent == []


# ---- a new task -------------------------------------------------------------------------------------
async def test_a_new_task_is_held_back_and_announced_before_codex_sees_it(env):
    await env.start(prompt="ファイルを直して")
    assert env.states() == ["awaiting_lender"]
    assert env.last_state()["task_id"] == "t1" and env.last_state()["seq"]
    assert env.fake.received("thread/start") is None and env.fake.received("turn/start") is None
    p = env.runner.pending_approval
    assert set(p) == {"task_id", "prompt", "kind", "created"}
    assert (p["task_id"], p["prompt"], p["kind"]) == ("t1", "ファイルを直して", "start") and p["created"] > 0
    assert env.runner.cur is None
    assert {"type": "task_prompt", "task_id": "t1", "prompt": "ファイルを直して"} in env.logged  # the lender can read it


@pytest.mark.parametrize("env", [DONE], indirect=True)
async def test_approving_starts_the_task_as_usual(env):
    await env.start()
    assert await env.runner.decide_pending(True) is True
    done = await env.wait_state("completed")
    assert env.states() == ["awaiting_lender", "running", "completed"] and done["report"] == "できました"
    assert env.fake.received("turn/start")["input"][0]["text"] == "ファイルを直して"
    assert env.runner.pending_approval is None
    assert len([m for m in env.logged if m.get("type") == "task_prompt"]) == 1  # shown once, not twice


async def test_rejecting_reports_rejected_by_lender_with_the_reason_and_codex_never_starts(env):
    await env.start()
    await env.runner.decide_pending(False, "今は使っているので")
    r = env.last_state()
    assert r["state"] == "rejected_by_lender" and r["task_id"] == "t1" and "今は使っているので" in r["report"]
    assert env.states() == ["awaiting_lender", "rejected_by_lender"]
    assert env.fake.received("thread/start") is None
    assert env.runner.pending_approval is None and env.runner.cur is None
    assert await env.runner.decide_pending(True) is False  # decided once


async def test_rejecting_without_a_reason_still_says_who_refused(env):
    await env.start()
    await env.runner.decide_pending(False)
    assert "拒否" in env.last_state()["report"]


async def test_after_a_rejection_the_next_task_can_come(env):
    await env.start()
    await env.runner.decide_pending(False)
    env.setting(on=False)
    await env.start("t2")
    await env.wait_state("running")
    assert env.last_state("t2")["state"] == "running"


# ---- time out -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["start", "followup"])
async def test_no_timeout_means_never_expires(env, kind):
    if kind == "followup":
        await env.running_task()
    env.setting(timeout_min=None)
    if kind == "followup":
        await env.followup()
    else:
        await env.start()
    pending = env.runner.pending_approval
    assert pending is not None, env.logged
    before = list(env.sent)
    env.clock.now += 365 * 24 * 60 * 60
    await env.runner.expire_pending()
    await env.runner.expire_pending()
    assert env.runner.pending_approval == pending and env.sent == before
    method = "turn/steer" if kind == "followup" else "turn/start"
    assert env.fake.received(method) is None
    assert await env.runner.decide_pending(True) is True
    await env.fake.wait_for(lambda: env.fake.received(method))
    assert env.runner.pending_approval is None


async def test_an_undecided_request_is_rejected_when_the_time_is_up(env):
    env.setting(on=True, timeout_min=5)
    await env.start()
    env.clock.now += 5 * 60 - 1
    await env.runner.expire_pending()
    assert env.states() == ["awaiting_lender"] and env.runner.pending_approval is not None
    env.clock.now += 1
    await env.runner.expire_pending()
    r = env.last_state()
    assert r["state"] == "rejected_by_lender" and "5 分" in r["report"]
    assert env.runner.pending_approval is None and env.fake.received("thread/start") is None
    await env.runner.expire_pending()  # nothing left: nothing more is sent
    assert env.states() == ["awaiting_lender", "rejected_by_lender"]


@pytest.mark.parametrize("kind", ["start", "followup"])
@pytest.mark.parametrize("timeout_min", [None, 5])
async def test_timeout_message_uses_the_minutes_the_request_was_held_with(env, kind, timeout_min):
    if kind == "followup":
        await env.running_task()
        await env.followup()
    else:
        await env.start()
    env.setting(timeout_min=timeout_min)
    env.clock.now += 30 * 60 - 1
    await env.runner.expire_pending()
    assert env.runner.pending_approval is not None
    env.clock.now += 1
    await env.runner.expire_pending()
    if kind == "followup":
        report = [m for m in env.sent if m["type"] == "event" and m["kind"] == "status"][-1]["data"]["text"]
        assert env.last_state()["state"] == "running"
        assert env.fake.received("turn/steer") is None
    else:
        report = env.last_state()["report"]
        assert env.last_state()["state"] == "rejected_by_lender"
        assert env.fake.received("thread/start") is None
    assert "Codex を貸している人が 30 分のあいだ承認しなかったため、拒否しました" in report
    assert env.runner.pending_approval is None


async def test_the_default_wait_is_30_minutes_and_the_tick_loop_checks_it(tmp_path):
    e = Env(tmp_path, RUNNING)
    e.runner.tick_interval = 0.02
    await e.fake.app.start()
    await e.runner.start()
    try:
        await e.start()
        e.clock.now += 30 * 60
        await e.wait_state("rejected_by_lender")
    finally:
        await e.runner.stop()
        await e.fake.app.stop()


# ---- a follow-up --------------------------------------------------------------------------------------
async def test_a_follow_up_is_held_too_and_goes_on_when_approved(env):
    await env.running_task()
    await env.followup("こうもして")
    assert env.fake.received("turn/steer") is None
    assert env.last_state()["state"] == "awaiting_lender" and env.last_state()["task_id"] == "t1"
    p = env.runner.pending_approval
    assert (p["task_id"], p["prompt"], p["kind"]) == ("t1", "こうもして", "followup")
    await env.runner.decide_pending(True)
    steer = await env.fake.wait_for(lambda: env.fake.received("turn/steer"))
    assert steer["input"][0]["text"] == "こうもして"
    await env.wait_state("completed")
    # the borrower sees it working again once the follow-up is let through
    assert env.states() == ["awaiting_lender", "running", "awaiting_lender", "running", "completed"]
    assert env.runner.pending_approval is None


async def test_a_rejected_follow_up_is_not_given_to_codex_and_the_task_goes_on(env):
    await env.running_task()
    await env.followup("こうもして")
    await env.runner.decide_pending(False, "それは困る")
    await asyncio.sleep(0.2)
    assert env.fake.received("turn/steer") is None
    assert env.runner.cur is not None and env.runner.cur.task_id == "t1"
    assert env.states() == ["awaiting_lender", "running", "awaiting_lender", "running"]  # not rejected as a whole
    status = [m for m in env.sent if m["type"] == "event" and m["kind"] == "status"][-1]
    assert "それは困る" in status["data"]["text"]


async def test_a_second_follow_up_while_one_waits_is_not_passed_on_and_is_said_so(env):
    await env.running_task()
    await env.followup("一つ目")
    await env.followup("二つ目")
    assert env.runner.pending_approval["prompt"] == "一つ目"
    status = [m for m in env.sent if m["type"] == "event" and m["kind"] == "status"][-1]
    assert "承認待ち" in status["data"]["text"]
    await env.runner.decide_pending(True)
    await env.fake.wait_for(lambda: env.fake.received("turn/steer"))
    assert env.fake.received("turn/steer")["input"][0]["text"] == "一つ目"


async def test_a_follow_up_for_a_task_still_awaiting_its_start_is_not_passed_on(env):
    await env.start()
    await env.followup("先に")
    assert env.runner.pending_approval["kind"] == "start" and env.runner.pending_approval["prompt"] == "ファイルを直して"
    assert any(m["type"] == "event" and "承認待ち" in m["data"]["text"] for m in env.sent)


async def test_a_pending_follow_up_goes_when_the_task_ends_and_a_late_decision_does_nothing(env):
    await env.running_task()
    await env.followup("こうもして")
    await env.runner.handle({"type": "task_cancel", "task_id": "t1"})
    await env.wait_state("cancelled")
    assert env.runner.pending_approval is None and env.runner.cur is None
    assert await env.runner.decide_pending(True) is False
    await asyncio.sleep(0.1)
    assert env.fake.received("turn/steer") is None


# ---- cancel, busy -------------------------------------------------------------------------------------
async def test_cancelling_a_task_that_waits_for_approval_ends_it_at_once(env):
    await env.start()
    await env.runner.handle({"type": "task_cancel", "task_id": "t1"})
    assert env.last_state()["state"] == "cancelled" and env.runner.pending_approval is None
    assert env.fake.received("thread/start") is None
    assert await env.runner.decide_pending(True) is False


async def test_a_cancel_for_another_task_leaves_the_pending_one(env):
    await env.start()
    await env.runner.handle({"type": "task_cancel", "task_id": "other"})
    assert env.runner.pending_approval["task_id"] == "t1" and env.states() == ["awaiting_lender"]


async def test_a_second_task_while_one_waits_is_refused_not_queued(env):
    await env.start()
    await env.start("t2", "別の")
    assert env.last_state("t2")["state"] == "failed" and "作業中" in env.last_state("t2")["report"]
    assert env.runner.pending_approval["task_id"] == "t1"


# ---- the exe ends ------------------------------------------------------------------------------------
async def test_when_the_exe_ends_a_pending_request_is_sent_as_rejected(env):
    await env.start()
    await env.runner.shutdown()
    r = env.last_state()
    assert r["state"] == "rejected_by_lender" and "終了" in r["report"]
    assert env.runner.pending_approval is None
    await env.runner.shutdown()  # twice is harmless
    assert env.states() == ["awaiting_lender", "rejected_by_lender"]


async def test_the_exe_ending_with_a_pending_follow_up_drops_it_and_the_task_is_left_as_it_was(env):
    await env.running_task()
    await env.followup()
    await env.runner.shutdown()
    assert env.runner.pending_approval is None and env.fake.received("turn/steer") is None
    assert "rejected_by_lender" not in env.states()


# ---- reconnect (Review Focus 4) -----------------------------------------------------------------------
class FakeWs:
    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(protocol.decode(text))


def client_of(env, **kw):
    c = ControlClient(Invite("http://h:1", "tok", "x"), env.runner,
                      hello=kw.get("hello", {"agent_version": "0.2", "protocol": 2}))
    c.ws = FakeWs()
    return c


@pytest.mark.parametrize("kind", ["start", "followup"])
@pytest.mark.parametrize("acknowledged", [False, True])
async def test_no_timeout_pending_is_resent_after_reconnect(env, kind, acknowledged):
    if kind == "followup":
        await env.running_task()
        for message in list(env.sent):
            if "seq" in message:
                await env.runner.handle({"type": "ack", "seq": message["seq"]})
    env.setting(timeout_min=None)
    if kind == "followup":
        await env.followup()
    else:
        await env.start()
    pending = env.runner.pending_approval
    assert pending is not None, env.logged
    first_seq = env.last_state()["seq"]
    if acknowledged:
        await env.runner.handle({"type": "ack", "seq": first_seq})
    env.clock.now += 365 * 24 * 60 * 60
    await env.runner.expire_pending()
    client = client_of(env)
    await client.on_connected()
    hello = next(m for m in client.ws.sent if m["type"] == "hello")
    states = [m for m in client.ws.sent if m["type"] == "task_state"]
    assert hello["current_task_id"] == "t1"
    assert len(states) == (1 if acknowledged else 2)
    assert all(m["state"] == "awaiting_lender" and m["task_id"] == "t1" for m in states)
    assert states[-1]["seq"] > first_seq
    assert client.ws.sent.index(hello) < client.ws.sent.index(states[-1])
    assert env.runner.pending_approval == pending


async def test_after_a_reconnect_hello_names_the_pending_task_and_awaiting_lender_is_sent_again(env):
    await env.start()
    c = client_of(env)
    await c.on_connected()
    hello = next(m for m in c.ws.sent if m["type"] == "hello")
    assert hello["current_task_id"] == "t1"  # not "I have nothing": the host would fail the task
    again = [m for m in c.ws.sent if m["type"] == "task_state"]
    # the old one (not acknowledged) goes again, then, after hello, a new one with a number of its own
    assert [m["state"] for m in again] == ["awaiting_lender", "awaiting_lender"]
    assert again[1]["seq"] > again[0]["seq"] and again[1]["task_id"] == "t1"
    assert c.ws.sent.index(hello) < c.ws.sent.index(again[1])


async def test_the_new_awaiting_lender_comes_even_when_the_old_one_was_acknowledged(env):
    await env.start()
    await env.runner.handle({"type": "ack", "seq": env.last_state()["seq"]})
    c = client_of(env)
    await c.on_connected()
    assert [m["type"] for m in c.ws.sent] == ["hello", "task_state"]
    assert c.ws.sent[1]["state"] == "awaiting_lender" and c.ws.sent[1]["task_id"] == "t1"


async def test_nothing_is_added_after_a_reconnect_when_nothing_is_pending(env):
    c = client_of(env)
    await c.on_connected()
    assert [m["type"] for m in c.ws.sent] == ["hello"] and c.ws.sent[0]["current_task_id"] is None


async def test_a_working_task_is_named_in_hello_and_no_request_is_invented(env):
    await env.running_task()
    for m in list(env.sent):
        await env.runner.handle({"type": "ack", "seq": m["seq"]}) if "seq" in m else None
    c = client_of(env)
    await c.on_connected()
    assert [m["type"] for m in c.ws.sent] == ["hello"] and c.ws.sent[0]["current_task_id"] == "t1"


async def test_a_pending_follow_up_is_said_again_after_a_reconnect(env):
    await env.running_task()
    await env.followup()
    await env.runner.handle({"type": "ack", "seq": env.last_state()["seq"]})
    c = client_of(env)
    await c.on_connected()
    assert next(m for m in c.ws.sent if m["type"] == "hello")["current_task_id"] == "t1"
    states = [m for m in c.ws.sent if m["type"] == "task_state"]
    assert states[-1]["state"] == "awaiting_lender" and states[-1]["seq"] == max(m["seq"] for m in states)
    assert c.ws.sent.index(states[-1]) > c.ws.sent.index(next(m for m in c.ws.sent if m["type"] == "hello"))


# ---- hello because the settings changed (review of Task 9) ------------------------------------------
async def test_a_hello_for_changed_settings_leaves_out_the_task_so_the_host_does_not_reconcile(env):
    await env.start()
    c = client_of(env)
    await c.refresh_hello()
    assert c.ws.sent[0]["type"] == "hello" and "current_task_id" not in c.ws.sent[0]
    assert [m["type"] for m in c.ws.sent] == ["hello"]  # and nothing is said again
    await c.send_hello()
    assert c.ws.sent[1]["current_task_id"] == "t1"
