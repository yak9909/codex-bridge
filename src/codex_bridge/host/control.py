"""WebSocket /agent/ws: the agent's control channel."""

from __future__ import annotations

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
    @app.websocket("/agent/ws")
    async def agent_ws(ws: WebSocket) -> None:
        name = _lender_name(ws, tokens)
        if name is None:
            await ws.close(code=1008)
            return
        await ws.accept()

        async def send(m: dict) -> None:
            await ws.send_text(protocol.encode(m))

        link = mgr.attach(name, send)
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
