import asyncio

import pytest

from codex_bridge.agent.models import parse_model_list
from codex_bridge.agent.runner import AgentRunner
from codex_bridge.agent.sessions import SessionRegistry
from codex_bridge.agent.usage import STOP_TEXT, QuotaGuard
from fake_helpers import Fake

TURN_STARTED = {"method": "turn/started", "params": {"threadId": "th1", "turn": {"id": "u1"}}}


def agent_msg(text):
    return {"method": "item/completed", "params": {"threadId": "th1", "turnId": "u1", "completedAtMs": 0,
                                                   "item": {"type": "agentMessage", "id": "m", "text": text}}}


def turn_completed(status="completed"):
    return {"method": "turn/completed", "params": {"threadId": "th1", "turn": {"id": "u1", "status": status}}}


class Env:
    def __init__(self, tmp_path, script):
        self.fake = Fake(tmp_path, script)
        self.sent: list[dict] = []
        self.sessions = SessionRegistry(tmp_path / "sessions.json")
        self.guard = QuotaGuard({"five_hour": 90, "weekly": 95})

        async def send(m):
            self.sent.append(m)
        self.runner = AgentRunner(self.fake.app, self.guard, self.sessions, send,
                                  usage_interval=3600, tick_interval=0.05)

    def of(self, type_):
        return [m for m in self.sent if m["type"] == type_]

    async def wait_state(self, state, timeout=5):
        for _ in range(int(timeout / 0.05)):
            if any(m["state"] == state for m in self.of("task_state")):
                return [m for m in self.of("task_state") if m["state"] == state][-1]
            await asyncio.sleep(0.05)
        raise AssertionError(f"no {state}: {self.of('task_state')}")


@pytest.fixture
async def env(tmp_path, request):
    e = Env(tmp_path, getattr(request, "param", {}))
    await e.fake.app.start()
    await e.runner.start()
    yield e
    await e.runner.stop()
    await e.fake.app.stop()


DONE_SCRIPT = {"after": {"turn/start": [TURN_STARTED, agent_msg("できました"), turn_completed()]}}


@pytest.fixture
async def model_env(env):
    env.runner.catalog = parse_model_list(await env.fake.app.model_list())
    return env


async def test_start_passes_chosen_model_and_effort(model_env):
    env = model_env
    await env.runner.handle({"type": "task_start", "task_id": "t1", "prompt": "do"})
    assert env.fake.received("thread/start")["model"] == "gpt-fake"
    turn = env.fake.received("turn/start")
    assert turn["model"] == "gpt-fake" and turn["effort"] == "medium"


async def test_start_with_requested_effort(model_env):
    env = model_env
    await env.runner.handle({"type": "task_start", "task_id": "t1", "prompt": "do", "effort": "high"})
    assert env.fake.received("turn/start")["effort"] == "high"


async def test_refused_model_rejects_task_and_frees_slot(model_env):
    env = model_env
    await env.runner.handle({"type": "task_start", "task_id": "t1", "prompt": "do", "model": "nope"})
    rejected = await env.wait_state("rejected_by_lender")
    assert "使えるモデル" in rejected["report"]
    assert env.fake.received("thread/start") is None
    assert env.fake.received("turn/start") is None
    assert env.runner.active_task_id() is None


async def test_model_event_after_started(model_env):
    env = model_env
    await env.runner.handle({"type": "task_start", "task_id": "t1", "prompt": "do"})
    events = env.of("event")
    kinds = [event["kind"] for event in events]
    assert kinds[kinds.index("started") + 1] == "model"
    assert next(event["data"] for event in events if event["kind"] == "model") == {
        "model": "gpt-fake", "effort": "medium"}


async def test_reroute_reports_new_model(model_env):
    env = model_env
    await env.runner.handle({"type": "task_start", "task_id": "t1", "prompt": "do"})
    await env.runner._on_event({"method": "model/rerouted", "params": {
        "threadId": "th1", "turnId": "u1", "fromModel": "gpt-fake", "toModel": "gpt-mini",
        "reason": "highRiskCyberActivity"}})
    events = [event for event in env.of("event") if event["kind"] == "model"]
    assert events[-1]["data"] == {"model": "gpt-mini", "effort": "medium", "rerouted_from": "gpt-fake",
                                  "reason": "highRiskCyberActivity"}
    assert env.runner.cur.model == "gpt-mini"


@pytest.mark.parametrize("env", [{"errors": {"turn/steer": {"code": 1, "message": "no turn"}}}], indirect=True)
@pytest.mark.parametrize("active_turn", [False, True])
async def test_followup_new_turn_keeps_model(model_env, active_turn):
    env = model_env
    await env.runner.handle({"type": "task_start", "task_id": "t1", "prompt": "do", "effort": "high"})
    if not active_turn:
        env.runner.cur.turn_id = None
    await env.runner.handle({"type": "task_followup", "task_id": "t1", "text": "also"})
    turn = env.fake.received("turn/start")
    assert turn["input"] == [{"type": "text", "text": "also"}]
    assert turn["model"] == "gpt-fake" and turn["effort"] == "high"


async def test_resume_passes_model(model_env):
    env = model_env
    env.sessions.add("th1", "previous")
    await env.runner.handle({"type": "task_start", "task_id": "t1", "prompt": "more",
                             "continue_thread_id": "th1", "model": "gpt-fake", "effort": "high"})
    assert env.fake.received("thread/resume")["model"] == "gpt-fake"
    turn = env.fake.received("turn/start")
    assert turn["model"] == "gpt-fake" and turn["effort"] == "high"
    assert env.fake.received("thread/start") is None


@pytest.mark.parametrize("env", [{"responses": {"thread/start": {
    "thread": {"id": "th1"}, "model": None, "reasoningEffort": "medium"}}}], indirect=True)
async def test_task_without_model_from_v02_host(env):
    await env.runner.handle({"type": "task_start", "task_id": "t1", "prompt": "do"})
    turn = env.fake.received("turn/start")
    assert turn["effort"] == "medium" and "model" not in turn
    assert "model" not in env.fake.received("thread/start")


@pytest.mark.parametrize("env", [DONE_SCRIPT], indirect=True)
async def test_task_runs_to_completion(env):
    await env.runner.handle({"type": "task_start", "task_id": "t1", "prompt": "do", "continue_thread_id": None})
    done = await env.wait_state("completed")
    assert done["report"] == "できました" and done["thread_id"] == "th1"
    assert env.sessions.owns("th1")
    assert env.fake.received("thread/start")["developerInstructions"].startswith("ファイルの一覧")
    assert "ほかの共有フォルダ内は絶対パス" in env.fake.received("thread/start")["developerInstructions"]
    seqs = [m["seq"] for m in env.sent if "seq" in m]
    assert len(seqs) == len(set(seqs)) and min(seqs) > 1_000_000


@pytest.mark.parametrize("env", [{"after": {"turn/start": [TURN_STARTED]}}], indirect=True)
async def test_followup_uses_turn_steer_with_active_turn(env):
    await env.runner.handle({"type": "task_start", "task_id": "t1", "prompt": "do", "continue_thread_id": None})
    await env.wait_state("running")
    await asyncio.sleep(0.2)
    await env.runner.handle({"type": "task_followup", "task_id": "t1", "text": "also"})
    p = await env.fake.wait_for(lambda: env.fake.received("turn/steer"))
    assert p["expectedTurnId"] == "u1" and p["input"][0]["text"] == "also"


@pytest.mark.parametrize("env", [{"after": {"turn/start": [TURN_STARTED, agent_msg("a"), turn_completed()]}}],
                         indirect=True)
async def test_continue_thread_resumes_owned_thread(env):
    await env.runner.handle({"type": "task_start", "task_id": "t1", "prompt": "do", "continue_thread_id": None})
    await env.wait_state("completed")
    env.sent.clear()
    await env.runner.handle({"type": "task_start", "task_id": "t2", "prompt": "more", "continue_thread_id": "th1"})
    await env.wait_state("completed")
    assert env.fake.received("thread/resume")["threadId"] == "th1"
    assert "ほかの共有フォルダ内は絶対パス" in env.fake.received("thread/resume")["developerInstructions"]


async def test_continue_foreign_thread_fails(env):
    await env.runner.handle({"type": "task_start", "task_id": "t1", "prompt": "x", "continue_thread_id": "theirs"})
    failed = await env.wait_state("failed")
    assert "このツールで作った" in failed["report"]


QUOTA_SCRIPT = {
    "responses": {"account/rateLimits/read": {"rateLimits": {
        "primary": {"usedPercent": 91, "windowDurationMins": 300, "resetsAt": 1000}, "secondary": None}}},
    "after": {"turn/start": [TURN_STARTED],
              "turn/steer": [agent_msg("ここまでやりました"), turn_completed()]},
}


@pytest.mark.parametrize("env", [QUOTA_SCRIPT], indirect=True)
async def test_quota_flow_end_to_end(env):
    await env.runner.handle({"type": "task_start", "task_id": "t1", "prompt": "do", "continue_thread_id": None})
    await env.wait_state("running")
    await asyncio.sleep(0.2)
    await env.runner.refresh_usage()
    assert env.of("usage")[-1]["five_hour"]["used_percent"] == 91
    steer = await env.fake.wait_for(lambda: env.fake.received("turn/steer"))
    assert steer["input"][0]["text"] == STOP_TEXT
    s = await env.wait_state("suspended_quota")
    assert s["resume_at"] == 1000 and s["report"] == "ここまでやりました"


@pytest.mark.parametrize("env", [{"after": {"turn/start": [TURN_STARTED],
                                            "turn/interrupt": [turn_completed("interrupted")]}}], indirect=True)
async def test_cancel_interrupts_then_reports_cancelled(env):
    await env.runner.handle({"type": "task_start", "task_id": "t1", "prompt": "do", "continue_thread_id": None})
    await env.wait_state("running")
    await asyncio.sleep(0.2)
    await env.runner.handle({"type": "task_cancel", "task_id": "t1"})
    await env.wait_state("cancelled")
    assert env.fake.received("turn/interrupt") == {"threadId": "th1", "turnId": "u1"}


@pytest.mark.parametrize("env", [DONE_SCRIPT], indirect=True)
async def test_session_ops_limited_to_own_threads(env):
    await env.runner.handle({"type": "session_read", "req_id": "r1", "thread_id": "someone_elses"})
    r = env.of("reply")[-1]
    assert r["ok"] is False and "このツールで作った" in r["error"]
    assert env.fake.received("thread/read") is None
    await env.runner.handle({"type": "session_delete", "req_id": "r2", "thread_id": "someone_elses"})
    assert env.of("reply")[-1]["ok"] is False and env.fake.received("thread/delete") is None


@pytest.mark.parametrize("env", [DONE_SCRIPT], indirect=True)
async def test_session_list_read_delete(env):
    await env.runner.handle({"type": "task_start", "task_id": "t1", "prompt": "do", "continue_thread_id": None})
    await env.wait_state("completed")
    await env.runner.handle({"type": "session_list", "req_id": "r1"})
    assert [s["thread_id"] for s in env.of("reply")[-1]["data"]] == ["th1"]
    await env.runner.handle({"type": "session_read", "req_id": "r2", "thread_id": "th1"})
    assert env.of("reply")[-1]["ok"] is True and env.fake.received("thread/read")["threadId"] == "th1"
    await env.runner.handle({"type": "session_delete", "req_id": "r3", "thread_id": "th1"})
    assert env.of("reply")[-1]["ok"] is True and not env.sessions.owns("th1")


@pytest.mark.parametrize("env", [DONE_SCRIPT], indirect=True)
async def test_unacked_messages_are_resent(env):
    await env.runner.handle({"type": "task_start", "task_id": "t1", "prompt": "do", "continue_thread_id": None})
    await env.wait_state("completed")
    first = [m for m in env.sent if "seq" in m]
    await env.runner.handle({"type": "ack", "seq": first[0]["seq"]})
    env.sent.clear()
    await env.runner.resend_unacked()
    assert [m["seq"] for m in env.sent] == [m["seq"] for m in first[1:]]


@pytest.mark.parametrize("env", [QUOTA_SCRIPT], indirect=True)
async def test_lowering_threshold_takes_effect_immediately(env):
    env.guard.set_thresholds({"five_hour": 99})
    await env.runner.handle({"type": "task_start", "task_id": "t1", "prompt": "do", "continue_thread_id": None})
    await env.wait_state("running")
    await asyncio.sleep(0.2)
    assert env.fake.received("turn/steer") is None
    await env.runner.handle({"type": "settings", "thresholds": {"five_hour": 50, "weekly": 95},
                             "log_visibility": "summary"})
    steer = await env.fake.wait_for(lambda: env.fake.received("turn/steer"))
    assert steer["input"][0]["text"] == STOP_TEXT
    await env.wait_state("suspended_quota")


def test_session_registry_persists(tmp_path):
    r = SessionRegistry(tmp_path / "s.json")
    r.add("th1", "t1")
    assert SessionRegistry(tmp_path / "s.json").owns("th1")
    r.remove("th1")
    assert not SessionRegistry(tmp_path / "s.json").owns("th1")


@pytest.mark.parametrize("env", [{"errors": {"turn/steer": {"code": 1, "message": "no turn"},
                                             "turn/start": {"code": 1, "message": "busy"}}}], indirect=True)
async def test_handle_never_raises_on_app_server_errors(env):
    # Review I5: an AppServerError inside handle() used to end the control loop.
    from codex_bridge.agent.runner import Current
    env.runner.cur = Current(task_id="t1", thread_id="th1", turn_id="u1")
    await env.runner.handle({"type": "task_followup", "task_id": "t1", "text": "x"})  # steer fails, start fails
    assert env.of("task_state")[-1]["state"] == "failed"
    assert env.runner.cur is None


async def test_task_start_after_app_server_died_reports_failed(env):
    # Review I6
    await env.fake.app.stop()
    await env.runner.handle({"type": "task_start", "task_id": "t1", "prompt": "do", "continue_thread_id": None})
    await env.wait_state("failed")
    assert env.runner.cur is None


@pytest.mark.parametrize("env", [DONE_SCRIPT], indirect=True)
async def test_friend_limits_are_reported_with_usage(env):
    env.runner.apply_local_limits({"five_hour": 70, "weekly": None, "force": True})
    await env.runner.refresh_usage()
    u = env.of("usage")[-1]
    assert u["friend_limits"] == {"five_hour": 70, "weekly": None, "force": True}
    assert env.guard.effective_limits()["five_hour"] == 70 and env.guard.force is True
