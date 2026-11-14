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
    def __init__(self, invite: Invite, runner: AgentRunner, hello: dict, initial_backoff: float = 1.0,
                 on_status: Callable[[str], None] | None = None) -> None:
        self.invite = invite
        self.runner = runner
        self.hello = hello
        self.initial_backoff = initial_backoff
        self.on_status = on_status or (lambda s: None)
        self.ws = None
        self.paused = False
        runner.send = self.send

    async def send(self, m: dict) -> None:
        if self.ws is None:
            raise ConnectionError("not connected")
        await self.ws.send(protocol.encode(m))

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
                    backoff = self.initial_backoff
                    self.on_status("接続しました")
                    await self.send(protocol.msg("hello", **self.hello))
                    await self.runner.resend_unacked()
                    async for text in ws:
                        try:
                            m = protocol.decode(text)
                        except ValueError:
                            continue
                        await self.runner.handle(m)
            except InvalidStatus as e:
                self.on_status(f"接続を断られました（{e.response.status_code}）。招待コードが失効していないか確かめてください")
            except (OSError, ConnectionClosed, asyncio.TimeoutError) as e:
                self.on_status(f"接続できません: {type(e).__name__}")
            finally:
                self.ws = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)
