"""Messages between host and agent (JSON objects with a "type").

host -> agent:
  task_start{task_id, prompt, continue_thread_id}   task_followup{task_id, text}   task_cancel{task_id}
  session_list{req_id}   session_read{req_id, thread_id}   session_delete{req_id, thread_id}
  settings{thresholds:{five_hour, weekly}, log_visibility}   ack{seq}
agent -> host:
  hello{agent_version, codex_version, auth_mode, current_task_id}
    v0.2 adds: protocol: 2, capabilities{resumable, pre_approval}, limits{five_hour, weekly, force}
    (without them the host treats the agent as v0.1: sessions can be continued, no pre-approval)
  event{seq, task_id, kind, data}            kind: started|agent_message|turn_completed|status|final_report|error
    v0.2 adds (design 4.10): plan{steps:[{step,status}]}  reasoning_summary{text}
    tool_started{tool,arguments}  tool_finished{tool,arguments,duration_ms,success,result_summary}
    token_usage{input,output} (one per turn)  progress{text} (the half-written message: no seq, at most every
    2 seconds, kept by the host in memory only). tool_finished replaces v0.1's tool_call, which the host still shows.
  usage{five_hour:{used_percent, resets_at}|null, weekly:{...}|null, ts}
  task_state{seq, task_id, state, thread_id, report, resume_at}
  reply{req_id, ok, data|error}
Messages with "seq" are re-sent by the agent after a reconnect until acked; the host drops duplicates.
"""

from __future__ import annotations

import json
import math

HOST_TO_AGENT = {"task_start", "task_followup", "task_cancel", "session_list", "session_read",
                 "session_delete", "settings", "ack"}
AGENT_TO_HOST = {"hello", "event", "usage", "task_state", "reply"}
TYPES = HOST_TO_AGENT | AGENT_TO_HOST

TASK_STATES = ("queued", "running", "completed", "suspended_quota", "failed", "cancelled", "agent_offline",
               "awaiting_lender", "rejected_by_lender")
FINAL_STATES = ("completed", "suspended_quota", "failed", "cancelled", "rejected_by_lender")
LOG_VISIBILITY = ("summary", "files", "all")


def msg(type_: str, **fields) -> dict:
    if type_ not in TYPES:
        raise ValueError(f"unknown message type: {type_}")
    return {"type": type_, **fields}


def encode(m: dict) -> str:
    return json.dumps(m, ensure_ascii=False)


def finite(v):
    """`v` with every number JSON cannot carry (NaN, Infinity, -Infinity) replaced by None, at any depth.

    Python's json reads and writes those three although they are not JSON, and reads a number too
    large for a float (1e999) as infinity. Kept as they are, they end up in the files and in the
    answers of the admin API, where one of them makes the whole answer an error."""
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, dict):
        return {k: finite(x) for k, x in v.items()}
    if isinstance(v, list):
        return [finite(x) for x in v]
    return v


def decode(s: str) -> dict:
    """A message from the other side; ValueError when it is not one. Numbers JSON cannot carry
    arrive as None (see `finite`), so nothing taken from a message can hold one."""
    try:
        m = finite(json.loads(s))
    except (ValueError, RecursionError) as e:  # not JSON, a number with too many digits, nested too deep
        raise ValueError(f"bad message: {e}") from e
    if not isinstance(m, dict) or m.get("type") not in TYPES:
        raise ValueError(f"unknown message: {str(s)[:80]}")
    return m
