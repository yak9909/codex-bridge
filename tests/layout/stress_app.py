"""A resident app full of extreme data, for the browser layout test: a real host with many lenders and tasks (one
very long one-line request, one with hundreds of lines, hundreds of timeline events, long names and paths) and a lender
side with many connections and a long request. Nothing real is touched: everything lives under the given folder."""

from __future__ import annotations

import copy
import time
from pathlib import Path
from types import SimpleNamespace as NS

import yaml

from codex_bridge.agent.connections import connection_settings
from codex_bridge.agent.pre_approval import Pending
from codex_bridge.agent.runner import AgentRunner, Current, TaskSlot
from codex_bridge.agent.sessions import SessionRegistry
from codex_bridge.agent.settings import SettingsStore
from codex_bridge.agent.ui import create_ui_app
from codex_bridge.agent.usage import QuotaGuard, UsageSnapshot, Window
from codex_bridge.agent.worklog import WorkLog
from codex_bridge.common.applog import AppLog
from codex_bridge.common.invite import Invite
from codex_bridge.host.app import build_host
from codex_bridge.host.config import load_host_config
from codex_bridge.host.main import ensure_config
from codex_bridge.resident import Resident

LONG_LINE = "web のテストが落ちているので直してください。" + "parser の空行と末尾の改行の扱いを調べ、回帰テストを追加してください。" * 60
MANY_LINES = "\n".join(f"{i + 1}. src/module_{i}/file_{i}.ts の型を確かめ、必要なら直してください。" for i in range(200))
LONG_NAME = "very-long-lender-name-" * 3
LONG_PATH = "D:/work/" + "/".join(f"nested-folder-{i}" for i in range(25)) + "/report.md"
LONG_RULES = {
    "deny": [f"git push origin {LONG_PATH}/blocked-{i}" for i in range(28)],
    "ask": [f"npm run {LONG_PATH}/review-{i} *" for i in range(28)],
    "allow": [f"python {LONG_PATH}/check-{i}.py *" for i in range(28)],
}
VIEW = {"features": {"web_search": False, "image_generation": True, "multi_agent": False, "goals": True},
        "pre_approval": {"new_task": True, "followup": True, "timeout_min": 30}, "resumable": True, "over_limit": "report",
        "max_effort": "xhigh", "max_parallel": 3,
        "models_all": [{"model": f"GPT-6.{i}-Model-With-A-Long-Name", "allowed": i % 3 != 0, "is_default": i == 1} for i in range(14)]}
MODELS = [{"model": f"GPT-6.{i}-Model-With-A-Long-Name", "efforts": ["low", "high", "xhigh"], "is_default": i == 1}
          for i in range(14) if i % 3 != 0]


class _Link:
    """One connected lender, as the host sees it."""

    def __init__(self, host, name: str) -> None:
        self.host, self.seq = host, 0
        self.link = host.mgr.attach(name, self._send)

    async def _send(self, message) -> None:
        pass

    async def push(self, message_type: str, **fields) -> None:
        if message_type in ("event", "task_state"):
            self.seq += 1
            fields.setdefault("seq", self.seq)
        await self.host.mgr.on_agent_message(self.link, {"type": message_type, **fields})


async def _fill_host(host, now: float) -> None:
    names = ["yamada", "suzuki", LONG_NAME] + [f"lender-{i:02d}" for i in range(30)]
    for name in names:
        host.tokens.issue(name)
    links = {}
    for name in names[:3]:
        links[name] = link = _Link(host, name)
        await link.push("hello", agent_version="0.3.0", codex_version="x", auth_mode="chatgpt", protocol=5,
                        capabilities={"resumable": True, "pre_approval": True, "model_select": True, "max_parallel": 3}, models=MODELS,
                        max_effort="xhigh", lender_view=VIEW, limits={"five_hour": 80, "weekly": 60, "force": False},
                        current_task_id=None)
        await link.push("usage", five_hour={"used_percent": 45, "resets_at": now + 3600},
                        weekly={"used_percent": 45, "resets_at": now + 86400 * 2}, ts=now)
    host.policy.update(lender_exec_modes={"yamada": "all"},
                       **{f"exec_{key}": value for key, value in LONG_RULES.items()})
    host.mgr.check_start = None
    s = links["suzuki"]
    for i in range(60):  # a long list of finished requests
        t = await host.mgr.start(f"古い依頼 {i}: " + "長い題の依頼です。" * 8, lender="suzuki")
        await s.push("task_state", task_id=t, state="completed", report="終わりました。")
    t = await host.mgr.start(MANY_LINES, lender="suzuki")
    await s.push("task_state", task_id=t, state="completed", report="終わりました。")
    y = links["yamada"]
    t = await host.mgr.start(LONG_LINE, lender="yamada", model=MODELS[0]["model"], effort="high",
                             exec_mode="ask", deny=LONG_RULES["deny"], ask=LONG_RULES["ask"])
    await y.push("event", task_id=t, kind="started", data={})
    for i in range(150):  # hundreds of timeline lines
        await y.push("event", task_id=t, kind="agent_message", data={"text": f"手順 {i}: " + "確かめています。" * 6})
        await y.push("event", task_id=t, kind="tool_finished",
                     data={"tool": "run_command", "arguments": {"command": f"npm test -- {LONG_PATH}"}, "success": True})
    await y.push("task_state", task_id=t, state="running")
    second = await host.mgr.start("並行して進める別の依頼", lender="yamada")
    await y.push("task_state", task_id=second, thread_id="parallel-thread", state="running")
    host.approvals.request("command", "npm test -- --runInBand " + LONG_PATH, t)
    host.approvals.request("delete", LONG_PATH, t)
    # Populate tasks before displaying the over-limit state: real starts enforce quotas.
    for link in links.values():
        await link.push("usage", five_hour={"used_percent": 45, "resets_at": now + 3600},
                        weekly={"used_percent": 92, "resets_at": now + 86400 * 2}, ts=now)


def _lender_side(root: Path, now: float):
    shared = SettingsStore(root / "settings.json", shared=True)
    shared.update({"limits": {"five_hour": 80, "weekly": None}, "workdir": str(root / "work")})

    class App:
        workdir = root / "work"

    class Connections:
        paused = moving = False

        def __init__(self) -> None:
            self.runners, self.clients, self.saved, self.slot = {}, {}, {}, TaskSlot()
            self.store = NS(_read=lambda id: NS(settings=copy.deepcopy(self.saved[id])))
            self.catalog = [{"model": m["model"], "is_default": m["is_default"], "efforts": m["efforts"]} for m in MODELS]

        def create(self, id: str, host: str, connected: bool = True):
            async def send(message):
                pass
            runner = AgentRunner(App(), QuotaGuard({}), SessionRegistry(root / id / "sessions.json"), send,
                                 worklog=WorkLog(root / id / "tasks"), slot=self.slot, connection_id=id, host_name=host)
            self.saved[id] = connection_settings({"pre_approval": {"new_task": True}, "log_visibility": "all"})
            runner.apply_settings({**shared.get(), **self.saved[id]})
            runner.log_visibility = "all"
            runner.last_usage = UsageSnapshot(Window(45, now + 3600), Window(62, now + 3 * 86400))
            self.runners[id] = runner
            self.clients[id] = NS(ws=object() if connected else None, invite_problem=False, paused=False,
                                  invite=Invite("http://" + host, "fake-secret", "stress"))
            return runner

        def rows(self):
            return [{"id": id, "host_name": r.host_name, "connected": self.clients[id].ws is not None,
                     "invite_problem": False, "busy": r.active_task_id() is not None} for id, r in self.runners.items()]

        async def update_settings(self, id, patch):
            self.saved[id] = connection_settings({**self.saved[id], **patch}, strict=True)
            return copy.deepcopy(self.saved[id])

    connections = Connections()
    first = connections.create("a" * 12, "100.64.0.1:8765")
    for i in range(12):
        connections.create(f"{i:012d}", f"100.64.{i}.{i + 10}:8765", connected=i % 2 == 0)
    log = WorkLog(first.worklog.dir, clock=lambda: now - 300)
    log.add("t1", "task_prompt", {"text": LONG_LINE}, **first.connection_fields())
    log.add("t1", "state", {"state": "running"}, **first.connection_fields())
    for i in range(150):
        log.add("t1", "agent_message", {"text": f"手順 {i}: " + "確かめています。" * 6})
        log.add("t1", "tool_finished", {"tool": "run_command", "arguments": {"command": f"npm test -- {LONG_PATH}"}, "success": True})
    log2 = WorkLog(first.worklog.dir, clock=lambda: now - 900)
    log2.add("t2", "task_prompt", {"text": MANY_LINES}, **first.connection_fields())
    log2.add("t2", "state", {"state": "completed"}, **first.connection_fields())
    first.cur = Current("t1", "thread1", model=MODELS[0]["model"], effort="high")
    first.approval.pending = Pending({"task_id": "t1", "text": LONG_LINE}, "followup", now - 180, None, 30)
    first.currents["t3"] = Current("t3", "thread3")
    log.add("t3", "task_prompt", {"text": "同時に進める別の依頼"}, **first.connection_fields())
    log.add("t3", "state", {"state": "running"}, **first.connection_fields())
    log.add("t4", "task_prompt", {"text": MANY_LINES}, **first.connection_fields())
    log.add("t4", "state", {"state": "awaiting_lender"}, **first.connection_fields())
    first.approval.pendings["t4"] = Pending({"task_id": "t4", "prompt": MANY_LINES}, "start", now - 60, None, 30)

    async def apply(saved):
        pass

    async def move(path):
        return []

    class Agent:
        paused = False
        on_moved = None
        error = None

        def counts(self):
            return (sum(c["connected"] for c in connections.rows()), len(connections.runners), sum(len(r.pending_approvals) for r in connections.runners.values()))

        async def set_paused(self, p):
            self.paused = p

        def ui_app(self):
            return create_ui_app(None, shared, None, versions={"agent_version": "0.3.0", "codex_version": "stress"},
                                 data_root=root, on_settings=apply, on_move=move, connections=connections)

    return Agent()


async def build(folder: Path, port: int) -> Resident:
    """The resident state for create_resident_app; call inside the loop that will serve it."""
    folder = Path(folder)
    share = folder / "share"
    share.mkdir(parents=True, exist_ok=True)
    cfg = folder / "host.yaml"
    cfg.write_text(yaml.safe_dump({"share_root": str(share), "data_dir": str(folder / "data"),
                                   "public_bind": "127.0.0.1:1", "local_port": 1, "rg_path": str(folder / "rg.exe")}),
                   encoding="utf-8")
    ensure_config(cfg)
    host = build_host(load_host_config(cfg), config_path=cfg)
    now = time.time()
    await _fill_host(host, now)
    role = NS(host=host, error=None, running=True, counts=lambda: (3, 33, 2))

    async def nothing():
        pass
    role.start = role.stop = nothing
    (folder / "agent").mkdir(exist_ok=True)
    applog = AppLog(folder / "agent" / "logs" / "app.jsonl")
    for i in range(300):
        applog.add("use" if i % 2 else "lend", "warn" if i % 3 else "info", f"記録 {i}: {LONG_PATH}")
    return Resident(applog=applog, agent=_lender_side(folder / "agent", now),
                    host_role=role, config_path=cfg, port=port,
                    tray_factory=lambda r: NS(notify=lambda *a: None, refresh=lambda: None))
