import asyncio
import json
import time

import pytest

from codex_bridge.host import lenders
from codex_bridge.host.lenders import (Lender, normalize_capabilities, normalize_limits, normalize_window,
                                       pick_lender)
from codex_bridge.host.paths import ToolError
from codex_bridge.host.tasks import TaskManager

TH = {"five_hour": 80, "weekly": 80}


async def test_busy_elsewhere_lender_is_skipped_by_auto_routing(mgr):
    alice, bob = await FakeAgent.join(mgr, "alice"), await FakeAgent.join(mgr, "bob")
    await alice.push("usage", **usage(five_hour=0))
    await bob.push("usage", **usage(five_hour=70))
    await alice.push("lender_status", busy_elsewhere=True)
    assert mgr.lenders()[0]["busy_elsewhere"] is True
    assert mgr.tasks[await mgr.start("one")].lender == "bob"
    await alice.push("lender_status", busy_elsewhere=False)
    assert mgr.tasks[await mgr.start("two")].lender == "alice"


async def test_named_busy_elsewhere_lender_gets_busy_answer(mgr):
    alice = await FakeAgent.join(mgr, "alice")
    await alice.push("lender_status", busy_elsewhere=True)
    with pytest.raises(ToolError, match="上限"):
        await mgr.start("one", lender="alice")
    assert not alice.all("task_start")
    await alice.push("lender_status", busy_elsewhere="false")
    assert mgr.lenders()[0]["busy_elsewhere"] is True  # malformed status cannot clear it
    mgr.detach(alice.link)
    await FakeAgent.join(mgr, "alice")  # a v0.1 replacement starts with the old behaviour
    assert mgr.lenders()[0]["busy_elsewhere"] is False
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
    busy = lender("busy", five_hour=0, current_task_id="t1", active_task_ids=["t1"])
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


# ---- what a lender sends is checked on receipt ---------------------------
NO_LIMITS = {"five_hour": None, "weekly": None, "force": False}


@pytest.mark.parametrize("raw, expected", [
    ({"five_hour": 50, "weekly": 60.5, "force": True}, {"five_hour": 50, "weekly": 60.5, "force": True}),
    ({"five_hour": 100, "weekly": None, "force": False}, {"five_hour": 100, "weekly": None, "force": False}),
    ({"five_hour": 70}, {"five_hour": 70, "weekly": None, "force": False}),
    ({"five_hour": "50", "weekly": -5, "force": "yes"}, NO_LIMITS),   # text, negative, not a real flag
    ({"five_hour": True, "weekly": 0, "force": 1}, NO_LIMITS),        # a bool is not a percent; 0 is out of range
    ({"five_hour": 100.5, "weekly": float("nan")}, NO_LIMITS),
    ({"five_hour": float("inf"), "weekly": [80]}, NO_LIMITS),
    ({"five_hour": 10 ** 400, "weekly": -(10 ** 400)}, NO_LIMITS),
    ({"five_hour": {"v": 80}, "weekly": "x"}, NO_LIMITS),
    ({}, NO_LIMITS),
])
def test_normalize_limits_keeps_only_percents(raw, expected):
    assert normalize_limits(raw) == expected


@pytest.mark.parametrize("raw", [None, "80", 80, True, [80, 80]])
def test_normalize_limits_of_something_else_is_none(raw):
    assert normalize_limits(raw) is None


@pytest.mark.parametrize("raw, expected", [
    ({"used_percent": 12.5, "resets_at": 1_800_000_000}, {"used_percent": 12.5, "resets_at": 1_800_000_000}),
    ({"used_percent": 0, "resets_at": None}, {"used_percent": 0, "resets_at": None}),
    ({"used_percent": 100, "resets_at": 1}, {"used_percent": 100, "resets_at": 1}),
    ({"used_percent": 30, "resets_at": "soon"}, {"used_percent": 30, "resets_at": None}),
    ({"used_percent": 99, "resets_at": 1e300}, {"used_percent": 99, "resets_at": None}),  # no such moment
    ({"used_percent": 99, "resets_at": True}, {"used_percent": 99, "resets_at": None}),
    ({"used_percent": 99, "resets_at": float("inf")}, {"used_percent": 99, "resets_at": None}),
    # only the two known keys are kept: whatever else was sent never reaches a page or a tool answer
    ({"used_percent": 30, "resets_at": 1, "window_minutes": 300, "note": float("nan"), "more": {"x": float("inf")}},
     {"used_percent": 30, "resets_at": 1}),
    ({"used_percent": "30", "resets_at": 1}, None),
    ({"used_percent": True, "resets_at": 1}, None),
    ({"used_percent": 10 ** 400, "resets_at": 1}, None),
    ({"used_percent": float("nan"), "resets_at": 1}, None),
    ({"used_percent": -1, "resets_at": 1}, None),      # a percent is 0..100: anything else is not usable
    ({"used_percent": -0.5, "resets_at": 1}, None),
    ({"used_percent": 100.5, "resets_at": 1}, None),
    ({"used_percent": 1000, "resets_at": 1}, None),
    ({"resets_at": 1}, None),
    (None, None), ("12", None), (12, None), ([12], None),
])
def test_normalize_window(raw, expected):
    assert normalize_window(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ({"resumable": False, "pre_approval": True}, {"resumable": False, "pre_approval": True, "model_select": False, "max_parallel": 1}),
    ({"resumable": False}, {"resumable": False, "pre_approval": False, "model_select": False, "max_parallel": 1}),
    ({"resumable": "no", "pre_approval": 1, "extra": float("nan")},
     {"resumable": True, "pre_approval": False, "model_select": False, "max_parallel": 1}),
    ({}, {"resumable": True, "pre_approval": False, "model_select": False, "max_parallel": 1}),
    (None, {"resumable": True, "pre_approval": False, "model_select": False, "max_parallel": 1}),   # a v0.1 agent sends none
    ("all", {"resumable": True, "pre_approval": False, "model_select": False, "max_parallel": 1}),
])
def test_normalize_capabilities_keeps_the_known_flags(raw, expected):
    assert normalize_capabilities(raw) == {**expected, "max_parallel": 1}


def test_capabilities_keep_model_select():
    assert normalize_capabilities({"model_select": True})["model_select"] is True
    assert normalize_capabilities(None)["model_select"] is False
    for value in (False, 1, "true", None):
        assert normalize_capabilities({"model_select": value})["model_select"] is False


def test_lender_model_data_starts_unknown():
    lender = Lender(name="a")
    assert lender.models is None and lender.max_effort is None
    assert lender.capabilities["model_select"] is False


def test_normalize_models_keeps_valid_entries():
    raw = [{"model": "m1", "name": "M1", "efforts": ["low", "low", 3, "high"],
            "default_effort": "low", "is_default": True}, {"model": ""}, "x"]
    assert lenders.normalize_models(raw) == [
        {"model": "m1", "name": "M1", "efforts": ["low", "high"], "default_effort": "low", "is_default": True}]


def test_normalize_models_not_a_list_is_none():
    assert lenders.normalize_models({"a": 1}) is None
    for raw in (None, "m1", 1, True, ()):
        assert lenders.normalize_models(raw) is None


def test_normalize_models_fills_defaults_and_checks_field_types():
    raw = [{"model": "m1"}, {"model": "m2", "name": 7, "efforts": "low", "default_effort": 7, "is_default": 1},
           {"model": "m3", "name": "", "efforts": ["future", None, "", "future", False], "default_effort": "future"},
           {}, {"model": None}, {"model": 3}, {"model": False}, None]
    assert lenders.normalize_models(raw) == [
        {"model": "m1", "name": "m1", "efforts": [], "default_effort": None, "is_default": False},
        {"model": "m2", "name": "m2", "efforts": [], "default_effort": None, "is_default": False},
        {"model": "m3", "name": "m3", "efforts": ["future", ""], "default_effort": "future", "is_default": False},
    ]
    assert lenders.normalize_models([]) == []


def test_normalize_models_stops_after_200_items():
    models = lenders.normalize_models([{"model": f"m{i}"} for i in range(201)])
    assert [entry["model"] for entry in models] == [f"m{i}" for i in range(200)]


def test_normalize_effort_keeps_only_known_values():
    for effort in ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"):
        assert lenders.normalize_effort(effort) == effort
    for value in (None, "", "HIGH", "future", 1, True, [], {}):
        assert lenders.normalize_effort(value) is None


def test_describe_models():
    assert lenders.describe_models(None) == "なし"
    assert lenders.describe_models([]) == "なし"
    assert lenders.describe_models([{"model": "gpt-a", "efforts": ["low", "medium"]},
                                   {"model": "gpt-b", "efforts": ["high"]}]) == "gpt-a（low・medium）、gpt-b（high）"


def test_accepts_nothing_specified_even_v02():
    assert lenders.lender_accepts(Lender(name="a"), None, None) is None


@pytest.mark.parametrize("model, effort", [("m1", None), (None, "low"), ("m1", "low")])
def test_accepts_v02_refuses_any_choice(model, effort):
    reason = lenders.lender_accepts(Lender(name="a"), model, effort)
    assert "v0.2 以前" in reason
    assert reason == "「a」の exe は v0.2 以前のため、モデルと推論の強さを指定できません。"


def test_accepts_no_list_yet():
    lender = Lender(name="a", capabilities={"model_select": True}, models=None)
    reason = lenders.lender_accepts(lender, "m1", None)
    assert "まだ届いていません" in reason
    assert reason == ("「a」のモデルの一覧がまだ届いていません（その人の Codex が起動すると届きます）。"
                      "少し待つか、モデルと推論の強さを指定せずに依頼してください。")


def test_accepts_unknown_model_lists_offer():
    lender = Lender(name="a", capabilities={"model_select": True},
                    models=[{"model": "m1", "efforts": ["low", "high"]}])
    reason = lenders.lender_accepts(lender, "m2", None)
    assert "m1（low・high）" in reason
    assert reason == ("「a」は、指定のモデルと推論の強さを使わせていません。"
                      "この人が使えるモデルと推論の強さ: m1（low・high）")
    assert lenders.lender_accepts(lender, "m1", None) is None
    assert lenders.lender_accepts(lender, "m1", "high") is None
    assert lenders.lender_accepts(lender, "m1", "medium") == reason


def test_accepts_effort_only_uses_default_model():
    lender = Lender(name="a", capabilities={"model_select": True},
                    models=[{"model": "m1", "efforts": ["low"]},
                            {"model": "m2", "efforts": ["high"], "is_default": True}])
    assert lenders.lender_accepts(lender, None, "high") is None
    assert lenders.lender_accepts(lender, None, "low") is not None


def test_accepts_effort_only_falls_back_to_first_model():
    lender = Lender(name="a", capabilities={"model_select": True},
                    models=[{"model": "m1", "efforts": ["low"]}, {"model": "m2", "efforts": ["high"]}])
    assert lenders.lender_accepts(lender, None, "low") is None
    assert lenders.lender_accepts(lender, None, "high") is not None


def test_accepts_empty_offer_refuses_a_choice():
    lender = Lender(name="a", capabilities={"model_select": True}, models=[])
    for model, effort in (("m1", None), (None, "low")):
        assert lenders.lender_accepts(lender, model, effort) == (
            "「a」は、指定のモデルと推論の強さを使わせていません。この人が使えるモデルと推論の強さ: なし")
    assert lenders.lender_accepts(lender, None, None) is None


# ---- TaskManager with several lenders -----------------------------------
HELLO ={"agent_version": "0.1.0", "codex_version": "x", "auth_mode": "chatgpt"}


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
    return TaskManager(tmp_path / "data", touched_fn=lambda tid: [])


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
    with pytest.raises(ToolError, match="上限"):
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
    with pytest.raises(ToolError, match="上限"):
        await mgr.start("two", lender="bob")
    with pytest.raises(ToolError, match="接続されていません"):
        await mgr.start("two", lender="carol")
    mgr.detach(alice.link)
    with pytest.raises(ToolError, match="接続されていません"):
        await mgr.start("two", lender="alice")


async def test_explicit_lender_over_its_limit_is_refused(mgr):
    alice = await FakeAgent.join(mgr, "alice", limits=TH)
    bob = await FakeAgent.join(mgr, "bob", limits=TH)
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

    with pytest.raises(ToolError, match="会話は作業中"):  # The same conversation is still active.
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
    assert info["capabilities"] == {"resumable": True, "pre_approval": False, "model_select": False, "max_parallel": 1}
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
    assert info["protocol"] == 2 and info["capabilities"] == {"resumable": True, "pre_approval": True, "model_select": False, "max_parallel": 1}
    assert info["limits"] == {"five_hour": 50, "weekly": 60, "force": False}
    await alice.push("usage", **usage(five_hour=55))  # over alice's own limit, under the host's
    with pytest.raises(ToolError, match="再開可能"):
        await mgr.start("one")


async def test_bad_numbers_from_one_lender_do_not_break_the_host(mgr):
    # Whatever one agent sends, the lists, the usage and the routing for everyone keep working.
    alice = FakeAgent(mgr, "alice")
    await alice.push("hello", **HELLO, limits={"five_hour": "50", "weekly": -1, "force": "no"}, current_task_id=None)
    await alice.push("usage", **usage(five_hour=10, weekly=10))
    assert mgr.lenders()[0]["limits"] == NO_LIMITS
    assert mgr.usage()["lenders"]["alice"]["blocked_until"] is None

    await alice.push("usage", **usage(five_hour=10, weekly=10), friend_limits={"five_hour": True, "weekly": "x"})
    assert mgr.usage()["friend_limits"] == NO_LIMITS and mgr.lenders()[0]["limits"] == NO_LIMITS

    await alice.push("usage", five_hour={"used_percent": "99", "resets_at": "x"}, weekly="full", ts=0)
    u = mgr.usage()["lenders"]["alice"]
    assert u["five_hour"] is None and u["weekly"] is None and u["blocked_until"] is None
    await alice.push("usage", five_hour={"used_percent": 99, "resets_at": 1e300}, weekly=None, ts=0)
    u = mgr.usage()["lenders"]["alice"]
    assert u["five_hour"]["resets_at_local"] is None and u["blocked_until"] is None
    assert mgr.tasks[await mgr.start("one")].lender == "alice"


NAN, INF = float("nan"), float("inf")


async def test_nothing_a_lender_sends_keeps_the_lists_from_being_json(mgr):
    # NaN and Infinity are not JSON: one of them in a list would turn the whole admin page into an error.
    alice = FakeAgent(mgr, "alice")
    await alice.push("hello", agent_version=NAN, codex_version=INF, auth_mode={"x": NAN}, protocol=NAN,
                     capabilities={"resumable": NAN, "pre_approval": INF, "extra": NAN},
                     limits={"five_hour": NAN, "weekly": INF, "force": NAN}, current_task_id=None)
    await alice.push("usage", five_hour={"used_percent": 10, "resets_at": NAN, "extra": NAN},
                     weekly={"used_percent": 20, "resets_at": int(time.time()) + 60, "more": [INF]},
                     ts=NAN, friend_limits={"five_hour": INF, "weekly": NAN, "force": NAN}, extra=NAN)
    row = json.loads(json.dumps(mgr.lenders(), allow_nan=False))[0]
    assert (row["agent_version"], row["codex_version"], row["auth_mode"], row["protocol"]) == (None, None, None, 1)
    assert row["capabilities"] == {"resumable": True, "pre_approval": False, "model_select": False, "max_parallel": 1}
    assert row["limits"] == NO_LIMITS
    assert row["usage"]["five_hour"] == {"used_percent": 10, "resets_at": None, "resets_at_local": None}
    assert set(row["usage"]["weekly"]) == {"used_percent", "resets_at", "resets_at_local"}
    assert row["usage"]["ts"] is None
    json.dumps(mgr.usage(), allow_nan=False)
    json.dumps(mgr.agent_info(), allow_nan=False)
    await alice.push("usage", five_hour=None, weekly=None, ts=INF)
    assert mgr.usage()["ts"] is None
    await alice.push("usage", five_hour=None, weekly=None, ts=1_800_000_000)
    assert mgr.usage()["ts"] == 1_800_000_000


async def test_a_bogus_percent_does_not_put_a_lender_first(mgr):
    alice = await FakeAgent.join(mgr, "alice", limits=TH)
    bob = await FakeAgent.join(mgr, "bob", limits=TH)
    await alice.push("usage", five_hour={"used_percent": -500, "resets_at": int(time.time()) + 60}, weekly=None, ts=0)
    await bob.push("usage", **usage(five_hour=70))
    assert mgr.usage()["lenders"]["alice"]["five_hour"] is None
    assert mgr.tasks[await mgr.start("one")].lender == "bob"  # unknown usage goes last (design 3.3)


async def test_invited_names_are_listed_even_before_they_connect(mgr):
    alice = await FakeAgent.join(mgr, "alice")
    await alice.push("usage", **usage(five_hour=10))
    rows = mgr.lenders(["carol", "alice", "dave"])
    assert [r["name"] for r in rows] == ["alice", "carol", "dave"]  # seen first, then the invited, each once
    assert set(rows[1]) == set(rows[0])  # the same columns for everyone
    carol = rows[1]
    assert carol["connected"] is False and carol["ready"] is False and carol["since"] is None
    assert carol["capabilities"] is None and carol["limits"] is None and carol["current_task_id"] is None
    assert carol["usage"]["five_hour"] is None and carol["usage"]["weekly"] is None
    assert carol["usage"]["blocked_until"] is None and "thresholds" not in carol["usage"]
    assert [r["name"] for r in mgr.lenders()] == ["alice"]  # without the invite list: as before
    with pytest.raises(ToolError, match="接続されていません"):  # being listed does not make one reachable
        await mgr.start("one", lender="carol")
    assert mgr.usage()["lenders"].keys() == {"alice"}


async def test_limits_that_are_not_an_object_leave_the_known_ones_in_place(mgr):
    alice = await FakeAgent.join(mgr, "alice", limits={"five_hour": 50, "weekly": 60, "force": True})
    await alice.push("usage", **usage(five_hour=55), friend_limits="none")
    assert mgr.lenders()[0]["limits"] == {"five_hour": 50, "weekly": 60, "force": True}
    with pytest.raises(ToolError, match="再開可能"):  # alice's own 50% still counts
        await mgr.start("one")


async def test_usage_and_lenders_are_per_lender(mgr):
    assert mgr.lenders() == [] and mgr.usage()["lenders"] == {} and mgr.agent_info() == {"connected": False}
    alice = await FakeAgent.join(mgr, "alice", limits=TH)
    bob = await FakeAgent.join(mgr, "bob", limits=TH)
    await alice.push("usage", **usage(five_hour=10))
    await bob.push("usage", **usage(five_hour=95))
    u = mgr.usage()
    assert u["lenders"]["alice"]["five_hour"]["used_percent"] == 10 and u["lenders"]["alice"]["blocked_until"] is None
    assert u["lenders"]["bob"]["five_hour"]["used_percent"] == 95 and u["lenders"]["bob"]["blocked_until"]
    assert u["five_hour"]["used_percent"] == 95  # top level: the latest connection
    tid = await mgr.start("one")
    rows = {r["name"]: r for r in mgr.lenders()}
    assert rows["alice"]["connected"] and rows["alice"]["current_task_id"] == tid
    assert rows["bob"]["current_task_id"] is None and rows["bob"]["usage"]["five_hour"]["used_percent"] == 95
    mgr.detach(bob.link)
    assert mgr.agent_info()["name"] == "alice" and mgr.agent_info()["connected"] is True
    assert {r["name"]: r["connected"] for r in mgr.lenders()} == {"alice": True, "bob": False}


async def test_status_shows_the_agent_of_the_tasks_own_lender_and_no_other(tmp_path, mgr):
    alice = await FakeAgent.join(mgr, "alice", auth_mode="chatgpt")
    t1 = await mgr.start("one")
    await alice.finish(t1, seq=1)
    await FakeAgent.join(mgr, "bob", auth_mode="apikey")  # the latest connection is now bob's
    agent = mgr.status(t1)["agent"]
    assert (agent["name"], agent["auth_mode"], agent["connected"]) == ("alice", "chatgpt", True)
    assert mgr.status()["agent"]["name"] == "alice"  # no task id: the latest task, which is alice's

    # After a host restart only bob has connected: alice's task must not be shown with bob's agent.
    (tmp_path / "data" / "tasks" / "old.json").write_text(
        '{"task_id": "old", "prompt": "p", "created": 1.0, "state": "completed", "thread_id": "th0"}', encoding="utf-8")
    mgr2 = TaskManager(tmp_path / "data", touched_fn=lambda t: [])
    await FakeAgent.join(mgr2, "bob", auth_mode="apikey")
    assert mgr2.status(t1)["agent"] == {"connected": False, "name": "alice"}
    assert mgr2.agent_info("alice") == {"connected": False, "name": "alice"}
    # A v0.1 record names no lender, so nobody's agent is shown as its own.
    assert mgr2.status("old")["lender"] is None and mgr2.status("old")["agent"] is None


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
    with pytest.raises(ToolError, match="上限"):  # bob works; alice still cannot be chosen
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


async def test_named_lender_at_its_own_capacity_reports_the_capacity(mgr):
    alice = await FakeAgent.join(mgr, "alice")
    await alice.push("hello", protocol=5, capabilities={"max_parallel": 2})
    ids = [await mgr.start("first", lender="alice"), await mgr.start("second", lender="alice")]
    await alice.push("lender_status", busy_elsewhere=True)
    with pytest.raises(ToolError) as error:
        await mgr.start("third", lender="alice")
    assert str(error.value) == "同時に受ける依頼の上限（2 件）に達しています"
    assert "ほかの依頼主" not in str(error.value) and mgr.active_task_ids("alice") == ids
