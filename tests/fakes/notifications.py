"""app-server notifications in the shapes a real `codex app-server` 0.167.0 sends.

item/*, thread/tokenUsage/updated, turn/*: copied from a capture of the real server (the plan workspace's
task-8-appserver-capture.jsonl), ids shortened. turn/plan/updated: from the app-server's own JSON schema
(TurnPlanUpdatedNotification), because codex 0.167.0 with the available model has no plan tool to
trigger it with. Used as `after` messages of tests/fakes/fake_appserver.py.
"""

TH, TURN = "th1", "u1"


def _n(method: str, **params) -> dict:
    return {"method": method, "params": {"threadId": TH, **params}, "emittedAtMs": 1794732046443}


def turn_started() -> dict:
    return _n("turn/started", turn={"id": TURN, "status": "inProgress", "items": [], "error": None})


def turn_completed(status: str = "completed") -> dict:
    return _n("turn/completed", turn={"id": TURN, "rootTurnId": TURN, "items": [], "itemsView": "summary",
                                       "status": status, "error": None, "durationMs": 12423})


def plan_updated(steps: list[tuple[str, str]], explanation: str | None = None) -> dict:
    return _n("turn/plan/updated", turnId=TURN, explanation=explanation,
              plan=[{"step": s, "status": st} for s, st in steps])


def token_usage(input_tokens: int, output_tokens: int, total_input: int | None = None) -> dict:
    def breakdown(i, o):
        return {"totalTokens": i + o, "inputTokens": i, "cachedInputTokens": 0, "cacheWriteInputTokens": 0,
                "outputTokens": o, "reasoningOutputTokens": 0}
    return _n("thread/tokenUsage/updated", turnId=TURN, tokenUsage={
        "total": breakdown(total_input or input_tokens, output_tokens),
        "last": breakdown(input_tokens, output_tokens), "modelContextWindow": 475000})


def reasoning_completed(summary: list[str] | None = None) -> dict:
    return _n("item/completed", turnId=TURN, completedAtMs=1794732053735, item={
        "type": "reasoning", "id": "rs_1", "summary": summary or [], "content": [],
        "startedAtMs": 1794732049174, "completedAtMs": 1794732053735})


def _mcp_item(status: str, **more) -> dict:
    return {"type": "mcpToolCall", "id": "exec-1", "server": "cap", "tool": "echo", "status": status,
            "arguments": {"text": "hi"}, "appContext": None, "mcpAppUi": None, "pluginId": None,
            "readOnlyHint": None, "result": None, "error": None, "durationMs": None,
            "startedAtMs": 1794732106464, "completedAtMs": None, **more}


def mcp_started(**item_fields) -> dict:
    return _n("item/started", turnId=TURN, startedAtMs=1794732106464, item=_mcp_item("inProgress", **item_fields))


def mcp_completed(text: str = "echo: hi", **item_fields) -> dict:
    fields = {"result": {"content": [{"type": "text", "text": text}], "structuredContent": {"result": text},
                         "_meta": None}, "durationMs": 3, "completedAtMs": 1794732106468, **item_fields}
    return _n("item/completed", turnId=TURN, completedAtMs=1794732106468, item=_mcp_item("completed", **fields))


def mcp_failed(message: str, **item_fields) -> dict:
    return _n("item/completed", turnId=TURN, completedAtMs=1794732106468, item=_mcp_item(
        "failed", error={"message": message}, durationMs=5, completedAtMs=1794732106469, **item_fields))


def agent_delta(delta: str, item_id: str = "msg_1") -> dict:
    return _n("item/agentMessage/delta", turnId=TURN, itemId=item_id, delta=delta)


def agent_completed(text: str, item_id: str = "msg_1", phase: str = "commentary") -> dict:
    return _n("item/completed", turnId=TURN, completedAtMs=1794732046442, item={
        "type": "agentMessage", "id": item_id, "text": text, "phase": phase, "memoryCitation": None,
        "delivery": None, "questions": None, "startedAtMs": 1794732045912, "completedAtMs": 1794732046442})
