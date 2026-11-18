import asyncio
import subprocess
import sys

import pytest

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


def test_allowlist_exact_tokens():
    assert match_allow("git status", ["git status"])
    assert not match_allow("git status --porcelain", ["git status"])


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


async def test_ask_mode_waits_then_runs_after_approval(share):
    share.set_policy(exec_mode="ask", limits={"sync_wait_sec": 1})
    r = await share.runner.run_command(f'{PY} -c "print(1)"')
    assert r["status"] == "waiting_approval"
    share.approvals.decide(share.approvals.pending()[0].id, True)
    r2 = await finish(share.runner, await share.runner.get_command_result(r["job_id"], wait_sec=10))
    assert r2["status"] == "done" and r2["stdout"].strip() == "1"


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
    assert r["status"] == "done" and r["exit_code"] == 0 and r["stdout"].strip() == "1"
    assert share.approvals.pending() == []


@pytest.mark.parametrize("global_mode, status", [("off", "denied"), ("ask", "waiting_approval"),
                                                 ("all", "done")])
async def test_lender_without_entry_uses_global(share, global_mode, status):
    share.set_policy(exec_mode=global_mode, lender_exec_modes={"bob": "all"}, limits={"sync_wait_sec": 1})
    share.runner.lender_fn = lambda: "alice"
    r = await share.runner.run_command(f'{PY} -c "print(1)"')
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
    assert r["status"] == status
    if status == "waiting_approval":
        share.approvals.decide(share.approvals.pending()[0].id, False)
        assert (await share.runner.get_command_result(r["job_id"], 5))["status"] == "denied"
