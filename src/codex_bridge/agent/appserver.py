"""Client for `codex app-server` (JSON-RPC over stdio, one JSON object per line)."""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import re
import subprocess
from pathlib import Path

from .. import __version__
from .settings import defaults, feature_args
from .usage import UsageSnapshot, snapshot_from_rate_limits

MCP_NAME = "bridge"
TOKEN_ENV = "CODEX_BRIDGE_TOKEN"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class AppServerError(Exception):
    pass


# Built-in features that would let Codex act on the friend's PC (commands, screen, browser, local images)
# or bring in the friend's own add-ons (apps/connectors, plugins, hooks). Verified against codex-cli 0.167.0.
# Do NOT add code_mode_host: Codex routes MCP tool calls through it (disabling it broke every bridge call).
ISOLATED_FEATURES = (
    "shell_tool", "unified_exec", "unified_exec_tty", "computer_use", "browser_use", "browser_use_external",
    "apps", "plugins", "remote_plugin", "hooks", "view_image", "skill_mcp_dependency_install",
)
_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


def _toml_key(name: str) -> str:
    return name if _BARE_KEY.fullmatch(name) else json.dumps(name)


def launch_args(host_url: str, disabled_mcp: list[str] | tuple = (), features: dict | None = None) -> list[str]:
    """app-server arguments: only the bridge MCP server, none of the friend's tools or add-ons.

    disabled_mcp must list servers that really exist in the friend's config.toml: naming an unknown
    server creates an empty entry and makes app-server fail to start. features: which of the lender's
    restrictable features are allowed (True); None stops all of them (design 4.4)."""
    args = ["app-server"]
    for feature in ISOLATED_FEATURES:
        args += ["--disable", feature]
    args += feature_args(features)
    for name in disabled_mcp:
        if name != MCP_NAME:
            args += ["-c", f"mcp_servers.{_toml_key(name)}.enabled=false"]
    return args + [
        "-c", f'mcp_servers.{MCP_NAME}.url="{host_url.rstrip("/")}/codex/mcp"',
        "-c", f'mcp_servers.{MCP_NAME}.bearer_token_env_var="{TOKEN_ENV}"',
        "-c", f"mcp_servers.{MCP_NAME}.tool_timeout_sec=120",
        # The host approves risky actions itself; Codex must not wait for its own user.
        "-c", f'mcp_servers.{MCP_NAME}.default_tools_approval_mode="approve"',
    ]


def codex_command(path: Path) -> list[str]:
    """Command prefix that runs codex at path (.cmd/.bat wrappers go through cmd.exe)."""
    path = Path(path)
    if path.suffix.lower() in (".cmd", ".bat"):
        return ["cmd.exe", "/d", "/c", str(path)]
    return [str(path)]


def _text(text: str) -> list[dict]:
    return [{"type": "text", "text": text}]


class AppServer:
    def __init__(self, codex_cmd: list[str], host_url: str, token: str, workdir: Path,
                 disabled_mcp: list[str] | tuple = (), features: dict | None = None) -> None:
        self.codex_cmd = list(codex_cmd)
        self.disabled_mcp = list(disabled_mcp)
        self.host_url = host_url
        self.token = token
        self.workdir = Path(workdir)
        self.features = {**defaults()["features"], **(features or {})}  # True = allowed; see settings.py
        self.events: asyncio.Queue[dict] = asyncio.Queue()
        self.proc: asyncio.subprocess.Process | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._reader: asyncio.Task | None = None
        self._write_lock = asyncio.Lock()
        self._lifecycle = asyncio.Lock()  # start / restart never overlap (the watchdog also starts it)
        self._planned_exit = False

    async def start(self) -> None:
        async with self._lifecycle:
            if not self.running:
                await self._start()

    async def restart(self, features: dict | None = None, workdir: Path | None = None) -> None:
        """Stop and start again, with new features / work folder. The planned exit is not reported as a crash."""
        async with self._lifecycle:
            if features is not None:
                self.features = {**defaults()["features"], **features}
            if workdir is not None:
                self.workdir = Path(workdir)
            self._planned_exit = True
            try:
                await self._stop()
            finally:
                self._planned_exit = False
            await self._start()

    async def _start(self) -> None:
        env = {**os.environ, TOKEN_ENV: self.token}
        self.proc = await asyncio.create_subprocess_exec(
            *self.codex_cmd, *launch_args(self.host_url, self.disabled_mcp, self.features), cwd=str(self.workdir),
            env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            limit=64 * 1024 * 1024, creationflags=CREATE_NO_WINDOW)
        self._reader = asyncio.create_task(self._read_loop())
        await self.request("initialize", {"clientInfo": {"name": "codex-bridge-agent", "version": __version__},
                                          "capabilities": {"experimentalApi": False}})
        await self._write({"method": "initialized"})

    async def stop(self) -> None:
        async with self._lifecycle:
            await self._stop()

    async def _stop(self) -> None:
        if self.proc and self.proc.returncode is None:
            # EOF also shuts down the RPC server if Windows refuses taskkill (or the executable is a
            # launcher whose child owns the pipes). Do not copy its data while that child still runs.
            if self.proc.stdin:
                self.proc.stdin.close()
            # Kill the whole tree: codex may be a .cmd wrapper around node.
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)], capture_output=True,
                           creationflags=CREATE_NO_WINDOW)
            try:
                await asyncio.wait_for(self.proc.wait(), 10)
            except asyncio.TimeoutError:
                self.proc.kill()
                await self.proc.wait()
        if self._reader:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)  # its "exited" report is settled now

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def _write(self, obj: dict) -> None:
        assert self.proc and self.proc.stdin
        async with self._write_lock:
            self.proc.stdin.write((json.dumps({"jsonrpc": "2.0", **obj}, ensure_ascii=False) + "\n").encode("utf-8"))
            await self.proc.stdin.drain()

    async def request(self, method: str, params: dict | None = None, timeout: float = 120.0):
        rid = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._write({"id": rid, "method": method, "params": params or {}})
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(rid, None)

    async def _read_loop(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            while line := await self.proc.stdout.readline():
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "method" in msg and "id" in msg:
                    await self._answer_server_request(msg)
                elif "method" in msg:
                    await self.events.put(msg)
                elif msg.get("id") in self._pending:
                    fut = self._pending[msg["id"]]
                    if not fut.done():
                        if "error" in msg:
                            fut.set_exception(AppServerError(msg["error"].get("message", str(msg["error"]))))
                        else:
                            fut.set_result(msg.get("result"))
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(AppServerError("app-server が終了しました"))
            if not self._planned_exit:
                await self.events.put({"method": "_exited", "params": {}})

    async def _answer_server_request(self, msg: dict) -> None:
        method, params = msg["method"], msg.get("params") or {}
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            result = {"decision": "decline"}
        elif method in ("execCommandApproval", "applyPatchApproval"):
            result = {"decision": "denied"}
        elif method == "mcpServer/elicitation/request":
            ok = params.get("serverName") == MCP_NAME
            result = {"action": "accept" if ok else "decline", "content": {} if ok else None}
        elif method == "item/tool/requestUserInput":
            result = {"answers": {}}
        else:
            await self._write({"id": msg["id"], "error": {"code": -32601, "message": f"unsupported: {method}"}})
            return
        await self._write({"id": msg["id"], "result": result})

    # ---- typed helpers -------------------------------------------------
    async def model_list(self) -> list[dict]:
        entries = []
        params = {}
        while True:
            r = await self.request("model/list", params)
            entries.extend(r["data"])
            cursor = r.get("nextCursor")
            if cursor is None:
                return entries
            params = {"cursor": cursor}

    async def thread_start(self, developer_instructions: str, ephemeral: bool = False,
                           model: str | None = None) -> dict:
        params = {"cwd": str(self.workdir), "sandbox": "read-only", "approvalPolicy": "never",
                  "developerInstructions": developer_instructions, "serviceName": "codex-bridge"}
        if ephemeral:  # disposable session (design 4.6): no conversation record is written under ~/.codex
            params["ephemeral"] = True
        if model is not None:
            params["model"] = model
        r = await self.request("thread/start", params)
        return {"id": r["thread"]["id"], "model": r.get("model"), "effort": r.get("reasoningEffort")}

    async def thread_resume(self, thread_id: str, developer_instructions: str, model: str | None = None) -> dict:
        params = {"threadId": thread_id, "cwd": str(self.workdir), "sandbox": "read-only", "approvalPolicy": "never",
                  "developerInstructions": developer_instructions}
        if model is not None:
            params["model"] = model
        r = await self.request("thread/resume", params)
        return {"id": r["thread"]["id"], "model": r.get("model"), "effort": r.get("reasoningEffort")}

    async def turn_start(self, thread_id: str, text: str, model: str | None = None, effort: str | None = None) -> str:
        params = {"threadId": thread_id, "input": _text(text)}
        if model is not None:
            params["model"] = model
        if effort is not None:
            params["effort"] = effort
        r = await self.request("turn/start", params)
        return r["turn"]["id"]

    async def turn_steer(self, thread_id: str, turn_id: str, text: str) -> None:
        await self.request("turn/steer", {"threadId": thread_id, "expectedTurnId": turn_id, "input": _text(text)})

    async def turn_interrupt(self, thread_id: str, turn_id: str) -> None:
        await self.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})

    async def rate_limits(self) -> UsageSnapshot:
        return snapshot_from_rate_limits(await self.request("account/rateLimits/read", {}))

    async def thread_read(self, thread_id: str) -> dict:
        return await self.request("thread/read", {"threadId": thread_id, "includeTurns": True})

    async def thread_delete(self, thread_id: str) -> None:
        await self.request("thread/delete", {"threadId": thread_id})

    async def account(self) -> dict:
        return await self.request("account/read", {})

    async def auth_mode(self) -> str:
        acc = (await self.account()).get("account")
        if not acc:
            return "none"
        return {"chatgpt": "chatgpt", "apiKey": "apikey"}.get(acc.get("type"), str(acc.get("type")))


async def _probe() -> None:
    import shutil
    import tempfile
    exe = shutil.which("codex")
    if not exe:
        raise SystemExit("codex が見つかりません")
    with tempfile.TemporaryDirectory() as d:
        app = AppServer(codex_command(Path(exe)), "http://127.0.0.1:9", "probe", Path(d))
        await app.start()
        try:
            print("auth_mode:", await app.auth_mode())
            print("usage:", await app.rate_limits())
        finally:
            await app.stop()


if __name__ == "__main__":
    import sys
    if "--probe" in sys.argv:
        asyncio.run(_probe())
