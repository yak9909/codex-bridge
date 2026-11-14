"""Start AppServer against the scripted fake app-server."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from codex_bridge.agent.appserver import AppServer

FAKE = Path(__file__).resolve().parent / "fakes" / "fake_appserver.py"


class Fake:
    def __init__(self, tmp_path: Path, script: dict | None = None, token: str = "tok") -> None:
        self.record = tmp_path / "record.jsonl"
        script = {"record": str(self.record), **(script or {})}
        self.script_path = tmp_path / "script.json"
        self.script_path.write_text(json.dumps(script), encoding="utf-8")
        workdir = tmp_path / "workdir"
        workdir.mkdir(exist_ok=True)
        self.app = AppServer([sys.executable, "-I", str(FAKE), str(self.script_path)],
                             host_url="http://100.64.0.1:8765", token=token, workdir=workdir)

    def _lines(self) -> list[dict]:
        if not self.record.exists():
            return []
        return [json.loads(l) for l in self.record.read_text(encoding="utf-8").splitlines()]

    @property
    def argv(self) -> list[str]:
        return next(l["argv"] for l in self._lines() if "argv" in l)

    @property
    def env(self) -> dict:
        return next(l["env"] for l in self._lines() if "env" in l)

    def received(self, method: str) -> dict | None:
        for l in reversed(self._lines()):
            m = l.get("received")
            if m and m.get("method") == method:
                return m.get("params")
        return None

    def responses_to(self, request_id) -> list[dict]:
        return [l["received"] for l in self._lines()
                if "received" in l and l["received"].get("id") == request_id and "method" not in l["received"]]

    async def wait_for(self, pred, timeout: float = 5.0):
        for _ in range(int(timeout / 0.05)):
            v = pred()
            if v:
                return v
            await asyncio.sleep(0.05)
        raise AssertionError("condition not met")
