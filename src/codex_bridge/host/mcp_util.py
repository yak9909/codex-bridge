"""Helpers shared by the Codex-facing and Claude-facing MCP endpoints."""

from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import CallToolResult, TextContent


def json_result(obj: Any) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(obj, ensure_ascii=False, default=str))])


def error_result(msg: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=msg)], is_error=True)


def sha256_hex(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class BearerAuth:
    """ASGI wrapper: accept only requests whose bearer token is currently valid, and note whose it is.

    match_fn(token) is the invite name the token was issued under, or None when it is not valid.
    The name goes to scope["state"]["lender"], so a request never gets in without one.
    """

    def __init__(self, app, match_fn: Callable[[str], str | None]) -> None:
        self.app = app
        self.match_fn = match_fn

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            header = dict(scope.get("headers") or []).get(b"authorization", b"").decode("latin-1")
            token = header[7:].strip() if header.lower().startswith("bearer ") else ""
            name = self.match_fn(token) if token else None
            if name is None:
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
            # New dicts: the server may hand the same state object to every request.
            scope = {**scope, "state": {**(scope.get("state") or {}), "lender": name}}
        await self.app(scope, receive, send)


class LoopbackOnly:
    """ASGI wrapper: refuse requests that do not come from this machine."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] in ("http", "websocket"):
            client = scope.get("client") or ("", 0)
            if client[0] not in ("127.0.0.1", "::1", "localhost"):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                    return
                await send({"type": "http.response.start", "status": 403,
                            "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
                await send({"type": "http.response.body", "body": b"forbidden"})
                return
        await self.app(scope, receive, send)


@dataclass
class McpEndpoint:
    asgi: Any
    server: MCPServer

    @asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        manager = self.server._lowlevel_server._session_manager
        async with manager.run():
            yield


def make_endpoint(server: MCPServer, wrap: Callable[[Any], Any], dns_protection: bool) -> McpEndpoint:
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=dns_protection,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
    )
    app = server.streamable_http_app(streamable_http_path="/mcp", json_response=True, stateless_http=True,
                                     transport_security=security)
    return McpEndpoint(asgi=wrap(app), server=server)
