import asyncio
import subprocess
import sys
from threading import Event

import pytest

from codex_bridge.host.command_rules import inspect_commands, lex
from codex_bridge.host.paths import ToolError
from codex_bridge.host.runner import match_allow

PY = f'"{sys.executable}"'


async def finish(runner, r: dict) -> dict:
    """Poll get_command_result until the job leaves running/waiting."""
    for _ in range(30):
        if r["status"] not in ("running", "waiting_approval"):
            return r
        r = await runner.get_command_result(r["job_id"], 5)
    return r


def test_allowlist_match():
    pats = ["python scripts/**/*.py *"]
    assert match_allow("python scripts/a/b.py --in raw/x", pats)
    assert match_allow("python scripts/b.py", pats)
    assert match_allow("python scripts\\a\\b.py x", pats)
    assert not match_allow("python other.py", pats)
    assert not match_allow("python scripts/a.py && del x", pats)
    assert not match_allow("python scripts/a.py | more", pats)
    assert not match_allow("python scripts/../../evil.py", pats)
    assert not match_allow("python scripts/a.txt", pats)
    assert not match_allow(r"A:\share\python.exe scripts/x.py", pats)
    assert not match_allow('python "scripts/a.py', pats)


def test_allowlist_exact_tokens():
    assert match_allow("git status", ["git status"])
    assert not match_allow("git status --porcelain", ["git status"])


@pytest.mark.parametrize("command, pattern", [
    ("echo a && git push", "git push *"),
    ("echo a||git push|more", "git push *"),
    ("echo a&git push;echo b", "git push *"),
    ("echo a\r\ngit push", "git push *"),
    ('cmd /c "git push origin"', "git push *"),
    ("CMD.EXE /K git push origin", "git push *"),
    ('powershell -Command "git push"', "git push *"),
    ("pwsh -c git push", "git push *"),
    ('pwsh -c "git push" origin', "git push *"),
    ("bash -c 'rm -rf x'", "rm -rf *"),
    ("""cmd /c "pwsh -c 'sh -c git'" """, "git"),
    (r'"C:\Program Files\Git\cmd\git.exe" push', "git push *"),
    (r'C:\Program Files\Git\cmd\git.exe push', "git push *"),
    (r'cmd /c ""C:\Program Files\Git\cmd\git.exe" push"', "git push *"),
    ('git push "unfinished', "git push *"),
    ('git push && echo "unfinished', "git push *"),
])
async def test_command_rules_match_segments_and_wrappers(share, monkeypatch, command, pattern):
    async def unexpected(*args):
        pytest.fail("the command must be blocked or wait for approval")

    monkeypatch.setattr(share.runner, "_execute", unexpected)
    share.set_policy(exec_mode="all", exec_deny=[pattern], exec_ask=[pattern], limits={"sync_wait_sec": 0})
    denied = await share.runner.run_command(command)
    assert denied["status"] == "denied"
    assert denied["message"] == f"コマンドの規則の拒否リストに当たるため実行しません: {pattern}"
    assert not share.approvals.pending()

    share.set_policy(exec_deny=[])
    waiting = await share.runner.run_command(command)
    assert waiting["status"] == "waiting_approval"
    approval, = share.approvals.pending()
    share.approvals.decide(approval.id, False)
    await asyncio.wait_for(share.runner.jobs[waiting["job_id"]].done.wait(), timeout=2)


@pytest.mark.parametrize("mode, deny, ask, modified, allowed, status", [
    ("off", True, True, True, True, "denied"),
    ("all", True, True, True, True, "denied"),
    ("all", False, True, True, True, "waiting_approval"),
    ("all", False, False, True, True, "waiting_approval"),
    ("allowlist", False, False, True, True, "waiting_approval"),
    ("allowlist", False, False, False, True, "done"),
    ("allowlist", False, False, False, False, "waiting_approval"),
    ("ask", False, False, False, True, "waiting_approval"),
    ("all", False, False, False, False, "done"),
])
async def test_command_decision_order(share, monkeypatch, mode, deny, ask, modified, allowed, status):
    command = "python scripts/new.py"
    executed = []

    async def execute(job, cwd, timeout):
        executed.append(job.command)
        job.status = "done"

    monkeypatch.setattr(share.runner, "_execute", execute)
    share.task("t1")
    share.write.write_file("scripts/new.py", "print(1)\n")
    share.set_policy(exec_mode=mode, exec_deny=[command] if deny else [], exec_ask=[command] if ask else [],
                     exec_allow=[command] if allowed else [], exec_confirm_modified_scripts=modified,
                     limits={"sync_wait_sec": 0})
    reply = await share.runner.run_command(command)
    job = share.runner.jobs[reply["job_id"]]
    if status != "waiting_approval":
        await asyncio.wait_for(job.done.wait(), timeout=2)
    assert job.status == status
    if status == "waiting_approval":
        approval, = share.approvals.pending()
        share.approvals.decide(approval.id, False)
        await asyncio.wait_for(job.done.wait(), timeout=2)
    else:
        assert not share.approvals.pending()
    assert executed == ([command] if status == "done" else [])
    if mode == "off":
        assert "off" in reply["message"]


async def test_rule_separators_respect_quotes_and_wrapper_depth(share, monkeypatch):
    async def execute(job, cwd, timeout):
        job.status = "done"

    monkeypatch.setattr(share.runner, "_execute", execute)
    share.set_policy(exec_mode="all", exec_deny=["blocked"], limits={"sync_wait_sec": 1})
    assert (await share.runner.run_command('echo "quoted && blocked"'))["status"] == "done"
    waiting = await share.runner.run_command("cmd /c cmd /c cmd /c cmd /c blocked")
    assert waiting["status"] == "waiting_approval"
    approval, = share.approvals.pending()
    share.approvals.decide(approval.id, False)
    await share.runner.jobs[waiting["job_id"]].done.wait()
    assert (await share.runner.run_command("cmd /c cmd /c cmd /c blocked"))["status"] == "denied"


@pytest.mark.parametrize("shell, source, expected", [
    ("cmd", r'write "sample\" & show next', [["write", "sample\\"], ["show", "next"]]),
    ("cmd", "write 'one & show two'", [["write", "'one"], ["show", "two'"]]),
    ("cmd", 'show pa^rt left^&right "a^b"', [["show", "part", "left&right", "a^b"]]),
    ("posix", r"""show 'one & two' name\ space; show "literal\q" """,
     [["show", "one & two", "name space"], ["show", "literal\\q"]]),
    ("powershell", 'show "a' + chr(96) + """"b"; show 'one | two'""",
     [["show", 'a"b'], ["show", "one | two"]]),
])
def test_rule_lexer_keeps_each_shells_literals(shell, source, expected):
    segments, uncertain = lex(source, shell)
    assert [[word.value for word in words] for words in segments] == expected
    assert not uncertain


@pytest.mark.parametrize("source", [
    'cmd /s /C "show blue"',
    'cmd /C"show blue"',
    '%COMSPEC% /k "show blue"',
    'pwsh -NoProfile -comm "show blue"',
    """bash -lc 'show blue'""",
    'call show blue',
])
def test_inspection_finds_nested_sample_command(source):
    commands, uncertain = inspect_commands(source)
    assert ["show", "blue"] in commands
    assert not uncertain


@pytest.mark.parametrize("source", [
    'show "unfinished', 'show last^', 'pwsh -NoProfile -ec sample',
    'powershell -EncodedCommand sample', 'bash -lc', 'cmd /c cmd /c cmd /c cmd /c show',
    'show (sample)', 'show %SAMPLE%',
])
async def test_unreadable_input_requires_approval(share, source):
    share.set_policy(exec_mode="all", exec_deny=["show"], limits={"sync_wait_sec": 0})
    reply = await share.runner.run_command(source)
    assert reply["status"] == "waiting_approval"
    approval, = share.approvals.pending()
    share.approvals.decide(approval.id, False)
    await share.runner.jobs[reply["job_id"]].done.wait()


@pytest.mark.parametrize("rule_type", ["deny", "ask"])
@pytest.mark.parametrize("source, expected", [
    ('show "assets/color.dat"', True),
    ('show assets/../assets/color.dat', True),
    ('show assets/../images/color.dat', False),
])
async def test_restrictive_rules_match_normalized_arguments(share, monkeypatch, rule_type, source, expected):
    async def execute(job, cwd, timeout):
        job.status = "done"

    monkeypatch.setattr(share.runner, "_execute", execute)
    share.set_policy(exec_mode="all", limits={"sync_wait_sec": 0}, **{f"exec_{rule_type}": ["show assets/**"]})
    reply = await share.runner.run_command(source)
    job = share.runner.jobs[reply["job_id"]]
    if expected and rule_type == "ask":
        assert reply["status"] == "waiting_approval"
        approval, = share.approvals.pending()
        share.approvals.decide(approval.id, False)
        await job.done.wait()
    else:
        await job.done.wait()
        assert job.status == ("denied" if expected else "done")


@pytest.mark.parametrize("source, expected", [
    ('echo before & cmd /s /c "python scripts/./new.py"', True),
    ('cmd /c "python scripts/example.py"', False),
])
async def test_modified_script_is_checked_in_nested_commands(share, monkeypatch, source, expected):
    async def execute(job, cwd, timeout):
        job.status = "done"

    monkeypatch.setattr(share.runner, "_execute", execute)
    share.task("t1")
    share.write.write_file("scripts/new.py", "print(1)\n")
    share.set_policy(exec_mode="all", exec_confirm_modified_scripts=True, limits={"sync_wait_sec": 0})
    reply = await share.runner.run_command(source)
    job = share.runner.jobs[reply["job_id"]]
    if expected:
        assert reply["status"] == "waiting_approval"
        approval, = share.approvals.pending()
        share.approvals.decide(approval.id, False)
    await job.done.wait()
    assert job.status == ("denied" if expected else "done")


async def test_new_rules_apply_after_the_pre_execution_scan(share, monkeypatch):
    loop = asyncio.get_running_loop()
    scanning = asyncio.Event()
    release = Event()

    def scan(*args):
        loop.call_soon_threadsafe(scanning.set)
        assert release.wait(5)
        return {}

    async def unexpected(*args, **kwargs):
        pytest.fail("the command became denied during the scan")

    monkeypatch.setattr(share.runner, "_write_area_state", scan)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", unexpected)
    share.set_policy(exec_mode="all", limits={"sync_wait_sec": 0})
    reply = await share.runner.run_command("echo example")
    job = share.runner.jobs[reply["job_id"]]
    try:
        await asyncio.wait_for(scanning.wait(), timeout=5)
        share.set_policy(exec_deny=["echo example"])
    finally:
        release.set()
    await asyncio.wait_for(job.done.wait(), timeout=5)
    assert job.status == "denied" and "拒否リスト" in job.message


async def test_mode_off_refuses(share):
    share.set_policy(exec_mode="off")
    r = await share.runner.run_command(f"{PY} -c 1")
    assert r["status"] == "denied" and "実行は許可されていません" in r["message"]


async def test_mode_all_runs(share):
    share.set_policy(exec_mode="all")
    r = await share.runner.run_command(f'{PY} -c "print(1)"')
    assert r["status"] == "done" and r["exit_code"] == 0 and r["stdout"].strip() == "1"


async def test_final_review_output_path_is_unguessable_and_owner_can_retrieve(share):
    import re
    from pathlib import Path
    share.set_policy(exec_mode="all", limits={"output_max_bytes": 2048})
    share.runner.lender_fn = lambda: "alice"
    r = await share.runner.run_command(f'{PY} -c "print(\'x\'*3000)"')
    result = await share.runner.get_command_result(r["job_id"], 5)
    assert re.fullmatch(r"[0-9a-f]{32}\.stdout\.txt", Path(result["output_file"]).name)
    assert share.read.read_file(result["output_file"])["text"].startswith("xxx")
    share.runner.lender_fn = lambda: "bob"
    with pytest.raises(ToolError, match="ジョブが見つかりません"):
        await share.runner.get_command_result(r["job_id"], 0)


async def test_allowlisted_runs_without_approval(share):
    (share.root / "scripts/hello.py").write_text("print('hi')\n", encoding="utf-8")
    share.set_policy(exec_allow=[f"{PY} scripts/*.py *"])
    r = await share.runner.run_command(f"{PY} scripts/hello.py")
    assert r["status"] == "done" and r["stdout"].strip() == "hi"


async def test_not_allowlisted_waits_for_approval(share):
    share.set_policy(limits={"sync_wait_sec": 1})
    r = await share.runner.run_command(f'{PY} -c "print(1)"')
    assert r["status"] == "waiting_approval" and r["job_id"]
    assert share.approvals.pending()[0].cwd == "."


async def test_ask_mode_waits_then_runs_after_approval(share):
    share.set_policy(exec_mode="ask", limits={"sync_wait_sec": 1})
    r = await share.runner.run_command(f'{PY} -c "print(1)"')
    assert r["status"] == "waiting_approval"
    share.approvals.decide(share.approvals.pending()[0].id, True)
    r2 = await finish(share.runner, await share.runner.get_command_result(r["job_id"], wait_sec=10))
    assert r2["status"] == "done" and r2["stdout"].strip() == "1"


@pytest.mark.parametrize("pause_first", [True, False], ids=["pause-before-approval", "pause-after-approval"])
async def test_pause_prevents_an_approved_command_from_starting(share, pause_first):
    share.set_policy(exec_mode="ask", limits={"sync_wait_sec": 1})
    marker = share.root / "out" / "ran.txt"
    result = await share.runner.run_command(f'{PY} -c "open(\'out/ran.txt\', \'w\').close()"')
    assert result["status"] == "waiting_approval"

    if pause_first:
        share.set_policy(paused=True)
    share.approvals.decide(share.approvals.pending()[0].id, True)
    if not pause_first:
        share.set_policy(paused=True)
    result = await finish(share.runner, result)
    assert result["status"] == "denied"
    assert "停止中" in result["message"]
    assert not marker.exists()


async def test_denied_approval(share):
    share.set_policy(exec_mode="ask", limits={"sync_wait_sec": 1})
    r = await share.runner.run_command(f"{PY} -c 1")
    share.approvals.decide(share.approvals.pending()[0].id, False)
    assert (await share.runner.get_command_result(r["job_id"], 5))["status"] == "denied"


async def test_job_started_without_a_lender_is_hidden_from_a_named_lender(share):
    share.set_policy(exec_mode="off")
    job_id = (await share.runner.run_command(f"{PY} -c 1"))["job_id"]
    assert (await share.runner.get_command_result(job_id, 1))["status"] == "denied"  # no lender on either side
    share.runner.lender_fn = lambda: "bob"
    with pytest.raises(ToolError, match=f"ジョブが見つかりません: {job_id}"):
        await share.runner.get_command_result(job_id, 1)


async def test_unanswered_approval_expires(share):
    share.set_policy(exec_mode="ask", limits={"approval_timeout_sec": 1, "sync_wait_sec": 1})
    r = await share.runner.run_command(f"{PY} -c 1")
    await asyncio.sleep(1.5)
    assert (await share.runner.get_command_result(r["job_id"], 2))["status"] == "denied"


async def test_long_command_returns_job_id_then_result(share):
    share.set_policy(exec_mode="all", limits={"sync_wait_sec": 1})
    r = await share.runner.run_command(f'{PY} -c "import time;time.sleep(2);print(\'ok\')"')
    assert r["status"] == "running"
    r = await finish(share.runner, r)
    assert r["status"] == "done" and r["stdout"].strip() == "ok"


async def test_timeout_kills_process_tree(share):
    share.set_policy(exec_mode="all")
    (share.root / "scripts/spawn.py").write_text(
        "import subprocess,sys,time\n"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
        "open('out/child.pid','w').write(str(p.pid))\n"
        "time.sleep(60)\n", encoding="utf-8")
    r = await share.runner.run_command(f"{PY} scripts/spawn.py", timeout_sec=3)
    assert r["status"] == "timeout"
    pid = (share.root / "out/child.pid").read_text()
    await asyncio.sleep(0.5)
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True).stdout
    assert pid not in out


async def test_large_output_spills_to_file(share):
    share.set_policy(exec_mode="all")
    r = await share.runner.run_command(f'{PY} -c "import sys;sys.stdout.write(\'x\'*(2*1024*1024))"')
    assert r["status"] == "done" and len(r["stdout"].encode()) < 1024 * 1024
    assert r["output_file"].startswith(".codex-bridge/outputs/")
    assert (share.root / r["output_file"]).stat().st_size == 2 * 1024 * 1024
    assert share.read.read_file(r["output_file"], max_lines=1)["text"].startswith("x")


async def test_outputs_dir_readable_even_if_default_hidden(share):
    share.set_policy(default="hidden", exec_mode="all")
    r = await share.runner.run_command(f'{PY} -c "import sys;sys.stdout.write(\'x\'*(2*1024*1024))"')
    assert share.read.stat(r["output_file"])["size"] == 2 * 1024 * 1024


async def test_changed_files_reported(share):
    share.set_policy(exec_mode="all")
    r = await share.runner.run_command(f"{PY} -c \"open('out/made.txt','w').write('m')\"")
    assert r["changed_files"] == ["out/made.txt"]


@pytest.mark.parametrize("nested", [False, True])
async def test_changed_files_ignore_shares_added_or_removed_during_command(share, monkeypatch, nested):
    from codex_bridge.host.policy_form import remove_folder

    share.set_policy(exec_mode="all")
    added = share.root / "docs" if nested else share.outside
    (added / "existing.txt").write_text("existing", encoding="utf-8")
    removed = share.root.parent / "removed"
    removed.mkdir()
    (removed / "existing.txt").write_text("existing", encoding="utf-8")
    share.add_folder(removed, "write")
    start_process = asyncio.create_subprocess_shell

    async def change_shares(*args, **kwargs):
        proc = await start_process(*args, **kwargs)
        share.add_folder(added, "write")
        remove_folder(share.policy, removed.as_posix())
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_shell", change_shares)
    result = await share.runner.run_command(f"{PY} -c \"open('out/made.txt','w').write('m')\"")
    assert result["status"] == "done", result
    assert result["changed_files"] == ["out/made.txt"]


async def test_changed_files_skip_hidden(share):
    share.set_policy(exec_mode="all")
    r = await share.runner.run_command(f"{PY} -c \"open('raw/secret/s.txt','w').write('m')\"")
    assert r["changed_files"] == []


async def test_cwd_hidden_refused(share):
    share.set_policy(exec_mode="all")
    with pytest.raises(ToolError, match="not found: raw/secret"):
        await share.runner.run_command(f"{PY} -c 1", cwd="raw/secret")


async def test_cwd_is_used(share):
    share.set_policy(exec_mode="all")
    r = await share.runner.run_command(f'{PY} -c "import os;print(os.listdir())"', cwd="docs")
    assert "readme.md" in r["stdout"]


async def test_modified_script_needs_approval_when_enabled(share):
    share.set_policy(exec_confirm_modified_scripts=True, exec_allow=[f"{PY} scripts/**/*.py *"],
                     limits={"sync_wait_sec": 1})
    share.task("t1")
    share.write.write_file("scripts/new.py", "print(1)\n")
    r = await share.runner.run_command(f"{PY} scripts/new.py x")
    assert r["status"] == "waiting_approval"


async def test_modified_script_runs_when_setting_off(share):
    share.set_policy(exec_allow=[f"{PY} scripts/**/*.py *"])
    share.task("t1")
    share.write.write_file("scripts/new.py", "print(1)\n")
    assert (await share.runner.run_command(f"{PY} scripts/new.py x"))["status"] == "done"


async def test_unknown_job(share):
    with pytest.raises(ToolError, match="見つかりません"):
        await share.runner.get_command_result("nope", 1)


async def test_paused_refuses(share):
    share.set_policy(paused=True)
    with pytest.raises(ToolError, match="停止中"):
        await share.runner.run_command(f"{PY} -c 1")


async def test_lender_off_denies_while_global_all(share):
    share.set_policy(exec_mode="all", lender_exec_modes={"alice": "off"})
    share.runner.lender_fn = lambda: "alice"
    r = await share.runner.run_command(f'{PY} -c "print(1)"')
    assert r["status"] == "denied" and "実行モードが off" in r["message"]
    assert share.approvals.pending() == []


@pytest.mark.parametrize("global_mode", ["ask", "off"])
async def test_lender_all_runs_without_approval_while_global_ask(share, global_mode):
    share.set_policy(exec_mode=global_mode, lender_exec_modes={"alice": "all"}, limits={"sync_wait_sec": 1})
    share.runner.lender_fn = lambda: "alice"
    r = await share.runner.run_command(f'{PY} -c "print(1)"')
    assert r["status"] in ("done", "running")
    assert share.approvals.pending() == []
    r = await finish(share.runner, r)
    assert r["status"] == "done" and r["exit_code"] == 0 and r["stdout"].strip() == "1"


@pytest.mark.parametrize("global_mode, status", [("off", "denied"), ("ask", "waiting_approval"),
                                                 ("all", "done")])
async def test_lender_without_entry_uses_global(share, global_mode, status):
    share.set_policy(exec_mode=global_mode, lender_exec_modes={"bob": "all"}, limits={"sync_wait_sec": 1})
    share.runner.lender_fn = lambda: "alice"
    r = await share.runner.run_command(f'{PY} -c "print(1)"')
    if status == "done" and r["status"] == "running":
        r = await finish(share.runner, r)
    assert r["status"] == status
    if status == "waiting_approval":
        share.approvals.decide(share.approvals.pending()[0].id, False)
        assert (await share.runner.get_command_result(r["job_id"], 5))["status"] == "denied"


@pytest.mark.parametrize("mode, modified, status", [("allowlist", False, "done"),
                                                   ("ask", False, "waiting_approval"),
                                                   ("all", True, "waiting_approval")])
async def test_lender_modes_keep_shared_exec_checks(share, mode, modified, status):
    share.set_policy(exec_mode="off", lender_exec_modes={"alice": mode},
                     exec_allow=[f"{PY} scripts/*.py *"], exec_confirm_modified_scripts=modified,
                     limits={"sync_wait_sec": 1})
    share.runner.lender_fn = lambda: "alice"
    share.task("t1")
    share.write.write_file("scripts/new.py", "print(1)\n")
    r = await share.runner.run_command(f"{PY} scripts/new.py")
    if status == "done" and r["status"] == "running":
        r = await finish(share.runner, r)
    assert r["status"] == status
    if status == "waiting_approval":
        share.approvals.decide(share.approvals.pending()[0].id, False)
        assert (await share.runner.get_command_result(r["job_id"], 5))["status"] == "denied"
