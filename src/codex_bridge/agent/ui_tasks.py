"""Project WorkLog records into the shared task UI, filtering only at display time."""

from __future__ import annotations

import json

from .runner import AgentRunner

TOOLS = {"tool_call", "tool_started", "tool_finished"}
FINISHED = {"completed", "cancelled", "failed", "suspended_quota", "rejected_by_lender"}
PATH_KEYS = {"path", "paths", "cwd", "source", "destination", "src", "dst"}


def visible_events(events: list[dict], visibility: str) -> list[dict]:
    out = []
    for event in events:
        if event.get("kind") not in TOOLS:
            out.append(event)
            continue
        if visibility == "all":
            out.append(event)
        elif visibility == "files":
            data = event.get("data") or {}
            args = data.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}  # unstructured command text may contain secrets, not just paths
            paths = {k: v for k, v in args.items() if k in PATH_KEYS and
                     (isinstance(v, str) or isinstance(v, list) and all(isinstance(p, str) for p in v))
                     } if isinstance(args, dict) else {}
            # Names and target paths only: no content, command arguments, results or error body.
            out.append({**event, "data": {"tool": data.get("tool"), "arguments": paths}})
    return out


def task_view(runner: AgentRunner, task_id: str, *, detail: bool = True) -> dict | None:
    log = runner.worklog
    if log is None:
        return None
    try:
        events = log.task_events(task_id)
    except ValueError:
        return None
    if not events:
        return None
    task = {"task_id": task_id, "created": events[0]["ts"], "finished": None, "state": "queued",
            "prompt": "", "report": None, "thread_id": None, "resume_at": None}
    for event in events:
        data = event.get("data")
        data = data if isinstance(data, dict) else {}
        if event.get("kind") == "task_prompt" and not task["prompt"]:
            task["prompt"] = data.get("text", "")
        if event.get("kind") == "state":
            task["state"] = data.get("state", task["state"])
            task["resume_at"] = data.get("resume_at")
            if data.get("report") is not None:
                task["report"] = data["report"]
            if task["state"] in FINISHED and task["finished"] is None:
                task["finished"] = event["ts"]
        elif event.get("kind") == "final_report":  # older/imported records
            task["report"] = data.get("text")
    for session in runner.sessions.list():
        if task_id in session["task_ids"]:
            task["thread_id"] = session["thread_id"]
    cur = runner.cur
    pending = runner.pending_approval
    if cur is not None and cur.task_id == task_id:
        task.update(state="running", thread_id=cur.thread_id, finished=None)
        if detail:
            task["progress"] = cur.progress
    if pending is not None and pending["task_id"] == task_id:
        task.update(state="awaiting_lender", finished=None)
    if detail:
        task["events"] = visible_events(events, runner.log_visibility)
        task["event_count"] = len(task["events"])
    return task
