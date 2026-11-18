import asyncio
import json
import os

import pytest

from codex_bridge.agent.runner import AgentRunner, Current
from codex_bridge.agent.sessions import SessionRegistry
from codex_bridge.agent.usage import QuotaGuard
from codex_bridge.agent.worklog import WorkLog
from fake_helpers import Fake
from fakes import notifications as n

DAY = 86400


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


# ---- WorkLog ----------------------------------------------------------------
def test_records_are_kept_per_task_in_order_and_survive_a_restart(tmp_path):
    clock = Clock()
    log = WorkLog(tmp_path / "tasks", clock=clock)
    log.add("t1", "plan", {"steps": [{"step": "読む", "status": "completed"}]})
    clock.now += 5
    log.add("t1", "agent_message", {"text": "読みました"})
    log.add("t2", "status", {"text": "x"})
    assert [(e["ts"], e["kind"]) for e in log.task_events("t1")] == [(1_000_000.0, "plan"), (1_000_005.0, "agent_message")]
    assert log.task_events("t1")[0]["data"]["steps"][0]["step"] == "読む"
    assert (tmp_path / "tasks" / "t1.jsonl").exists()  # logs/tasks/<task_id>.jsonl (design 4.10)
    again = WorkLog(tmp_path / "tasks")
    assert [e["kind"] for e in again.task_events("t1")] == ["plan", "agent_message"]
    assert again.task_events("nobody") == []


def test_tasks_lists_each_task_newest_first(tmp_path):
    clock = Clock()
    log = WorkLog(tmp_path, clock=clock)
    log.add("old", "started", {})
    clock.now += 100
    log.add("new", "started", {})
    clock.now += 10
    log.add("new", "status", {"text": "x"})
    assert log.tasks() == [{"task_id": "new", "started": 1_000_100.0, "updated": 1_000_110.0, "count": 2},
                           {"task_id": "old", "started": 1_000_000.0, "updated": 1_000_000.0, "count": 1}]


def touched(path, at: float) -> None:
    """The file was last written at `at` (the file system's modification time is what prune() goes by)."""
    os.utime(path, (at, at))


def test_prune_removes_what_is_older_than_the_retention_period(tmp_path):
    clock = Clock()
    log = WorkLog(tmp_path, retention_days=30, clock=clock)
    log.add("old", "started", {})
    touched(tmp_path / "old.jsonl", clock.now)
    clock.now += 20 * DAY
    log.add("recent", "started", {})
    touched(tmp_path / "recent.jsonl", clock.now)
    clock.now += 11 * DAY  # old: 31 days since its last write, recent: 11
    assert log.prune() == 1
    assert [t["task_id"] for t in log.tasks()] == ["recent"] and log.task_events("old") == []
    clock.now += 19 * DAY  # recent: exactly 30 days: kept; one second more: gone
    assert log.prune() == 0
    clock.now += 1
    assert log.prune() == 1 and log.tasks() == []


def test_a_task_still_being_written_is_not_pruned_for_its_first_record(tmp_path):
    clock = Clock()
    log = WorkLog(tmp_path, retention_days=1, clock=clock)
    log.add("long", "started", {})
    touched(tmp_path / "long.jsonl", clock.now)
    clock.now += 2 * DAY
    log.add("long", "status", {"text": "still going"})  # the age is that of the last write
    touched(tmp_path / "long.jsonl", clock.now)
    assert log.prune() == 0


def test_prune_decides_by_the_file_time_without_reading_the_files(tmp_path, monkeypatch):
    # it runs at every task start: it must not parse every record of every task
    clock = Clock()
    log = WorkLog(tmp_path, retention_days=30, clock=clock)
    log.add("old", "started", {})
    log.add("new", "started", {})
    touched(tmp_path / "old.jsonl", clock.now - 40 * DAY)
    touched(tmp_path / "new.jsonl", clock.now - 1 * DAY)
    reads = []
    monkeypatch.setattr("codex_bridge.agent.worklog._read", lambda p: reads.append(p) or [])
    assert log.prune() == 1
    assert reads == [] and not (tmp_path / "old.jsonl").exists() and (tmp_path / "new.jsonl").exists()


def test_a_file_with_invalid_bytes_still_shows_its_lines_and_is_still_pruned(tmp_path):
    clock = Clock()
    log = WorkLog(tmp_path, retention_days=30, clock=clock)
    log.add("t1", "started", {})
    with open(tmp_path / "t1.jsonl", "ab") as f:
        f.write(b'{"ts": 1.0, "kind": "status", "data": {"text": "caf\xe9"}}\n\xff\xfe not json\n')
    log.add("t1", "status", {"text": "after"})
    kinds = [e["kind"] for e in log.task_events("t1")]
    assert kinds == ["started", "status", "status"]  # the line with a bad byte keeps its other characters
    assert log.task_events("t1")[1]["data"]["text"].startswith("caf")
    assert [t["task_id"] for t in log.tasks()] == ["t1"]
    touched(tmp_path / "t1.jsonl", clock.now - 31 * DAY)
    assert log.prune() == 1 and not (tmp_path / "t1.jsonl").exists()


def test_a_cut_off_last_line_does_not_swallow_the_next_record(tmp_path):
    log = WorkLog(tmp_path)
    log.add("t1", "started", {})
    with open(tmp_path / "t1.jsonl", "ab") as f:
        f.write(b'{"ts": 5.0, "kind": "sta')  # a crash in the middle of a line: no newline
    log.add("t1", "status", {"text": "after the crash"})
    log.add("t1", "status", {"text": "and again"})
    assert [(e["kind"], e["data"].get("text")) for e in log.task_events("t1")] == [
        ("started", None), ("status", "after the crash"), ("status", "and again")]
    raw = (tmp_path / "t1.jsonl").read_bytes()
    assert raw.count(b"\n") == 4 and not raw.endswith(b"\n\n")  # exactly one line break was added


def test_clear_removes_every_record_and_nothing_else(tmp_path):
    log = WorkLog(tmp_path)
    log.add("t1", "started", {})
    log.add("t2", "started", {})
    (tmp_path / "keep.txt").write_text("x", encoding="utf-8")
    assert log.clear() == 2
    assert log.tasks() == [] and (tmp_path / "keep.txt").exists()


def test_unreadable_lines_are_skipped_and_the_rest_is_read(tmp_path):
    log = WorkLog(tmp_path)
    log.add("t1", "started", {})
    with open(tmp_path / "t1.jsonl", "a", encoding="utf-8") as f:
        f.write("{cut off\n[1]\n\n")
    log.add("t1", "status", {"text": "after"})
    assert [e["kind"] for e in log.task_events("t1")] == ["started", "status"]
    assert log.tasks()[0]["count"] == 2


@pytest.mark.parametrize("bad", ["../x", "..\\..\\x", "a/b", "a\\b", "", ".", "..", "C:\\x", "t1.jsonl\x00", "a b", "é"])
def test_a_task_id_that_is_not_a_plain_name_never_reaches_the_disk(tmp_path, bad):
    # the id comes from the host: it must not let the host write outside logs/tasks on the lender's PC
    log = WorkLog(tmp_path / "tasks")
    with pytest.raises(ValueError):
        log.add(bad, "started", {})
    with pytest.raises(ValueError):
        log.task_events(bad)
    assert [p.name for p in tmp_path.rglob("*") if p.is_file()] == []


# ---- runner -> host messages and the work log ---------------------------------
class Env:
    def __init__(self, tmp_path, script, retention_days=30):
        self.fake = Fake(tmp_path, script)
        self.sent: list[dict] = []
        self.logged: list[dict] = []
        self.clock = Clock(0.0)
        self.wall = Clock()
        self.worklog = WorkLog(tmp_path / "logs" / "tasks", retention_days=retention_days, clock=self.wall)

        async def send(m):
            self.sent.append(m)
        self.runner = AgentRunner(self.fake.app, QuotaGuard({"five_hour": 90, "weekly": 95}),
                                  SessionRegistry(tmp_path / "sessions.json"), send, usage_interval=3600,
                                  tick_interval=3600, on_log=self.logged.append, worklog=self.worklog,
                                  clock=self.clock)

    def events(self, kind=None):
        return [m for m in self.sent if m["type"] == "event" and (kind is None or m["kind"] == kind)]

    async def wait_state(self, state, timeout=5):
        for _ in range(int(timeout / 0.05)):
            if any(m["state"] == state for m in self.sent if m["type"] == "task_state"):
                return
            await asyncio.sleep(0.05)
        raise AssertionError(f"no {state}: {[m for m in self.sent if m['type'] == 'task_state']}")


@pytest.fixture
async def env(tmp_path, request):
    e = Env(tmp_path, getattr(request, "param", {}))
    await e.fake.app.start()
    await e.runner.start()
    yield e
    await e.runner.stop()
    await e.fake.app.stop()


SCRIPT = {"after": {"turn/start": [
    n.turn_started(),
    n.agent_delta("まず"), n.agent_delta("読みます"), n.agent_completed("まず読みます"),
    n.plan_updated([("ファイルを読む", "inProgress"), ("まとめる", "pending")]),
    n.reasoning_completed(["最初に", "構成を確かめる"]),
    n.mcp_started(),
    n.mcp_completed(),
    n.token_usage(17298, 34),
    n.token_usage(17400, 60, total_input=34698),
    n.agent_completed("できました", item_id="msg_2", phase="final_answer"),
    n.turn_completed()]}}


@pytest.mark.parametrize("env", [SCRIPT], indirect=True)
async def test_codex_progress_is_sent_to_the_host_and_recorded_per_task(env):
    await env.runner.handle({"type": "task_start", "task_id": "t1", "prompt": "do", "continue_thread_id": None})
    await env.wait_state("completed")
    by_kind = {}
    for m in env.events():
        by_kind.setdefault(m["kind"], []).append(m["data"])
    assert by_kind["plan"] == [{"steps": [{"step": "ファイルを読む", "status": "inProgress"},
                                          {"step": "まとめる", "status": "pending"}]}]
    assert by_kind["reasoning_summary"] == [{"text": "最初に\n構成を確かめる"}]
    assert by_kind["tool_started"] == [{"tool": "echo", "arguments": {"text": "hi"}}]
    assert by_kind["tool_finished"] == [{"tool": "echo", "arguments": {"text": "hi"}, "duration_ms": 3,
                                         "success": True, "result_summary": "echo: hi"}]
    assert by_kind["token_usage"] == [{"input": 34698, "output": 94}]  # the whole turn: both model calls
    assert [d["text"] for d in by_kind["agent_message"]] == ["まず読みます", "できました"]
    assert by_kind["turn_completed"] == [{"status": "completed"}]
    assert "tool_call" not in by_kind  # replaced by tool_finished (design 6)
    assert [m["kind"] for m in env.events()].index("token_usage") < [m["kind"] for m in env.events()].index("turn_completed")
    assert all(m["task_id"] == "t1" and "seq" in m for m in env.events() if m["kind"] != "progress")
    rec = env.worklog.task_events("t1")
    kinds = [e["kind"] for e in rec]
    assert kinds[0] == "task_prompt" and rec[0]["data"] == {"text": "do"}
    assert {"state", "started", "plan", "reasoning_summary", "tool_started", "tool_finished", "token_usage",
            "agent_message", "turn_completed"} <= set(kinds)
    final = [e for e in rec if e["kind"] == "state"][-1]["data"]
    assert final["state"] == "completed" and final["report"] == "できました"  # design 4.9: the report is kept
    assert env.worklog.tasks()[0]["task_id"] == "t1"


@pytest.mark.parametrize("env", [SCRIPT], indirect=True)
async def test_progress_is_sent_without_a_seq_and_never_recorded(env):
    await env.runner.handle({"type": "task_start", "task_id": "t1", "prompt": "do", "continue_thread_id": None})
    await env.wait_state("completed")
    progress = env.events("progress")
    assert len(progress) == 1  # the clock never moved: the first delta goes out, the second is held back
    assert progress[0]["data"] == {"text": "まず"} and "seq" not in progress[0] and progress[0]["task_id"] == "t1"
    assert all(m.get("kind") != "progress" for m in env.runner._unacked.values())  # not re-sent after a reconnect
    assert all(m.get("kind") != "progress" for m in env.logged)
    assert "progress" not in [e["kind"] for e in env.worklog.task_events("t1")]


async def _start(env, task_id="t1"):
    env.runner.cur = Current(task_id=task_id, thread_id="th1", turn_id="u1")


@pytest.mark.parametrize("env", [{}], indirect=True)
async def test_progress_is_sent_at_most_once_every_two_seconds(env):
    await _start(env)
    texts = []

    async def delta(text, at):
        env.clock.now = at
        before = len(env.events("progress"))
        await env.runner._on_event(n.agent_delta(text))
        got = env.events("progress")[before:]
        assert len(got) <= 1
        texts.extend(m["data"]["text"] for m in got)

    await delta("a", 0.0)      # first: goes out at once
    await delta("b", 0.5)      # held back
    await delta("c", 1.99)     # held back
    await delta("d", 2.0)      # two seconds since the last one: the whole message so far
    await delta("e", 3.9)
    await delta("f", 4.0)
    assert texts == ["a", "abcd", "abcdef"]


@pytest.mark.parametrize("env", [{}], indirect=True)
async def test_a_new_message_starts_a_new_progress_text(env):
    await _start(env)
    await env.runner._on_event(n.agent_delta("one", item_id="m1"))
    await env.runner._on_event(n.agent_completed("one", item_id="m1"))
    env.clock.now = 5.0
    await env.runner._on_event(n.agent_delta("two", item_id="m2"))
    assert [m["data"]["text"] for m in env.events("progress")] == ["one", "two"]
    assert env.runner.cur.progress == "two"


@pytest.mark.parametrize("env", [{}], indirect=True)
async def test_an_empty_reasoning_summary_sends_nothing(env):
    # the real server sends reasoning items with summary [] (seen in the capture): nothing to show
    await _start(env)
    await env.runner._on_event(n.reasoning_completed([]))
    await env.runner._on_event(n.reasoning_completed(["", ""]))
    assert env.events("reasoning_summary") == []


@pytest.mark.parametrize("env", [{}], indirect=True)
async def test_tokens_are_counted_per_turn_and_start_again_each_turn(env):
    await _start(env)
    await env.runner._on_event(n.turn_started())
    await env.runner._on_event(n.token_usage(100, 10))
    await env.runner._on_event(n.token_usage(150, 20, total_input=250))
    assert env.events("token_usage") == []  # one per turn, when the turn ends
    await env.runner._on_event(n.turn_completed())
    assert [m["data"] for m in env.events("token_usage")] == [{"input": 250, "output": 30}]
    env.runner.cur = Current(task_id="t1", thread_id="th1")
    await env.runner._on_event(n.turn_started())
    await env.runner._on_event(n.token_usage(7, 1))
    await env.runner._on_event(n.turn_completed())
    assert [m["data"] for m in env.events("token_usage")] == [{"input": 250, "output": 30}, {"input": 7, "output": 1}]


@pytest.mark.parametrize("env", [{}], indirect=True)
async def test_a_turn_without_token_numbers_sends_no_token_event(env):
    await _start(env)
    await env.runner._on_event(n.turn_started())
    await env.runner._on_event(n.turn_completed())
    assert env.events("token_usage") == []


@pytest.mark.parametrize("env", [{}], indirect=True)
async def test_a_failed_tool_call_says_why_and_big_arguments_are_cut(env):
    await _start(env)
    big = {"path": "a.txt", "content": "x" * 50_000}
    await env.runner._on_event(n.mcp_started(arguments=big))
    await env.runner._on_event(n.mcp_failed("ファイルがありません", arguments=big))
    started, finished = env.events("tool_started")[0]["data"], env.events("tool_finished")[0]["data"]
    for d in (started, finished):
        assert isinstance(d["arguments"], str) and d["arguments"].startswith('{"path": "a.txt"') and len(d["arguments"]) < 1100
    assert (finished["success"], finished["duration_ms"], finished["result_summary"]) == (False, 5, "ファイルがありません")
    json.dumps(env.sent)


@pytest.mark.parametrize("env", [{}], indirect=True)
async def test_other_tool_kinds_are_reported_the_same_way(env):
    await _start(env)
    item = {"type": "commandExecution", "id": "c1", "command": "dir", "status": "completed", "durationMs": 40,
            "aggregatedOutput": "a.txt\n", "exitCode": 0}
    await env.runner._on_event({"method": "item/started", "params": {"threadId": "th1", "item": {**item, "status": "inProgress"}}})
    await env.runner._on_event({"method": "item/completed", "params": {"threadId": "th1", "item": item}})
    assert env.events("tool_started")[0]["data"]["tool"] == "dir"
    d = env.events("tool_finished")[0]["data"]
    assert (d["tool"], d["success"], d["duration_ms"]) == ("dir", True, 40)


@pytest.mark.parametrize("env", [{}], indirect=True)
async def test_a_disk_error_in_the_work_log_never_stops_the_events(env, monkeypatch):
    await _start(env)

    def broken(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(env.worklog, "add", broken)
    await env.runner._on_event(n.agent_completed("hello"))
    assert [m["data"]["text"] for m in env.events("agent_message")] == ["hello"]
    assert any(e.get("type") == "error" and "disk full" in e["text"] for e in env.logged)


@pytest.mark.parametrize("env", [{}], indirect=True)
async def test_followups_are_recorded(env):
    await _start(env)
    await env.runner.handle({"type": "task_followup", "task_id": "t1", "text": "もう一つ"})
    assert env.worklog.task_events("t1")[-1] == {"ts": 1_000_000.0, "kind": "task_prompt", "data": {"text": "もう一つ"}}


async def test_the_runner_prunes_old_records_when_it_starts(tmp_path):
    e = Env(tmp_path, {}, retention_days=30)
    e.worklog.add("ancient", "started", {})
    touched(tmp_path / "logs" / "tasks" / "ancient.jsonl", e.wall.now)
    e.wall.now += 31 * DAY
    await e.fake.app.start()
    await e.runner.start()
    try:
        assert e.worklog.tasks() == []
    finally:
        await e.runner.stop()
        await e.fake.app.stop()
