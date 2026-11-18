"""WebSocket /agent/ws: the agent's control channel."""

from __future__ import annotations

import asyncio

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from ..common import protocol
from .tasks import TaskManager
from .tokens import TokenStore


def _lender_name(ws: WebSocket, tokens: TokenStore) -> str | None:
    """The invite name of the token in the Authorization header: that is who the lender is."""
    header = ws.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    return tokens.match(token)


def add_control_route(app: FastAPI, mgr: TaskManager, tokens: TokenStore) -> None:
    mgr.invited_names = lambda: {i["name"] for i in tokens.list()}
    @app.websocket("/agent/ws")
    async def agent_ws(ws: WebSocket) -> None:
        name = _lender_name(ws, tokens)
        if name is None:
            await ws.close(code=1008)
            return
        await ws.accept()

        async def send(m: dict) -> None:
            if _lender_name(ws, tokens) != name:
                raise ConnectionError("招待コードが失効しました")
            await ws.send_text(protocol.encode(m))

        link = mgr.attach(name, send)
        loop = asyncio.get_running_loop()

        async def disconnect() -> None:
            mgr.detach(link)  # identical to a network disconnect, including any running task
            await ws.close(code=1008)

        def changed(changed_name: str) -> None:
            if changed_name == name:
                loop.call_soon_threadsafe(lambda: asyncio.create_task(disconnect()))

        tokens.on_change.append(changed)
        try:
            if _lender_name(ws, tokens) != name:  # token changed while accept() was in flight
                await disconnect()
                return
            while True:
                text = await ws.receive_text()
                if _lender_name(ws, tokens) != name:
                    await disconnect()
                    return
                try:
                    m = protocol.decode(text)
                except ValueError:
                    continue
                await mgr.on_agent_message(link, m)
        except WebSocketDisconnect:
            pass
        finally:
            tokens.on_change.remove(changed)
            mgr.detach(link)
