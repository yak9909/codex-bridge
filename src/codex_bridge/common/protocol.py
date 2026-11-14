"""Messages between host and agent (JSON objects with a "type").

host -> agent:
  task_start{task_id, prompt, continue_thread_id}   task_followup{task_id, text}   task_cancel{task_id}
  session_list{req_id}   session_read{req_id, thread_id}   session_delete{req_id, thread_id}
  settings{thresholds:{five_hour, weekly}, log_visibility}   ack{seq}
agent -> host:
  hello{agent_version, codex_version, auth_mode, current_task_id}
    v0.2 adds: protocol: 2, capabilities{resumable, pre_approval}, limits{five_hour, weekly, force}
    (without them the host treats the agent as v0.1: sessions can be continued, no pre-approval)
  event{seq, task_id, kind, data}            kind: started|agent_message|tool_call|turn_completed|status|final_report|error
  usage{five_hour:{used_percent, resets_at}|null, weekly:{...}|null, ts}
  task_state{seq, task_id, state, thread_id, report, resume_at}
  reply{req_id, ok, data|error}
Messages with "seq" are re-sent by the agent after a reconnect until acked; the host drops duplicates.
"""

from __future__ import annotations

import json

HOST_TO_AGENT = {"task_start", "task_followup", "task_cancel", "session_list", "session_read",
                 "session_delete", "settings", "ack"}
AGENT_TO_HOST = {"hello", "event", "usage", "task_state", "reply"}
TYPES = HOST_TO_AGENT | AGENT_TO_HOST

TASK_STATES = ("queued", "running", "completed", "suspended_quota", "failed", "cancelled", "agent_offline")
FINAL_STATES = ("completed", "suspended_quota", "failed", "cancelled")
LOG_VISIBILITY = ("summary", "files", "all")


def msg(type_: str, **fields) -> dict:
    if type_ not in TYPES:
        raise ValueError(f"unknown message type: {type_}")
    return {"type": type_, **fields}


def encode(m: dict) -> str:
    return json.dumps(m, ensure_ascii=False)


def decode(s: str) -> dict:
    try:
        m = json.loads(s)
    except json.JSONDecodeError as e:
        raise ValueError(f"bad message: {e}") from e
    if not isinstance(m, dict) or m.get("type") not in TYPES:
        raise ValueError(f"unknown message: {str(s)[:80]}")
    return m
