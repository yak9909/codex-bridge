"""The lender's settings (design 4.3 - 4.8, 6): limits, feature restrictions, disposable sessions, where the data
and the work folder are, and what hello says about them."""

import asyncio
import copy
import json
import math
import os
import re
import shutil
from pathlib import Path

import pytest

from codex_bridge.agent import settings as st
from codex_bridge.agent import setup
from codex_bridge.agent.appserver import launch_args
from codex_bridge.agent.control_client import ControlClient
from codex_bridge.agent.runner import PREAMBLE, AgentRunner, Current
from codex_bridge.agent.sessions import SessionRegistry
from codex_bridge.agent.usage import QuotaGuard
from codex_bridge.agent.worklog import WorkLog
from codex_bridge.common import protocol
from codex_bridge.common.invite import Invite
from codex_bridge.host import lenders
from fake_helpers import Fake
from fakes import notifications as n

DEFAULTS = {
    "limits": {"five_hour": 80, "weekly": 80, "force": False},
    "features": {"web_search": False, "image_generation": False, "multi_agent": False, "goals": False},
    "pre_approval": {"enabled": False, "timeout_min": 30},
    "disposable": False,
    "workdir": None,
    "log_retention_days": 30,
}


# ---- the store ------------------------------------------------------------------
def test_defaults_are_those_of_the_design(tmp_path):
    store = st.SettingsStore(tmp_path / "settings.json")
    assert store.get() == DEFAULTS
    assert not (tmp_path / "settings.json").exists()  # reading writes nothing


def test_update_merges_a_partial_patch_saves_it_and_returns_everything(tmp_path):
    store = st.SettingsStore(tmp_path / "sub" / "settings.json")
    out = store.update({"limits": {"five_hour": 70}, "features": {"web_search": True}, "disposable": True})
    expected = copy.deepcopy(DEFAULTS)
    expected["limits"]["five_hour"] = 70
    expected["features"]["web_search"] = True
    expected["disposable"] = True
    assert out == expected
    assert st.SettingsStore(tmp_path / "sub" / "settings.json").get() == expected  # it is on the disk
    out2 = store.update({"limits": {"weekly": None}, "workdir": "D:\\work", "log_retention_days": 7,
                         "pre_approval": {"enabled": True}})
    assert out2["limits"] == {"five_hour": 70, "weekly": None, "force": False}  # None: no limit of its own
    assert out2["workdir"] == "D:\\work" and out2["log_retention_days"] == 7
    assert out2["pre_approval"] == {"enabled": True, "timeout_min": 30} and out2["disposable"] is True
    assert store.update({"workdir": None})["workdir"] is None
    assert store.update({"limits": {"five_hour": 70.0}})["limits"]["five_hour"] == 70  # a whole number stays one


def test_get_returns_a_copy_nobody_can_change_the_store_through(tmp_path):
    store = st.SettingsStore(tmp_path / "settings.json")
    store.get()["limits"]["five_hour"] = 1
    store.get()["features"]["goals"] = True
    assert store.get() == DEFAULTS


BAD = [
    {"limits": {"five_hour": 0}}, {"limits": {"five_hour": 101}}, {"limits": {"weekly": -5}},
    {"limits": {"weekly": "80"}}, {"limits": {"five_hour": True}}, {"limits": {"weekly": math.nan}},
    {"limits": {"weekly": math.inf}}, {"limits": {"force": 1}}, {"limits": {"hourly": 50}}, {"limits": 80},
    {"features": {"web_search": "yes"}}, {"features": {"telepathy": True}}, {"features": None},
    {"disposable": "no"}, {"disposable": 1},
    {"pre_approval": {"enabled": 1}}, {"pre_approval": {"timeout_min": 0}}, {"pre_approval": {"timeout_min": 1.5}},
    {"pre_approval": {"timeout_min": True}}, {"pre_approval": {"timeout_min": 100000}},
    {"workdir": "relative\\dir"}, {"workdir": 5}, {"workdir": "   "},
    {"log_retention_days": 0}, {"log_retention_days": 1.5}, {"log_retention_days": False},
    {"log_retention_days": 100000}, {"unknown": 1}, "not a dict",
]


@pytest.mark.parametrize("bad", BAD, ids=[json.dumps(b, default=str)[:60] for b in BAD])
def test_a_bad_value_is_refused_and_nothing_at_all_is_changed(tmp_path, bad):
    store = st.SettingsStore(tmp_path / "settings.json")
    store.update({"disposable": True})
    before = (tmp_path / "settings.json").read_bytes()
    with pytest.raises(ValueError):
        store.update({"log_retention_days": 7, **bad} if isinstance(bad, dict) else bad)  # the good part too
    assert (tmp_path / "settings.json").read_bytes() == before
    assert store.get()["log_retention_days"] == 30 and store.get()["disposable"] is True


def test_the_limits_are_a_percent_from_1_to_100(tmp_path):
    store = st.SettingsStore(tmp_path / "settings.json")
    for v in (1, 50, 99.5, 100):
        assert store.update({"limits": {"five_hour": v, "weekly": v}})["limits"]["five_hour"] == v


def test_a_missing_or_broken_file_reads_as_the_defaults_and_a_bad_field_alone_falls_back(tmp_path):
    p = tmp_path / "settings.json"
    store = st.SettingsStore(p)
    p.write_bytes(b"\xff\xfe not json")
    assert store.get() == DEFAULTS
    p.write_text("[1, 2]", encoding="utf-8")
    assert store.get() == DEFAULTS
    p.write_text(json.dumps({"limits": {"five_hour": 500, "weekly": 60}, "disposable": True,
                             "features": {"goals": "x", "web_search": True}, "surprise": 1,
                             "log_retention_days": -3}), encoding="utf-8")
    got = store.get()
    assert got["limits"] == {"five_hour": 80, "weekly": 60, "force": False}
    assert got["disposable"] is True and got["features"] == {**DEFAULTS["features"], "web_search": True}
    assert got["log_retention_days"] == 30 and "surprise" not in got


def test_the_v01_limits_are_carried_over_until_the_first_save(tmp_path):
    (tmp_path / "limits.json").write_text(json.dumps({"five_hour": 70, "weekly": None, "force": True}),
                                          encoding="utf-8")
    store = st.SettingsStore(tmp_path / "settings.json", legacy_limits=tmp_path / "limits.json")
    assert store.get()["limits"] == {"five_hour": 70, "weekly": None, "force": True}
    store.update({"disposable": True})
    (tmp_path / "limits.json").unlink()
    assert store.get()["limits"] == {"five_hour": 70, "weekly": None, "force": True}  # now it is in settings.json
    other = st.SettingsStore(tmp_path / "none" / "settings.json", legacy_limits=tmp_path / "none" / "limits.json")
    assert other.get() == DEFAULTS


# ---- limits and features in the app-server's arguments -----------------------------------
def test_every_feature_is_stopped_by_default_with_the_names_verified_on_codex_0_167():
    argv = launch_args("http://h:1")
    assert argv[argv.index('web_search="disabled"') - 1] == "-c"
    for f in ("image_generation", "goals"):
        assert argv[argv.index(f) - 1] == "--disable"
    assert "agents.max_depth=0" in argv  # sub-agents: best effort (design 4.4; Codex 0.167 cannot switch them off)
    assert launch_args("http://h:1", features=DEFAULTS["features"]) == argv  # None means the defaults
    assert "multi_agent" not in " ".join(argv)  # `--disable multi_agent` does nothing in 0.167: not used


def test_an_allowed_feature_adds_no_argument_and_the_bridge_tools_are_always_there():
    allowed = {k: True for k in st.FEATURES}
    argv = launch_args("http://h:1", ["x"], allowed)
    assert not any(k in a for a in argv for k in ("web_search", "image_generation", "goals", "agents."))
    assert 'mcp_servers.bridge.url="http://h:1/codex/mcp"' in argv and "mcp_servers.x.enabled=false" in argv
    one = launch_args("http://h:1", features={**allowed, "goals": False})
    assert one.count("goals") == 1 and "image_generation" not in one and 'web_search="disabled"' not in one
    assert "code_mode_host" not in launch_args("http://h:1")


def test_the_best_effort_label_for_sub_agents_is_there_for_the_screen():
    assert set(st.FEATURE_INFO) == set(st.FEATURES)
    assert st.FEATURE_INFO["multi_agent"]["best_effort"] is True and st.FEATURE_INFO["multi_agent"]["note"]
    assert not any(st.FEATURE_INFO[f]["best_effort"] for f in st.FEATURES if f != "multi_agent")
    assert all(st.FEATURE_INFO[f]["label"] for f in st.FEATURES)


def test_stopping_sub_agents_is_also_asked_for_in_the_instructions():
    assert "サブエージェント" in st.feature_instructions(DEFAULTS["features"])
    assert st.feature_instructions({**DEFAULTS["features"], "multi_agent": True}) == ""
    assert st.feature_instructions({k: True for k in st.FEATURES}) == ""


# ---- hello (design 6) ----------------------------------------------------------------------
def test_hello_says_protocol_2_what_this_agent_can_do_and_its_limits():
    h = st.hello_fields(DEFAULTS)
    assert h == {"protocol": 2, "capabilities": {"resumable": True, "pre_approval": False},
                 "limits": {"five_hour": 80, "weekly": 80, "force": False}}
    s = copy.deepcopy(DEFAULTS)
    s["disposable"] = True
    s["pre_approval"]["enabled"] = True
    s["limits"] = {"five_hour": 60, "weekly": None, "force": True}
    h = st.hello_fields(s)
    assert h["capabilities"] == {"resumable": False, "pre_approval": True}
    assert h["limits"] == {"five_hour": 60, "weekly": None, "force": True}
    # what the host makes of it (host/lenders.py): the same, nothing dropped or turned around
    assert lenders.as_protocol(h["protocol"]) == 2
    assert lenders.normalize_capabilities(h["capabilities"]) == h["capabilities"]
    assert lenders.normalize_limits(h["limits"]) == h["limits"]
    h["limits"]["five_hour"] = 1  # a copy: the settings are not changed through it
    assert s["limits"]["five_hour"] == 60


class FakeWs:
    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(protocol.decode(text))


async def test_the_control_client_builds_hello_at_the_time_it_is_sent(tmp_path):
    e = Env(tmp_path, {})
    state = {"resumable": True}
    client = ControlClient(Invite("http://h:1", "tok", "x"), e.runner,
                           hello=lambda: {"agent_version": "0.2", "capabilities": {"resumable": state["resumable"]}})
    client.ws = ws = FakeWs()
    await client.send_hello()
    state["resumable"] = False
    await client.send_hello()
    assert [m["capabilities"]["resumable"] for m in ws.sent] == [True, False]
    assert all(m["type"] == "hello" and m["agent_version"] == "0.2" and m["current_task_id"] is None for m in ws.sent)
    e.runner.cur = Current(task_id="t9")
    await client.send_hello()
    assert ws.sent[-1]["current_task_id"] == "t9"
    client.ws = None
    await client.send_hello()  # not connected: nothing to do, no error
    assert len(ws.sent) == 3
    fixed = ControlClient(Invite("http://h:1", "tok", "x"), e.runner, hello={"agent_version": "0.1"})  # v0.1 style
    fixed.ws = ws2 = FakeWs()
    await fixed.send_hello()
    assert ws2.sent[0]["agent_version"] == "0.1"


# ---- the work folder (design 4.8) -------------------------------------------------------------
def test_the_default_work_folder_is_under_program_data_and_is_the_same_every_time(tmp_path):
    base = tmp_path / "ProgramData" / "codex-bridge-agent"
    root = tmp_path / "data"
    wd = st.default_workdir(root, base)
    assert wd.parent == base and re.fullmatch(r"work-[0-9a-z]{8}", wd.name) and wd.is_dir()
    (wd / "probe").write_text("x", encoding="utf-8")  # writable
    assert st.default_workdir(root, base) == wd  # not a new folder at every start
    assert list(base.iterdir()) == [wd]
    assert st.default_workdir(tmp_path / "other-data", base) != wd  # one per installation


def test_the_work_folder_falls_back_to_the_data_location_when_program_data_cannot_be_used(tmp_path):
    root = tmp_path / "data"
    blocked = tmp_path / "ProgramData-is-a-file"
    blocked.write_text("x", encoding="utf-8")
    wd = st.default_workdir(root, blocked / "codex-bridge-agent")
    assert wd == root / "workdir" and wd.is_dir()
    assert not (root / "workdir.txt").exists()  # the fallback is not remembered: it moves with the data
    # the remembered folder is gone (someone deleted it): a new one is made, no failure
    base = tmp_path / "pd"
    first = st.default_workdir(root, base)
    shutil.rmtree(first)
    assert st.default_workdir(root, base).is_dir()


def test_the_chosen_work_folder_is_used_and_an_unusable_one_falls_back_to_the_default(tmp_path):
    root, base = tmp_path / "data", tmp_path / "pd"
    mine = tmp_path / "my" / "work"
    assert st.resolve_workdir({**DEFAULTS, "workdir": str(mine)}, root, base) == mine and mine.is_dir()
    assert st.resolve_workdir(DEFAULTS, root, base).parent == base
    file = tmp_path / "afile"
    file.write_text("x", encoding="utf-8")
    assert st.resolve_workdir({**DEFAULTS, "workdir": str(file / "sub")}, root, base).parent == base


# ---- where the data is (design 4.7) --------------------------------------------------------------
def build_root(root: Path) -> None:
    (root / "logs" / "tasks").mkdir(parents=True)
    (root / "bin").mkdir()
    (root / "workdir").mkdir()
    (root / "invite.bin").write_bytes(bytes(range(256)) * 10)
    (root / "settings.json").write_text(json.dumps(DEFAULTS), encoding="utf-8")
    (root / "sessions.json").write_text("{}", encoding="utf-8")
    (root / "logs" / "log.jsonl").write_text('{"a": "日本語"}\n', encoding="utf-8")
    WorkLog(root / "logs" / "tasks").add("t1", "task_prompt", {"text": "やって"})
    (root / "bin" / "codex.exe").write_bytes(os.urandom(300_000))
    (root / "bin" / "codex-x.dll").write_bytes(b"dll")


@pytest.mark.skipif(os.name != "nt", reason="Windows junction")
def test_final_review_remove_junction_leaves_target_untouched(tmp_path):
    from conftest import make_junction
    target = tmp_path / "outside"
    target.mkdir()
    file = target / "keep.txt"
    file.write_bytes(b"keep")
    link = tmp_path / "junction"
    make_junction(link, target)
    try:
        left = []
        st._remove_tree(link, left)
        assert not link.exists() and left == []
        assert file.read_bytes() == b"keep"
    finally:
        if os.path.isjunction(link):
            link.rmdir()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction")
def test_final_review_copy_does_not_follow_junction(tmp_path):
    from conftest import make_junction
    src, dst, target = (tmp_path / p for p in ("src", "dst", "outside"))
    src.mkdir()
    target.mkdir()
    (target / "private.txt").write_bytes(b"private")
    link = src / "junction"
    make_junction(link, target)
    try:
        # Copy may refuse a junction; either way it must not materialize its target.
        try:
            st._copy_tree(src, dst, frozenset())
        except ValueError as e:
            assert "ジャンクション" in str(e)
        assert not (dst / "junction").exists() or os.path.isjunction(dst / "junction")
        assert (target / "private.txt").read_bytes() == b"private"
    finally:
        if os.path.isjunction(link):
            link.rmdir()


def snapshot(root: Path) -> dict:
    return {str(p.relative_to(root)): (p.read_bytes() if p.is_file() else None) for p in sorted(root.rglob("*"))}


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("CODEX_BRIDGE_AGENT_HOME", str(h))
    return h


def test_the_data_root_is_the_default_home_until_location_txt_says_otherwise(home, tmp_path):
    assert setup.data_root() == home
    target = tmp_path / "elsewhere" / "データ"
    (home / "location.txt").write_text(str(target), encoding="utf-8")
    assert setup.data_root() == target and target.is_dir()
    for bad in ("", "   \n", "relative\\path"):
        (home / "location.txt").write_text(bad, encoding="utf-8")
        assert setup.data_root() == home
    blocked = tmp_path / "afile"
    blocked.write_text("x", encoding="utf-8")
    (home / "location.txt").write_text(str(blocked / "sub"), encoding="utf-8")  # cannot be made: not a crash
    assert setup.data_root() == home


def test_moving_copies_verifies_switches_location_txt_and_removes_the_original(home, tmp_path):
    build_root(home)
    before = snapshot(home)
    new = tmp_path / "D" / "new-place"
    assert st.move_data_root(new) == []
    after = snapshot(new)
    assert after == before  # everything is there, byte for byte, including empty folders
    assert sorted(p.name for p in home.iterdir()) == ["location.txt"]  # the original is gone; location.txt stays
    assert (home / "location.txt").read_text(encoding="utf-8").strip() == str(new)
    assert setup.data_root() == new
    assert [e["data"] for e in WorkLog(setup.data_root() / "logs" / "tasks").task_events("t1")] == [{"text": "やって"}]


def test_moving_again_leaves_nothing_behind_and_can_go_back_home(home, tmp_path):
    build_root(home)
    before = snapshot(home)
    a, b = tmp_path / "a", tmp_path / "b"
    st.move_data_root(a)
    st.move_data_root(b)
    assert not a.exists() and snapshot(b) == before and setup.data_root() == b
    st.move_data_root(home)  # back to the default place (its location.txt is the only thing there)
    assert not b.exists() and setup.data_root() == home
    assert {k: v for k, v in snapshot(home).items() if k != "location.txt"} == before


def test_an_existing_empty_destination_is_used_and_kept_on_failure(home, tmp_path):
    build_root(home)
    new = tmp_path / "empty"
    new.mkdir()

    def broken(src, dst, skip):
        (dst / "partial.txt").write_text("half", encoding="utf-8")
        raise OSError("disk full")
    before = snapshot(home)
    with pytest.raises(OSError, match="disk full"):
        st.move_data_root(new, copy=broken)
    assert new.is_dir() and list(new.iterdir()) == [] and snapshot(home) == before  # their folder is theirs
    st.move_data_root(new)
    assert (new / "invite.bin").exists()


def _fail_after_first_file(src, dst, skip):
    (dst / "invite.bin").write_bytes((src / "invite.bin").read_bytes())
    (dst / "logs").mkdir()
    raise OSError("the disk was removed")


def _copy_then_corrupt(src, dst, skip):
    st._copy_tree(src, dst, skip)
    data = (dst / "bin" / "codex.exe").read_bytes()
    (dst / "bin" / "codex.exe").write_bytes(data[:-1])  # one byte short, same everything else


def _copy_then_change_a_byte(src, dst, skip):
    st._copy_tree(src, dst, skip)
    data = bytearray((dst / "invite.bin").read_bytes())
    data[5] ^= 1
    (dst / "invite.bin").write_bytes(bytes(data))  # same size: only a content check sees it


def _write_location_failing(home, new):
    raise OSError("location.txt is locked")


def _write_location_then_fail(home, new):
    (home / "location.txt").write_text(str(new), encoding="utf-8")
    raise OSError("failed after writing")


@pytest.mark.parametrize("kwargs", [
    {"copy": _fail_after_first_file}, {"copy": _copy_then_corrupt}, {"copy": _copy_then_change_a_byte},
    {"write_location": _write_location_failing}, {"write_location": _write_location_then_fail},
], ids=["copy-fails", "size-differs", "content-differs", "location-write-fails", "location-written-then-fails"])
@pytest.mark.parametrize("custom_current", [False, True], ids=["from-default", "from-custom"])
def test_a_failure_anywhere_leaves_the_original_in_use_and_untouched(home, tmp_path, kwargs, custom_current):
    # Review focus 5: copy, verify, switch, delete: whatever fails before the switch changes nothing
    root = home
    if custom_current:
        root = tmp_path / "custom"
        st._write_location(home, root)
        root.mkdir()
    build_root(root)
    before_root, before_home = snapshot(root), snapshot(home)
    new = tmp_path / "target"
    with pytest.raises(OSError):
        st.move_data_root(new, **kwargs)
    assert snapshot(root) == before_root and snapshot(home) == before_home  # byte for byte, location.txt too
    assert not new.exists()  # no half copy left behind
    assert setup.data_root() == root  # the next start uses the original
    st.move_data_root(new)  # and the same move works when nothing fails
    assert setup.data_root() == new and (new / "bin" / "codex.exe").exists()


def test_a_failure_to_delete_the_original_is_not_a_rollback_and_names_what_is_left(home, tmp_path):
    # after the switch the data is safely in the new place: a locked file (codex.exe while it runs) stays behind
    build_root(home)
    before = snapshot(home)
    new = tmp_path / "new"
    with open(home / "bin" / "codex.exe", "rb") as locked:  # Windows: an open file cannot be deleted
        left = st.move_data_root(new)
        assert locked.read(1)
    assert [p.name for p in left] == ["codex.exe"] and left[0].parent == home / "bin"
    assert setup.data_root() == new
    assert {k: v for k, v in snapshot(new).items()} == before
    assert (home / "bin" / "codex.exe").exists() and not (home / "invite.bin").exists()


def test_an_exception_from_the_delete_step_is_reported_not_raised(home, tmp_path):
    build_root(home)

    def boom(root, skip):
        raise OSError("nope")
    left = st.move_data_root(tmp_path / "new", remove=boom)
    assert left == [home] and setup.data_root() == tmp_path / "new"


@pytest.mark.parametrize("where", ["same", "inside", "parent", "relative", "not-empty", "a-file"])
def test_destinations_that_would_lose_or_mix_data_are_refused_before_anything_is_touched(home, tmp_path, where):
    build_root(home)
    before = snapshot(home)
    new = {"same": home, "inside": home / "deeper" / "x", "parent": tmp_path, "relative": Path("rel/place"),
           "not-empty": tmp_path / "docs", "a-file": tmp_path / "file.txt"}[where]
    if where == "not-empty":
        new.mkdir()
        (new / "mine.txt").write_text("my own file", encoding="utf-8")
    if where == "a-file":
        new.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        st.move_data_root(new)
    assert snapshot(home) == before
    if where == "not-empty":
        assert [p.name for p in new.iterdir()] == ["mine.txt"]


def test_a_custom_location_inside_which_the_home_lives_is_not_moved(home, tmp_path, monkeypatch):
    # deleting the original would delete location.txt itself
    h = tmp_path / "outer" / "home"
    h.mkdir(parents=True)
    monkeypatch.setenv("CODEX_BRIDGE_AGENT_HOME", str(h))
    st._write_location(h, tmp_path / "outer")
    (tmp_path / "outer" / "f.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        st.move_data_root(tmp_path / "new")
    assert (tmp_path / "outer" / "f.txt").exists()


# ---- the runner: disposable sessions (design 4.6) ----------------------------------------------
def done_script(final="できました", status="completed"):
    return {"after": {"turn/start": [n.turn_started(), n.agent_completed(final), n.turn_completed(status)]}}


class Env:
    def __init__(self, tmp_path, script):
        self.fake = Fake(tmp_path, script)
        self.sent: list[dict] = []
        self.logged: list[dict] = []
        self.sessions = SessionRegistry(tmp_path / "sessions.json")
        self.worklog = WorkLog(tmp_path / "logs" / "tasks")

        async def send(m):
            self.sent.append(m)
        self.runner = AgentRunner(self.fake.app, QuotaGuard({"five_hour": 90, "weekly": 95}), self.sessions, send,
                                  usage_interval=3600, tick_interval=3600, on_log=self.logged.append,
                                  worklog=self.worklog)

    def states(self):
        return [m["state"] for m in self.sent if m["type"] == "task_state"]

    async def wait_state(self, state, timeout=5):
        for _ in range(int(timeout / 0.05)):
            found = [m for m in self.sent if m["type"] == "task_state" and m["state"] == state]
            if found:
                return found[-1]
            await asyncio.sleep(0.05)
        raise AssertionError(f"no {state}: {self.states()}")

    def disposable(self, on=True):
        self.runner.apply_settings({**copy.deepcopy(DEFAULTS), "disposable": on})

    async def start_task(self, task_id="t1", cont=None):
        await self.runner.handle({"type": "task_start", "task_id": task_id, "prompt": "do",
                                  "continue_thread_id": cont})

    async def forgotten(self, thread_id="th1"):
        await self.fake.wait_for(lambda: not self.sessions.owns(thread_id))


@pytest.fixture
async def env(tmp_path, request):
    e = Env(tmp_path, getattr(request, "param", {}))
    await e.fake.app.start()
    await e.runner.start()
    yield e
    await e.runner.stop()
    await e.fake.app.stop()


@pytest.mark.parametrize("env", [done_script()], indirect=True)
async def test_a_disposable_task_starts_an_ephemeral_thread_and_forgets_it_when_done(env):
    env.disposable()
    await env.start_task()
    assert env.fake.received("thread/start")["ephemeral"] is True
    await env.wait_state("completed")
    await env.forgotten()
    assert env.sessions.list() == [] and env.runner.cur is None
    assert env.fake.received("thread/delete") == {"threadId": "th1"}  # and the server is asked to drop it
    # the record of what was done stays (design 4.10): not the conversation, only what happened
    assert [e["kind"] for e in env.worklog.task_events("t1")][0] == "task_prompt"
    assert env.worklog.task_events("t1")[-1]["data"]["state"] == "completed"


@pytest.mark.parametrize("env", [done_script()], indirect=True)
async def test_a_normal_task_is_not_ephemeral_and_keeps_its_session(env):
    await env.start_task()
    await env.wait_state("completed")
    assert "ephemeral" not in env.fake.received("thread/start")
    assert env.sessions.owns("th1") and env.sessions.list()[0].get("ephemeral") is False
    assert env.fake.received("thread/delete") is None


@pytest.mark.parametrize("env", [{"after": {"turn/start": [n.turn_started()]}}], indirect=True)
async def test_while_it_runs_the_session_is_listed_as_disposable_and_followups_still_work(env):
    env.disposable()
    await env.start_task()
    await env.wait_state("running")
    assert [s["ephemeral"] for s in env.sessions.list()] == [True]
    await asyncio.sleep(0.2)
    await env.runner.handle({"type": "task_followup", "task_id": "t1", "text": "もう一つ"})
    p = await env.fake.wait_for(lambda: env.fake.received("turn/steer"))
    assert p["input"][0]["text"] == "もう一つ"
    env.disposable(False)  # switched off while it runs: this task still ends the way it began
    assert env.runner.cur.ephemeral is True


@pytest.mark.parametrize("env", [done_script("途中で失敗", "failed")], indirect=True)
async def test_a_failed_turn_also_discards_the_session(env):
    env.disposable()
    await env.start_task()
    await env.wait_state("failed")
    await env.forgotten()


@pytest.mark.parametrize("env", [{"after": {"turn/start": [n.turn_started()],
                                            "turn/interrupt": [n.turn_completed("interrupted")]}}], indirect=True)
async def test_a_cancelled_task_also_discards_the_session(env):
    env.disposable()
    await env.start_task()
    await env.wait_state("running")
    await asyncio.sleep(0.2)
    await env.runner.handle({"type": "task_cancel", "task_id": "t1"})
    await env.wait_state("cancelled")
    await env.forgotten()


@pytest.mark.parametrize("env", [{"after": {"turn/start": [n.turn_started()]}}], indirect=True)
async def test_a_cancel_without_a_running_turn_and_a_lost_app_server_also_discard_the_session(env, tmp_path):
    env.disposable()
    await env.start_task()
    await env.wait_state("running")
    env.runner.cur.turn_id = None
    await env.runner.handle({"type": "task_cancel", "task_id": "t1"})
    await env.wait_state("cancelled")
    await env.forgotten()
    await env.start_task("t2")
    await env.runner._on_event({"method": "_exited", "params": {}})
    await env.wait_state("failed")
    assert env.sessions.list() == [] and env.runner.cur is None


QUOTA = {"responses": {"account/rateLimits/read": {"rateLimits": {
    "primary": {"usedPercent": 91, "windowDurationMins": 300, "resetsAt": 1000}, "secondary": None}}},
    "after": {"turn/start": [n.turn_started()], "turn/steer": [n.agent_completed("ここまで"), n.turn_completed()]}}


@pytest.mark.parametrize("env", [QUOTA], indirect=True)
async def test_a_task_stopped_by_the_usage_limit_cannot_be_continued_either(env):
    env.disposable()
    await env.start_task()
    await env.wait_state("running")
    await asyncio.sleep(0.2)
    await env.runner.refresh_usage()
    s = await env.wait_state("suspended_quota")
    assert s["resume_at"] == 1000
    await env.forgotten()


@pytest.mark.parametrize("env", [{}], indirect=True)
async def test_a_disposable_lender_refuses_to_continue_and_says_why(env):
    env.disposable()
    env.sessions.add("old-thread", "t0")  # a session from before it became disposable
    await env.start_task("t1", cont="old-thread")
    failed = await env.wait_state("failed")
    assert "続き" in failed["report"] and "使い捨て" in failed["report"]
    assert env.fake.received("thread/resume") is None and env.fake.received("thread/start") is None
    assert env.runner.cur is None and env.sessions.owns("old-thread")  # nothing else was touched


@pytest.mark.parametrize("env", [done_script()], indirect=True)
async def test_a_normal_lender_still_continues_a_session(env):
    await env.start_task()
    await env.wait_state("completed")
    env.sent.clear()
    await env.start_task("t2", cont="th1")
    await env.wait_state("completed")
    assert env.fake.received("thread/resume")["threadId"] == "th1"


async def test_ephemeral_sessions_left_by_a_crash_are_not_listed_after_a_restart(tmp_path):
    reg = SessionRegistry(tmp_path / "sessions.json")
    reg.add("kept", "t1")
    reg.add("gone", "t2", ephemeral=True)
    e = Env(tmp_path, {})
    await e.fake.app.start()
    await e.runner.start()
    try:
        assert [s["thread_id"] for s in SessionRegistry(tmp_path / "sessions.json").list()] == ["kept"]
        assert [s["thread_id"] for s in e.sessions.list()] == ["kept"]
    finally:
        await e.runner.stop()
        await e.fake.app.stop()


@pytest.mark.parametrize("env", [done_script()], indirect=True)
async def test_the_developer_instructions_ask_not_to_use_sub_agents_unless_they_are_allowed(env):
    await env.start_task()
    await env.wait_state("completed")
    text = env.fake.received("thread/start")["developerInstructions"]
    assert text.startswith(PREAMBLE) and "サブエージェント" in text
    env.runner.apply_settings({**copy.deepcopy(DEFAULTS), "features": {**DEFAULTS["features"], "multi_agent": True}})
    await env.start_task("t2")
    assert env.fake.received("thread/start")["developerInstructions"] == PREAMBLE


# ---- putting a change to work ------------------------------------------------------------------------------
def all_argv(fake):
    return [l["argv"] for l in fake._lines() if "argv" in l]


class Rig:
    """An app-server, a runner and an applier wired like agent/main.py does."""

    def __init__(self, tmp_path):
        self.env = Env(tmp_path, {})
        self.root, self.base = tmp_path / "data", tmp_path / "pd"
        self.store = st.SettingsStore(self.root / "settings.json")
        self.hellos = 0
        self.env.fake.app.workdir = st.resolve_workdir(self.store.get(), self.root, self.base)
        self.env.runner.apply_settings(self.store.get())

        async def hello():
            self.hellos += 1
        self.applier = st.SettingsApplier(self.store, self.env.fake.app, self.env.runner, self.root,
                                          refresh_hello=hello, base=self.base, on_log=self.env.logged.append)

    async def change(self, patch):
        return await self.applier.apply(self.store.update(patch))


@pytest.fixture
async def rig(tmp_path):
    r = Rig(tmp_path)
    await r.env.fake.app.start()
    await r.env.runner.start()
    yield r
    await r.env.runner.stop()
    await r.env.fake.app.stop()


async def test_a_changed_feature_restarts_the_app_server_with_the_new_arguments_when_idle(rig):
    app = rig.env.fake.app
    assert 'web_search="disabled"' in all_argv(rig.env.fake)[-1]
    await rig.change({"features": {"web_search": True}})
    argvs = all_argv(rig.env.fake)
    assert len(argvs) == 2 and 'web_search="disabled"' not in argvs[-1] and "goals" in argvs[-1]
    assert app.running
    await rig.change({"features": {"web_search": True}})  # nothing changed: no restart
    await rig.change({"limits": {"five_hour": 50}, "log_retention_days": 9})  # not launch arguments: no restart
    assert len(all_argv(rig.env.fake)) == 2
    # and the new server works: a task runs on it
    await rig.env.start_task()
    assert rig.env.fake.received("thread/start")["cwd"] == str(app.workdir)


async def test_a_restart_is_not_mistaken_for_a_crash_by_a_task_started_right_after(rig):
    await rig.change({"features": {"goals": True}})
    await rig.env.start_task()
    await asyncio.sleep(0.3)  # a stale "app-server exited" would fail the task here
    assert "failed" not in rig.env.states() and rig.env.runner.cur is not None


async def test_while_a_task_runs_the_restart_waits_until_it_is_over(rig):
    rig.env.runner.cur = Current(task_id="t1", thread_id="th1")
    await rig.change({"features": {"image_generation": True}})
    assert len(all_argv(rig.env.fake)) == 1  # still the old server
    await rig.applier.settle()
    assert len(all_argv(rig.env.fake)) == 1
    rig.env.runner.cur = None
    await rig.applier.settle()  # the periodic check in main.py
    argvs = all_argv(rig.env.fake)
    assert len(argvs) == 2 and "image_generation" not in argvs[-1]
    await rig.applier.settle()
    assert len(all_argv(rig.env.fake)) == 2


async def test_a_changed_work_folder_is_used_after_a_restart(rig, tmp_path):
    mine = tmp_path / "my-work"
    await rig.change({"workdir": str(mine)})
    assert rig.env.fake.app.workdir == mine and mine.is_dir() and len(all_argv(rig.env.fake)) == 2
    await rig.env.start_task()
    assert rig.env.fake.received("thread/start")["cwd"] == str(mine)


async def test_limits_and_retention_and_disposable_take_effect_at_once(rig):
    await rig.change({"limits": {"five_hour": 50, "weekly": None, "force": True}, "log_retention_days": 9,
                      "disposable": True})
    r = rig.env.runner
    assert r.friend_limits == {"five_hour": 50, "weekly": None, "force": True}
    assert rig.env.worklog.retention_days == 9 and r.settings["disposable"] is True
    assert len(all_argv(rig.env.fake)) == 1  # no restart for these


async def test_hello_is_sent_again_only_when_what_it_says_about_the_agent_changed(rig):
    await rig.change({"limits": {"five_hour": 50}})
    await rig.change({"features": {"goals": True}})
    assert rig.hellos == 0
    await rig.change({"disposable": True})
    assert rig.hellos == 1
    await rig.change({"disposable": True})
    assert rig.hellos == 1
    await rig.change({"pre_approval": {"enabled": True}})
    assert rig.hellos == 2
    await rig.change({"pre_approval": {"timeout_min": 10}})
    assert rig.hellos == 2


async def test_a_failing_restart_is_logged_and_tried_again_at_the_next_check(rig, monkeypatch):
    app = rig.env.fake.app
    real = type(app).restart
    calls = []

    async def failing(self, *a, **k):
        calls.append(1)
        raise OSError("cannot start")
    monkeypatch.setattr(type(app), "restart", failing)
    await rig.change({"features": {"goals": True}})  # the setting is saved; applying it failed softly
    assert calls and any(e.get("type") == "error" and "cannot start" in e["text"] for e in rig.env.logged)
    monkeypatch.setattr(type(app), "restart", real)
    await rig.applier.settle()
    assert len(all_argv(rig.env.fake)) == 2


async def test_moving_the_data_needs_an_idle_agent_and_stops_codex_while_it_copies(rig, home, tmp_path):
    build_root(home)
    rig.applier.root = home  # the running data root (here: the default place, no location.txt)
    rig.env.runner.cur = Current(task_id="t1", thread_id="th1")
    with pytest.raises(ValueError, match="作業中"):
        await rig.applier.move_data(tmp_path / "new")
    assert rig.env.fake.app.running and (home / "invite.bin").exists()
    rig.env.runner.cur = None
    seen = []

    def spy(new, **k):  # at the moment of the copy Codex (and its lock on bin/codex.exe) must be gone
        seen.append(rig.env.fake.app.running)
        return st.move_data_root(new, **k)
    rig.applier._move = spy
    await rig.applier.move_data(tmp_path / "new")
    assert seen == [False] and setup.data_root() == tmp_path / "new"


async def test_a_failed_move_starts_codex_again_and_changes_nothing(rig, home, tmp_path):
    build_root(home)
    rig.applier.root = home
    before = snapshot(home)
    rig.applier._move = lambda new, **k: st.move_data_root(new, copy=_fail_after_first_file)
    with pytest.raises(OSError):
        await rig.applier.move_data(tmp_path / "new")
    assert rig.env.fake.app.running and snapshot(home) == before and setup.data_root() == home


async def test_the_data_is_moved_from_the_running_root_even_when_location_txt_says_another_place(rig, home, tmp_path):
    # location.txt names a place that is not the one in use (edited by hand, or an earlier move that only half
    # worked): the source of the move is where the data is now, not where location.txt points.
    running, stale = rig.root, tmp_path / "stale"
    build_root(running)
    stale.mkdir()
    (stale / "decoy.txt").write_text("not the data", encoding="utf-8")
    st._write_location(home, stale)
    before = snapshot(running)
    new = tmp_path / "new"
    left = await rig.applier.move_data(new)
    assert left == [] and not running.exists()
    assert snapshot(new) == before  # what was in use is what arrived
    assert (stale / "decoy.txt").read_text(encoding="utf-8") == "not the data"  # the other place is untouched
    assert setup.data_root() == new  # and location.txt now says where the data is
