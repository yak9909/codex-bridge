"""A scripted stand-in for `codex app-server` (stdio JSON-RPC, one JSON per line).

Usage: python -I fake_appserver.py <script.json> [ignored codex args...]

script.json:
  record:    path of a JSON Lines file; gets {"argv"...}, {"env"...} and every received message
  responses: {method: result}            (default {} ; built-ins for thread/start, turn/start)
  errors:    {method: {"code":..,"message":..}}
  after:     {method: [message, ...]}    messages to emit after answering that method
             (a message with "id" is a server->client request)
"""

import json
import os
import sys

DEFAULTS = {
    "initialize": {"userAgent": "fake"},
    "thread/start": {"thread": {"id": "th1"}},
    "thread/resume": {"thread": {"id": "th1"}},
    "thread/read": {"thread": {"id": "th1", "turns": []}},
    "turn/start": {"turn": {"id": "u1", "status": "inProgress"}},
    "turn/steer": {"turnId": "u1"},
    "account/read": {"account": {"type": "chatgpt", "email": None, "planType": "plus"}, "requiresOpenaiAuth": True},
    "account/rateLimits/read": {"rateLimits": {
        "primary": {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": 1800000000},
        "secondary": {"usedPercent": 30, "windowDurationMins": 10080, "resetsAt": 1800500000}}},
}


def main() -> None:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    script = json.load(open(sys.argv[1], encoding="utf-8"))
    rec = open(script["record"], "a", encoding="utf-8")

    def log(obj):
        rec.write(json.dumps(obj, ensure_ascii=False) + "\n")
        rec.flush()

    log({"argv": sys.argv[2:]})
    log({"env": {"CODEX_BRIDGE_TOKEN": os.environ.get("CODEX_BRIDGE_TOKEN")}})

    def emit(obj):
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    responses = {**DEFAULTS, **script.get("responses", {})}
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        log({"received": msg})
        method = msg.get("method")
        if method is None:
            continue  # a response to one of our server requests (already logged)
        if "id" in msg:
            err = script.get("errors", {}).get(method)
            if err:
                emit({"jsonrpc": "2.0", "id": msg["id"], "error": err})
            else:
                emit({"jsonrpc": "2.0", "id": msg["id"], "result": responses.get(method, {})})
        for out in script.get("after", {}).get(method, []):
            emit({"jsonrpc": "2.0", **out})


if __name__ == "__main__":
    main()
