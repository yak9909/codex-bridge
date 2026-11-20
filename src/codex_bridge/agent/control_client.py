"""Keeps the WebSocket to the host open, reconnecting with backoff."""

from __future__ import annotations

import asyncio
from typing import Callable

import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus

from ..common import protocol
from ..common.invite import Invite
from .runner import AgentRunner

MAX_BACKOFF = 60.0


def ws_url(http_url: str) -> str:
    url = http_url.rstrip("/")
    if url.startswith("https://"):
        url = "wss://" + url[len("https://"):]
    elif url.startswith("http://"):
        url = "ws://" + url[len("http://"):]
    return url + "/agent/ws"


class ControlClient:
    def __init__(self, invite: Invite, runner: AgentRunner, hello: dict | Callable[[], dict], initial_backoff: float = 1.0,
                 on_status: Callable[[str], None] | None = None) -> None:
        self.invite = invite
        self.runner = runner
        self.hello = hello
        self.initial_backoff = initial_backoff
        self.on_status = on_status or (lambda s: None)
        self.ws = None
        self.paused = False
        self.invite_problem: str | None = None
        runner.send = self.send

    async def send(self, m: dict) -> None:
        if self.ws is None:
            raise ConnectionError("not connected")
        await self.ws.send(protocol.encode(m))

    async def send_hello(self, with_task: bool = True) -> None:
        """Say who this agent is and what it can do. A callable `hello` is called now, so that a setting changed since
        the last connection (disposable, pre-approval) is what the host hears.

        `active_task_ids` includes work and pending approvals; `current_task_id` keeps the first for old hosts.
        A new host fails tasks it thinks are running here but are absent from this list. That is for a new connection only: a
        hello about changed settings leaves it out (`with_task=False`), because it can overlap the start of a task
        the agent has not heard of yet."""
        if self.ws is None:
            return
        fields = self.hello() if callable(self.hello) else self.hello
        extra = {"current_task_id": self.runner.active_task_id(),
                 "active_task_ids": self.runner.active_task_ids()} if with_task else {}
        await self.send(protocol.msg("hello", **fields, **extra))

    async def refresh_hello(self) -> None:
        """For the lender changing settings while connected (SettingsApplier)."""
        await self.send_hello(with_task=False)

    async def on_connected(self) -> None:
        """A connection was made. First what the host may have missed (a task that ended while the link was down
        must be over on the host before hello, or hello, which fails the tasks the agent does not name, would
        take its result away: a final state is not left again). Then hello. Then the request that still waits for the
        lender, said again with a number of its own: the host must never lose a task waiting for the lender."""
        await self.runner.resend_unacked()
        await self.send_hello()
        await self.runner.resend_pending()
        await self.runner.send_busy_status()
        if self.runner.slot and self.runner.slot.last_usage:
            snap = self.runner.slot.last_usage
            await self.send(protocol.msg("usage", **snap.to_message(), friend_limits=self.runner.friend_limits))

    async def drop(self) -> None:
        """Close the current connection (used by tests and on shutdown)."""
        if self.ws is not None:
            await self.ws.close()

    async def run_forever(self) -> None:
        backoff = self.initial_backoff
        while True:
            if self.paused:
                self.on_status("一時停止中")
                await asyncio.sleep(1)
                continue
            try:
                async with websockets.connect(
                        ws_url(self.invite.url), max_size=None, ping_interval=20, ping_timeout=60,
                        additional_headers={"Authorization": f"Bearer {self.invite.token}"}) as ws:
                    self.ws = ws
                    self.invite_problem = None
                    backoff = self.initial_backoff
                    self.on_status("接続しました")
                    await self.on_connected()
                    async for text in ws:
                        try:
                            m = protocol.decode(text)
                        except ValueError:
                            continue
                        try:
                            if m["type"] in {"task_start", "task_followup", "session_read", "session_delete"}:
                                self.runner.handle_later(m)
                            else:
                                await self.runner.handle(m)
                        except Exception as e:  # keep the connection loop alive whatever happens
                            self.on_status(f"処理中にエラー: {type(e).__name__}")
            except InvalidStatus as e:
                if e.response.status_code in (401, 403):
                    self.invite_problem = "rejected"
                self.on_status(f"接続を断られました（{e.response.status_code}）。招待コードが失効していないか確かめてください")
            except (OSError, ConnectionClosed, asyncio.TimeoutError) as e:
                self.on_status(f"接続できません: {type(e).__name__}")
            finally:
                self.ws = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)
