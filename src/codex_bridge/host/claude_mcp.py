"""MCP endpoint used by the local Claude Code (mounted at /claude/mcp, loopback only)."""

from __future__ import annotations

import asyncio

from mcp.server.mcpserver import MCPServer
from mcp_types import CallToolResult

from .approvals import Approvals
from .mcp_util import LoopbackOnly, McpEndpoint, error_result, json_result, make_endpoint
from .paths import ToolError
from .snapshots import Snapshots
from .tasks import ACTIVE_STATES, TaskManager

MAX_WAIT_SEC = 50  # keep each call well under MCP client timeouts

INSTRUCTIONS = (
    "知人の PC で動く Codex に、こちらの共有フォルダでの作業を依頼する道具です。"
    "send_task で依頼し、get_status / get_result で進み具合と結果を確かめます。"
    "コマンド実行や削除の承認は人が Web 管理画面で行います（この道具では承認できません）。"
    "使用量の上限で中断（suspended_quota）したら、resume_at 以降に send_task の continue_task_id で続きを依頼してください。"
)
APPROVAL_NOTE = "承認待ちがあります。人が Web 画面（/admin/）で許可か拒否をするまで、Codex の作業は止まります。"


def build_claude_mcp(mgr: TaskManager, approvals: Approvals, snapshots: Snapshots) -> McpEndpoint:
    server = MCPServer(name="codex-bridge-control", instructions=INSTRUCTIONS)

    async def run(fn) -> CallToolResult:
        try:
            return json_result(await fn())
        except ToolError as e:
            return error_result(str(e))

    def tool(description: str):
        return server.tool(description=description, structured_output=False)

    @tool("Codex に作業を依頼します。continue_task_id を渡すと、そのタスクの会話（セッション）の続きとして依頼します。task_id を返します。")
    async def send_task(prompt: str, continue_task_id: str | None = None) -> CallToolResult:
        async def f():
            return {"task_id": await mgr.start(prompt, continue_task_id)}
        return await run(f)

    @tool("作業中のタスクに追加の指示を送ります。")
    async def send_followup(task_id: str, text: str) -> CallToolResult:
        async def f():
            await mgr.followup(task_id, text)
            return {"ok": True}
        return await run(f)

    @tool("タスクの状態と最近の出来事、承認待ちの一覧を返します。task_id を省くと作業中（なければ最新）のタスク。"
          "wait_sec（最大 50）を渡すと、タスクが終わるか承認待ちが出るまで、その秒数だけ待ってから返します。"
          "作業の完了を待つときは、sleep などを使わずに get_status(wait_sec=50) を繰り返し呼んでください。")
    async def get_status(task_id: str | None = None, wait_sec: int = 0) -> CallToolResult:
        async def f():
            deadline = asyncio.get_running_loop().time() + max(0, min(int(wait_sec), MAX_WAIT_SEC))
            while asyncio.get_running_loop().time() < deadline:
                st = mgr.status(task_id)
                if st.get("state") not in ACTIVE_STATES or approvals.pending():
                    break
                await asyncio.sleep(0.5)
            s = mgr.status(task_id)
            pend = [a.to_dict() for a in approvals.pending()]
            s["pending_approvals"] = pend
            if pend:
                s["note"] = APPROVAL_NOTE
            return s
        return await run(f)

    @tool("タスクの最終報告・変更したファイルの一覧などを返します。")
    async def get_result(task_id: str) -> CallToolResult:
        async def f():
            return mgr.result(task_id)
        return await run(f)

    @tool("作業中のタスクを止めます（まず穏やかに止め、応じなければ強制的に中断します）。")
    async def cancel_task(task_id: str) -> CallToolResult:
        async def f():
            await mgr.cancel(task_id)
            return {"ok": True}
        return await run(f)

    @tool("知人の Codex の使用量（5 時間枠・週間枠の使用率と、戻る時刻）としきい値を返します。")
    async def get_usage() -> CallToolResult:
        async def f():
            return mgr.usage()
        return await run(f)

    @tool("使用量のしきい値（%）を変えます。超えると作業中の Codex に区切りで止めるよう指示し、新しい依頼を止めます。")
    async def set_thresholds(five_hour: float | None = None, weekly: float | None = None) -> CallToolResult:
        async def f():
            return await mgr.set_thresholds(five_hour, weekly)
        return await run(f)

    @tool("タスクがファイル操作の道具で変えたファイルを、すべて元に戻します（コマンド実行による変更は戻りません）。")
    async def undo_task(task_id: str) -> CallToolResult:
        async def f():
            mgr.result(task_id)  # raises ToolError for unknown ids
            if task_id == mgr.current_task_id():
                raise ToolError("作業中のタスクは取り消せません。先に cancel_task で止めてください")
            try:
                return {"restored": snapshots.undo(task_id)}
            except ValueError as e:
                raise ToolError(str(e)) from e
        return await run(f)

    @tool("このツールで作った Codex のセッション（スレッド）の一覧を返します。")
    async def list_sessions() -> CallToolResult:
        return await run(mgr.sessions_list)

    @tool("セッションの会話内容を読みます（このツールで作ったセッションのみ）。")
    async def read_session(thread_id: str) -> CallToolResult:
        async def f():
            return await mgr.session_read(thread_id)
        return await run(f)

    @tool("セッションを削除します（このツールで作ったセッションのみ）。")
    async def delete_session(thread_id: str) -> CallToolResult:
        async def f():
            return await mgr.session_delete(thread_id)
        return await run(f)

    return make_endpoint(server, LoopbackOnly, dns_protection=True)
