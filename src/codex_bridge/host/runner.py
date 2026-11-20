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
from .command_rules import inspect_commands, lex, normalized_path, program_name
from .paths import UNSET_SHARE_MSG, SafePaths, ToolError, not_found
from .policy import OUTPUTS_TOP, Policy, PolicyStore, glob_to_regex, normalize_rel
from .snapshots import Snapshots
from .tasks import TaskRecord

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


def _matching_rule(commands: list[list[str | None]], patterns: list[str]) -> str | None:
    for pattern in patterns:
        segments, uncertain = lex(pattern, "powershell", pattern=True)
        if uncertain or not segments:
            continue
        tokens = [word.value for word in segments[0]]
        rest_any = tokens[-1] == "*"
        fixed = tokens[:-1] if rest_any else tokens
        for command in commands:
            if len(command) < len(fixed) or (not rest_any and len(command) != len(fixed)):
                continue
            pairs = zip(fixed, command)
            if all(t is not None and _rule_token_match(p, t, i == 0) for i, (p, t) in enumerate(pairs)):
                return pattern
    return None


def _rule_token_match(pattern: str, token: str, program: bool) -> bool:
    normalize = program_name if program else normalized_path
    pat, value = normalize(pattern), normalize(token)
    if any(c in pat for c in "*?["):
        return glob_to_regex(pat).fullmatch(normalize_rel(value)) is not None
    return pat.casefold() == value.casefold()


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
    thread_id: str | None = None
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
                 lender_fn: Callable[[], str | None] = lambda: None,
                 thread_fn: Callable[[], str | None] = lambda: None,
                 task_record_fn: Callable[[str], TaskRecord | None] = lambda _: None) -> None:
        self.paths = paths
        self.policy = policy
        self.approvals = approvals
        self.snapshots = snapshots
        self.audit = audit
        self.task_id_fn = task_id_fn
        self.task_record_fn = task_record_fn
        self.lender_fn = lender_fn  # invite name of the lender making the current call (None: not known)
        self.thread_fn = thread_fn
        self.jobs: dict[str, Job] = {}
        self._ids = itertools.count(1)
        self._tasks: set[asyncio.Task] = set()

    def _command_decision(self, job: Job, policy: Policy, *, approved: bool = False) -> tuple[str | None, bool]:
        task = self.task_record_fn(job.task_id) if job.task_id is not None else None
        mode = policy.effective_exec_mode(job.lender, task.exec_mode if task else None)
        if mode == "off":
            return "実行は許可されていません（実行モードが off です）", False
        deny = [*policy.deny_patterns, *(task.deny if task else [])]
        ask = [] if approved else [*policy.ask_patterns, *(task.ask if task else [])]
        commands, uncertain = inspect_commands(job.command)
        if (rule := _matching_rule(commands, deny)) is not None:
            return f"コマンドの規則の拒否リストに当たるため実行しません: {rule}", False
        if approved:
            return None, False
        if uncertain or _matching_rule(commands, ask) is not None:
            return None, True
        if policy.confirm_modified_scripts:
            touched = {normalized_path(path).casefold() for path in self.snapshots.touched(job.task_id)}
            if any(normalized_path(token).casefold() in touched for words in commands for token in words
                   if token is not None):
                return None, True
        if mode == "allowlist":
            return None, not match_allow(job.command, policy.allow_patterns)
        return None, mode == "ask"

    async def run_command(self, command: str, cwd: str = ".", timeout_sec: int | None = None) -> dict:
        self.paths.check_running()
        p = self.policy.current()
        real_cwd = self.paths.resolve(cwd, "read")
        if not real_cwd.is_dir():
            raise not_found(cwd)
        task_id = self.task_id_fn()
        job = Job(id=f"j{next(self._ids)}", command=command, cwd=normalize_rel(cwd), task_id=task_id,
                  lender=self.lender_fn(), thread_id=self.thread_fn())
        self.jobs[job.id] = job
        denial, needs_approval = self._command_decision(job, p)
        if denial is not None:
            job.status, job.message = "denied", denial
            job.done.set()
            self.audit.write("command", job_id=job.id, command=command, status="denied", task_id=task_id)
            return job.snapshot()
        timeout = min(int(timeout_sec or p.limits.exec_timeout_sec), p.limits.exec_timeout_sec)
        if needs_approval:
            self._request_approval(job)
        t = asyncio.create_task(self._run_job(job, real_cwd, timeout))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return await self._wait(job, p.limits.sync_wait_sec)

    async def get_command_result(self, job_id: str, wait_sec: int = 30) -> dict:
        job = self.jobs.get(job_id)
        # Another lender's job gets the same answer as a job that does not exist.
        if (job is None or job.lender != self.lender_fn()
                or (job.lender is not None and (job.thread_id != self.thread_fn() if job.thread_id
                    else job.task_id != self.task_id_fn()))):
            raise ToolError(f"ジョブが見つかりません: {job_id}")
        return await self._wait(job, min(wait_sec, self.policy.current().limits.sync_wait_sec))

    async def _wait(self, job: Job, seconds: float) -> dict:
        try:
            await asyncio.wait_for(asyncio.shield(job.done.wait()), timeout=seconds)
        except asyncio.TimeoutError:
            pass
        return job.snapshot()

    def _request_approval(self, job: Job) -> None:
        approval = self.approvals.request("command", job.command, job.task_id, cwd=job.cwd or ".")
        job.approval_id = approval.id
        job.status = "waiting_approval"
        job.message = "管理者の承認待ちです。get_command_result で結果を確かめてください。"

    def _check_before_execution(self, job: Job) -> bool:
        self.paths.check_running()
        denial, needs_approval = self._command_decision(job, self.policy.current(), approved=job.approval_id is not None)
        if denial is not None:
            raise ToolError(denial)
        return needs_approval

    async def _await_approval(self, job: Job) -> None:
        approval_id = job.approval_id
        assert approval_id is not None
        status = await self.approvals.wait(approval_id, timeout=float("inf"))
        self.approvals.consume(approval_id)
        if status != "approved":
            raise ToolError("実行は拒否されました" if status == "denied" else "承認の期限が切れたため実行しませんでした")
        self._check_before_execution(job)

    async def _run_job(self, job: Job, real_cwd: Path, timeout: int) -> None:
        try:
            if job.approval_id:
                await self._await_approval(job)
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

    def _write_area_state(self, roots: list[Path] | None = None,
                          policy: Policy | None = None,
                          display: Callable[[str], str] | None = None) -> dict[str, tuple[int, int]]:
        """(mtime, size) of writable files under the selected roots."""
        state: dict[str, tuple[int, int]] = {}
        if roots is None:
            roots = self.paths.root_paths()
        if display is None:
            display = self.paths.display_formatter()
        walked: list[Path] = []
        for root in sorted(roots, key=lambda path: len(path.parts)):
            if any(root.is_relative_to(parent) for parent in walked):
                continue
            walked.append(root)
            for _, entry in self.paths.walk(root, "write", policy=policy):
                try:
                    st = entry.stat()
                except OSError:
                    continue
                state[display(entry.path)] = (st.st_mtime_ns, st.st_size)
        return state

    async def _execute(self, job: Job, real_cwd: Path, timeout: int) -> None:
        while True:
            policy = self.policy.current()
            roots = [Path(folder["path"]) for folder in self.paths.roots()]
            display = self.paths.display_formatter()
            root = self.paths.root
            if root is None:
                raise ToolError(UNSET_SHARE_MSG)
            before = await asyncio.to_thread(self._write_area_state, roots, policy, display)
            # Rules or shares can change while scanning. If a new approval is needed, scan again after it.
            if not self._check_before_execution(job):
                break
            self._request_approval(job)
            await self._await_approval(job)
            job.status, job.message = "running", ""
        limits = policy.limits
        self.paths.resolve(real_cwd.as_posix(), "read")
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
        active_roots = [folder for folder in roots if self.paths.level_of(folder) not in (None, "hidden")]
        after = await asyncio.to_thread(self._write_area_state, active_roots, policy, display)

        def still_writable(name: str) -> bool:
            path = root / name
            return self.paths.level_of(path) == self.paths.level_of(path.resolve()) == "write"

        job.changed_files = sorted(k for k, v in after.items() if before.get(k) != v and still_writable(k)) + \
            sorted(f"{k}（削除）" for k in before if k not in after and still_writable(k))
