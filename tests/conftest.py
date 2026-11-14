"""Shared fixtures: a sample share root with a policy."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from codex_bridge.common.audit import AuditLog
from codex_bridge.host.fs_read import ReadTools
from codex_bridge.host.paths import SafePaths
from codex_bridge.host.policy import PolicyStore
from codex_bridge.host.search import SearchTools
from codex_bridge.host.snapshots import Snapshots
from codex_bridge.host.fs_write import WriteTools
from codex_bridge.host.approvals import Approvals
from codex_bridge.host.runner import CommandRunner

RG_PATH = Path(__file__).resolve().parents[1] / "vendor" / "rg.exe"

POLICY = """
default: read
rules:
  - {path: "raw/**", level: write}
  - {path: "raw/secret/**", level: hidden}
  - {path: "out", level: write}
  - {path: "scripts", level: write}
exec: {mode: allowlist, allow: ["python scripts/**/*.py *"]}
"""

ORIGINAL_A = "line1\nline2\nthe needle here\nline4\n"


def raises_msg(fn) -> str:
    from codex_bridge.host.paths import ToolError
    with pytest.raises(ToolError) as ei:
        fn()
    return str(ei.value)


def make_junction(link: Path, target: Path) -> None:
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                   check=True, capture_output=True)


@dataclass
class Share:
    root: Path
    outside: Path
    data_dir: Path
    policy: PolicyStore
    audit: AuditLog
    paths: SafePaths
    read: ReadTools
    task_id: str | None = None
    extras: dict = field(default_factory=dict)

    def set_policy(self, **changes) -> None:
        self.policy.update(**changes)

    def task(self, task_id: str | None) -> None:
        self.task_id = task_id

    def approve_all(self) -> None:
        self.decision = "approved"

    def deny_all(self) -> None:
        self.decision = "denied"


def build_share(tmp_path: Path) -> Share:
    root = tmp_path / "share"
    outside = tmp_path / "outside"
    data_dir = tmp_path / "data"
    for d in (root / "raw" / "secret", root / "docs", root / "out", root / "scripts", outside, data_dir):
        d.mkdir(parents=True, exist_ok=True)
    (root / "raw" / "a.txt").write_text(ORIGINAL_A, encoding="utf-8", newline="\n")
    (root / "raw" / "b.txt").write_text("bee\n", encoding="utf-8")
    (root / "raw" / "secret" / "k.txt").write_text("the needle is secret\n", encoding="utf-8")
    (root / "raw" / "big.txt").write_text("".join(f"row {i}\n" for i in range(100_000)), encoding="utf-8",
                                          newline="\n")
    (root / "raw" / "sjis.txt").write_bytes("日本語のテキスト\n".encode("cp932"))
    (root / "raw" / "data.bin").write_bytes(bytes(range(256)) * 4)
    (root / "docs" / "readme.md").write_text("# readme\n", encoding="utf-8")
    (outside / "outside.txt").write_text("outside\n", encoding="utf-8")
    (data_dir / "policy.yaml").write_text(POLICY, encoding="utf-8")
    policy = PolicyStore(data_dir / "policy.yaml")
    audit = AuditLog(data_dir / "audit.jsonl")
    paths = SafePaths(root, policy)
    share = Share(root=root, outside=outside, data_dir=data_dir, policy=policy, audit=audit,
                  paths=paths, read=ReadTools(paths, policy))
    share.search = SearchTools(paths, policy, RG_PATH)
    share.snapshots = Snapshots(data_dir)
    share.decision = "approved"

    async def approve_fn(kind: str, summary: str) -> str:
        return share.decision

    share.write = WriteTools(paths, policy, share.snapshots, audit, lambda: share.task_id, approve_fn)
    share.approvals = Approvals(policy)
    share.runner = CommandRunner(paths, policy, share.approvals, share.snapshots, audit, lambda: share.task_id)
    return share


@pytest.fixture
def share(tmp_path) -> Share:
    return build_share(tmp_path)
