"""run_command / get_command_result: approvals, async jobs, limits."""

from __future__ import annotations

import asyncio
import itertools
import secrets
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..common.audit import AuditLog
from .approvals import Approvals
from .paths import UNSET_SHARE_MSG, SafePaths, ToolError, not_found
from .policy import OUTPUTS_TOP, PolicyStore, glob_to_regex, normalize_rel
from .snapshots import Snapshots

FORBIDDEN_CHARS = set("&|;<>^\r\n`")
CREATIONFLAGS = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _tokens(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=False)
    except ValueError:
        return []


def _token_match(pat: str, tok: str) -> bool:
    if any(c in pat for c in "*?["):
        if ".." in tok.replace("\\", "/").split("/"):
            return False
        return glob_to_regex(pat).fullmatch(normalize_rel(tok)) is not None
    return pat.replace("\\", "/").lower() == tok.replace("\\", "/").lower()


def match_allow(command: str, patterns: list[str]) -> bool:
    if any(c in FORBIDDEN_CHARS for c in command):
        return False
    toks = _tokens(command)
    if not toks:
        return False
    for pattern in patterns:
        ptoks = _tokens(pattern)
        rest_any = bool(ptoks) and ptoks[-1] == "*"
        fixed = ptoks[:-1] if rest_any else ptoks
        if len(toks) < len(fixed) or (not rest_any and len(toks) != len(fixed)):
            continue
        if all(_token_match(p, t) for p, t in zip(fixed, toks)):
            return True
    return False


def decode_output(b: bytes) -> str:
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("cp932", errors="replace")


@dataclass
class Job:
    id: str
    command: str
    cwd: str
    task_id: str | None
    lender: str | None = None  # who started it; only that lender can read the result
    status: str = "starting"
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    output_file: str | None = None
    changed_files: list[str] = field(default_factory=list)
    message: str = ""
    approval_id: str | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)

    def snapshot(self) -> dict:
        d = {"status": self.status, "job_id": self.id, "exit_code": self.exit_code,
             "stdout": self.stdout, "stderr": self.stderr, "output_file": self.output_file,
             "changed_files": self.changed_files}
        if self.message:
            d["message"] = self.message
        return d


class CommandRunner:
    def __init__(self, paths: SafePaths, policy: PolicyStore, approvals: Approvals,
                 snapshots: Snapshots, audit: AuditLog, task_id_fn: Callable[[], str | None],
                 lender_fn: Callable[[], str | None] = lambda: None) -> None:
        self.paths = paths
        self.policy = policy
        self.approvals = approvals
        self.snapshots = snapshots
        self.audit = audit
        self.task_id_fn = task_id_fn
        self.lender_fn = lender_fn  # invite name of the lender making the current call (None: not known)
        self.jobs: dict[str, Job] = {}
        self._ids = itertools.count(1)
        self._tasks: set[asyncio.Task] = set()

    def _needs_approval(self, command: str, task_id: str | None) -> bool:
        p = self.policy.current()
        if p.exec_mode == "ask":
            return True
        if p.exec_mode == "allowlist" and not match_allow(command, p.allow_patterns):
            return True
        if p.confirm_modified_scripts:
            touched = {t.lower() for t in self.snapshots.touched(task_id)}
            for tok in _tokens(command):
                if normalize_rel(tok.strip('"')).lower() in touched:
                    return True
        return False

    async def run_command(self, command: str, cwd: str = ".", timeout_sec: int | None = None) -> dict:
        self.paths.check_running()
        p = self.policy.current()
        real_cwd = self.paths.resolve(cwd, "read")
        if not real_cwd.is_dir():
            raise not_found(cwd)
        task_id = self.task_id_fn()
        job = Job(id=f"j{next(self._ids)}", command=command, cwd=normalize_rel(cwd), task_id=task_id,
                  lender=self.lender_fn())
        self.jobs[job.id] = job
        if p.exec_mode == "off":
            job.status, job.message = "denied", "実行は許可されていません（実行モードが off です）"
            job.done.set()
            self.audit.write("command", job_id=job.id, command=command, status="denied", task_id=task_id)
            return job.snapshot()
        timeout = min(int(timeout_sec or p.limits.exec_timeout_sec), p.limits.exec_timeout_sec)
        if self._needs_approval(command, task_id):
            a = self.approvals.request("command", command, task_id)
            job.approval_id = a.id
            job.status = "waiting_approval"
            job.message = "管理者の承認待ちです。get_command_result で結果を確かめてください。"
        t = asyncio.create_task(self._run_job(job, real_cwd, timeout))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return await self._wait(job, p.limits.sync_wait_sec)

    async def get_command_result(self, job_id: str, wait_sec: int = 30) -> dict:
        job = self.jobs.get(job_id)
        # Another lender's job gets the same answer as a job that does not exist.
        if job is None or job.lender != self.lender_fn():
            raise ToolError(f"ジョブが見つかりません: {job_id}")
        return await self._wait(job, min(wait_sec, self.policy.current().limits.sync_wait_sec))

    async def _wait(self, job: Job, seconds: float) -> dict:
        try:
            await asyncio.wait_for(asyncio.shield(job.done.wait()), timeout=seconds)
        except asyncio.TimeoutError:
            pass
        return job.snapshot()

    async def _run_job(self, job: Job, real_cwd: Path, timeout: int) -> None:
        try:
            if job.approval_id:
                status = await self.approvals.wait(job.approval_id, timeout=float("inf"))
                self.approvals.consume(job.approval_id)
                if status != "approved":
                    job.status = "denied"
                    job.message = "実行は拒否されました" if status == "denied" else "承認の期限が切れたため実行しませんでした"
                    return
                # The policy or folder may have changed while waiting.
                self.paths.check_running()
            job.status, job.message = "running", ""
            await self._execute(job, real_cwd, timeout)
        except ToolError as e:
            job.status, job.message = "denied", str(e)
        except Exception as e:  # keep the job record useful
            job.status, job.message = "error", f"実行に失敗しました: {type(e).__name__}: {e}"
        finally:
            job.done.set()
            self.audit.write("command", job_id=job.id, command=job.command, cwd=job.cwd, status=job.status,
                             exit_code=job.exit_code, changed_files=job.changed_files, task_id=job.task_id)

    def _write_area_state(self) -> dict[str, tuple[int, int]]:
        """(mtime, size) of every writable file in every shared folder."""
        state: dict[str, tuple[int, int]] = {}
        display = self.paths.display_formatter()
        for root in self.paths.root_paths():
            for _, entry in self.paths.walk(root, "write"):
                try:
                    st = entry.stat()
                except OSError:
                    continue
                state[display(entry.path)] = (st.st_mtime_ns, st.st_size)
        return state

    async def _execute(self, job: Job, real_cwd: Path, timeout: int) -> None:
        limits = self.policy.current().limits
        before = await asyncio.to_thread(self._write_area_state)
        root = self.paths.root  # read once: the main folder can vanish while a command starts
        if root is None:
            raise ToolError(UNSET_SHARE_MSG)
        out_dir = root / OUTPUTS_TOP / "outputs"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{secrets.token_hex(16)}.stdout.txt"
        proc = await asyncio.create_subprocess_shell(
            job.command, cwd=str(real_cwd), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, creationflags=CREATIONFLAGS)
        cap = limits.output_max_bytes
        head = bytearray()
        total = 0
        err = bytearray()

        async def pump_out() -> None:
            nonlocal total
            assert proc.stdout is not None
            with open(out_path, "wb") as f:
                while chunk := await proc.stdout.read(65536):
                    f.write(chunk)
                    total += len(chunk)
                    if len(head) < cap:
                        head.extend(chunk[: cap - len(head)])

        async def pump_err() -> None:
            assert proc.stderr is not None
            while chunk := await proc.stderr.read(65536):
                if len(err) < cap:
                    err.extend(chunk[: cap - len(err)])

        pumps = asyncio.gather(pump_out(), pump_err(), proc.wait())
        try:
            await asyncio.wait_for(asyncio.shield(pumps), timeout=timeout)
            job.status = "done"
        except asyncio.TimeoutError:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            await pumps
            job.status = "timeout"
            job.message = f"時間の上限（{timeout} 秒）を超えたため止めました"
        job.exit_code = proc.returncode
        if total > cap:
            keep = cap - 1024
            job.stdout = decode_output(bytes(head[:keep])) + f"\n…（出力が {total} バイトあるため省略。全体は output_file を read_file で読んでください）"
            job.output_file = self.paths.to_rel(out_path)
        else:
            job.stdout = decode_output(bytes(head))
            out_path.unlink(missing_ok=True)
        job.stderr = decode_output(bytes(err))
        after = await asyncio.to_thread(self._write_area_state)
        job.changed_files = sorted(k for k, v in after.items() if before.get(k) != v) + \
            sorted(f"{k}（削除）" for k in before if k not in after)
