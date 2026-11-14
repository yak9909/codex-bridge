"""WebSocket /agent/ws: the agent's control channel."""

from __future__ import annotations

import hmac

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from ..common import protocol
from .mcp_util import sha256_hex
from .tasks import TaskManager
from .tokens import TokenStore


def _authorized(ws: WebSocket, tokens: TokenStore) -> bool:
    header = ws.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not token:
        return False
    digest = sha256_hex(token)
    return any(hmac.compare_digest(digest, h) for h in tokens.hashes())


def add_control_route(app: FastAPI, mgr: TaskManager, tokens: TokenStore) -> None:
    @app.websocket("/agent/ws")
    async def agent_ws(ws: WebSocket) -> None:
        if not _authorized(ws, tokens):
            await ws.close(code=1008)
            return
        await ws.accept()

        async def send(m: dict) -> None:
            await ws.send_text(protocol.encode(m))

        link = mgr.attach(send)
        try:
            while True:
                text = await ws.receive_text()
                try:
                    m = protocol.decode(text)
                except ValueError:
                    continue
                await mgr.on_agent_message(link, m)
        except WebSocketDisconnect:
            pass
        finally:
            mgr.detach(link)
