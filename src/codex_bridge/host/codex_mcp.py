"""MCP endpoint used by the friend's Codex (mounted at /codex/mcp)."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer
from mcp_types import CallToolResult

from ..common.audit import AuditLog
from .approvals import Approvals
from .fs_read import ReadTools
from .fs_write import WriteTools
from .mcp_util import BearerAuth, McpEndpoint, error_result, json_result, make_endpoint
from .patch import apply_patch as _apply_patch
from .paths import SafePaths, ToolError
from .policy import PolicyStore
from .runner import CommandRunner
from .search import SearchTools
from .snapshots import Snapshots

INSTRUCTIONS = (
    "依頼主の PC の共有フォルダを操作する道具です。共有フォルダは複数登録できます。"
    'list_dir("/")（path 省略・空文字も同じ）で登録フォルダ一覧を確認できます。'
    "メイン内は相対パス、ほかの共有フォルダ内は絶対パス（区切りは /）で指定してください。"
    '明示的な "." はメインを指します。'
    "find_files と grep は path 省略で見えるすべてのフォルダを検索します。"
    "結果のパスもメイン内は相対パス、ほかは絶対パスです。"
    "見つからないパスは存在しないか、見ることを許されていない場所です。"
)

# Invite name of the lender whose Codex sent the message being handled. Set only while that one
# message is handled, so two lenders calling at the same time each see their own name.
current_lender: ContextVar[str | None] = ContextVar("current_lender", default=None)
current_thread_id: ContextVar[str | None] = ContextVar("current_thread_id", default=None)
call_task_id: ContextVar[str | None] = ContextVar("call_task_id", default=None)


async def _as_lender(ctx, call_next):
    """MCP server middleware: handle the message as the lender BearerAuth matched its token to."""
    scope = getattr(ctx.request, "scope", None) or {}
    meta = (ctx.params or {}).get("_meta") or {}
    thread_id = meta.get("threadId") if isinstance(meta, dict) else None
    thread_token = current_thread_id.set(thread_id if isinstance(thread_id, str) and thread_id else None)
    token = current_lender.set((scope.get("state") or {}).get("lender"))
    try:
        return await call_next(ctx)
    finally:
        current_lender.reset(token)
        current_thread_id.reset(thread_token)


@dataclass
class HostServices:
    paths: SafePaths
    policy: PolicyStore
    read: ReadTools
    search: SearchTools
    write: WriteTools
    runner: CommandRunner
    approvals: Approvals
    snapshots: Snapshots
    audit: AuditLog
    task_id_fn: Callable[[], str | None]


def _summarize(args: dict) -> dict:
    out = {}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > 300:
            out[k] = v[:200] + f"…（{len(v)} 文字）"
        else:
            out[k] = v
    return out


def build_codex_mcp(s: HostServices, match_token: Callable[[str], str | None]) -> McpEndpoint:
    """match_token(token) is the lender's invite name, or None for a token that is not valid."""
    server = MCPServer(name="codex-bridge", instructions=INSTRUCTIONS, middleware=[_as_lender])

    async def run(name: str, args: dict, fn, *, blocking: bool) -> CallToolResult:
        task_id = s.task_id_fn()
        token = call_task_id.set(task_id)
        try:
            s.paths.check_configured()
            if name in {"write_file", "edit_file", "apply_patch", "move_path", "delete_path", "run_command"} and task_id is None:
                raise ToolError("どの依頼の操作か分からないため、書き込み・削除・コマンド実行は受け付けません。")
            result = await (asyncio.to_thread(fn) if blocking else fn())
        except (ToolError, OSError) as e:
            if isinstance(e, PermissionError):
                # Use the caller's path, never an internal snapshot/temporary filename.
                path = args.get("path") or args.get("src") or "指定されたファイル"
                failed = e.filename2 or e.filename
                if not args.get("path") and isinstance(failed, str) and s.paths.visible(Path(failed)):
                    path = s.paths.display(Path(failed))
                message = "Windows がこの操作を断りました（管理者の権限が要る場所か、ほかのプログラムが使用中の可能性があります）: " + path
            elif isinstance(e, OSError):
                # OSError.__str__ includes filenames, which can belong to our private data.
                message = f"ファイルの操作に失敗しました: {e.strerror or str(e)}"
            else:
                message = str(e)
            s.audit.write("tool_call", tool=name, args=_summarize(args), ok=False, error=message, task_id=task_id)
            return error_result(message)
        finally:
            call_task_id.reset(token)
        s.audit.write("tool_call", tool=name, args=_summarize(args), ok=True, task_id=task_id)
        return json_result(result)

    def tool(description: str):
        return server.tool(description=description, structured_output=False)

    @tool("path 省略・空文字・/ は登録フォルダ一覧、. はメインの中身を返します。"
          "path: メイン内は相対パス、ほかの共有フォルダ内は絶対パス。指定フォルダでは名前・種類・大きさ・更新日時を返します。")
    async def list_dir(path: str = "/") -> CallToolResult:
        return await run("list_dir", {"path": path}, lambda: s.read.list_dir(path.strip() or "/"), blocking=True)

    @tool("検索先の配下でファイル名を glob（pattern、例: **/*.csv）で探します。"
          "path: メイン内は相対パス、ほかの共有フォルダ内は絶対パス。省略で見える全フォルダ、. でメインを検索。結果も同じパス形式。")
    async def find_files(pattern: str, path: str | None = None, limit: int = 1000) -> CallToolResult:
        return await run("find_files", {"pattern": pattern, "path": path},
                         lambda: s.read.find_files(pattern, path, limit), blocking=True)

    @tool("ファイルの中身を正規表現で検索します（ripgrep）。path: メイン内は相対パス、ほかの共有フォルダ内は絶対パス。"
          "省略で見える全フォルダ、. でメインを検索。結果も同じパス形式。glob で対象を絞れます。")
    async def grep(pattern: str, path: str | None = None, glob: str | None = None, ignore_case: bool = False,
                   context: int = 0, limit: int = 500) -> CallToolResult:
        return await run("grep", {"pattern": pattern, "path": path, "glob": glob},
                         lambda: s.search.grep(pattern, path, glob, ignore_case, context, limit), blocking=True)

    @tool("ファイルやフォルダの大きさ・更新日時を返します。path: メイン内は相対パス、ほかの共有フォルダ内は絶対パス。")
    async def stat(path: str) -> CallToolResult:
        return await run("stat", {"path": path}, lambda: s.read.stat(path), blocking=True)

    @tool("ファイルを読みます。path: メイン内は相対パス、ほかの共有フォルダ内は絶対パス。テキストは offset_line と max_lines で行単位に、"
          "バイナリは byte_offset と max_bytes で 16 進の一部を返します。続きがあれば next_offset_line / next_byte_offset を返します。")
    async def read_file(path: str, offset_line: int = 1, max_lines: int = 2000,
                        byte_offset: int | None = None, max_bytes: int | None = None) -> CallToolResult:
        return await run("read_file", {"path": path, "offset_line": offset_line},
                         lambda: s.read.read_file(path, offset_line, max_lines, byte_offset, max_bytes), blocking=True)

    @tool("ファイルを新規作成または全体を書き換えます。path: メイン内は相対パス、ほかの共有フォルダ内は絶対パス。既存ファイルの文字コードと改行は保ちます。")
    async def write_file(path: str, content: str) -> CallToolResult:
        return await run("write_file", {"path": path, "content": content},
                         lambda: s.write.write_file(path, content), blocking=True)

    @tool("ファイルの一部を置き換えます。path: メイン内は相対パス、ほかの共有フォルダ内は絶対パス。old は一意に一致する必要があります（replace_all で全置換）。")
    async def edit_file(path: str, old: str, new: str, replace_all: bool = False) -> CallToolResult:
        return await run("edit_file", {"path": path, "old": old, "new": new},
                         lambda: s.write.edit_file(path, old, new, replace_all), blocking=True)

    @tool("'*** Begin Patch' 形式のパッチを適用します（Add File / Update File / Move to / Delete File）。"
          "パッチ内のパス: メイン内は相対パス、ほかの共有フォルダ内は絶対パス。全部の変更を検証してから適用します。")
    async def apply_patch(patch: str) -> CallToolResult:
        return await run("apply_patch", {"patch": patch}, lambda: _apply_patch(s.write, patch), blocking=False)

    @tool("ファイルやフォルダを移動・改名します。src と dst: メイン内は相対パス、ほかの共有フォルダ内は絶対パス。")
    async def move_path(src: str, dst: str) -> CallToolResult:
        return await run("move_path", {"src": src, "dst": dst}, lambda: s.write.move_path(src, dst), blocking=True)

    @tool("ファイルやフォルダを削除します。path: メイン内は相対パス、ほかの共有フォルダ内は絶対パス。管理者の承認が要る場合は "
          "status=waiting_approval を返すので、しばらくしてから同じ呼び出しをやり直してください。")
    async def delete_path(path: str) -> CallToolResult:
        return await run("delete_path", {"path": path}, lambda: s.write.delete_path(path), blocking=False)

    @tool("依頼主の PC でコマンドを実行します（Windows の cmd）。cwd: メイン内は相対パス、ほかの共有フォルダ内は絶対パス。"
          "時間がかかる場合や承認待ちの場合は job_id を返すので、get_command_result で結果を確かめてください。")
    async def run_command(command: str, cwd: str = ".", timeout_sec: int | None = None) -> CallToolResult:
        return await run("run_command", {"command": command, "cwd": cwd},
                         lambda: s.runner.run_command(command, cwd, timeout_sec), blocking=False)

    @tool("run_command が返した job_id の結果を取得します。まだ終わっていなければ、終わった時点ですぐ返り、"
          "終わらなくても最長 60 秒で返ります（status が running か waiting_approval なら、もう一度呼んでください）。"
          "wait_sec は使いません（渡しても待つ時間は変わりません）。間をあけたり、ほかの方法で待ったりする必要はありません。")
    async def get_command_result(job_id: str, wait_sec: int | None = None) -> CallToolResult:
        return await run("get_command_result", {"job_id": job_id},
                         lambda: s.runner.get_command_result(job_id, wait_sec), blocking=False)

    return make_endpoint(server, lambda app: BearerAuth(app, match_token), dns_protection=False)
