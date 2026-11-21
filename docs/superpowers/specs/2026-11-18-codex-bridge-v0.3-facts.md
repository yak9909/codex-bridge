# codex-bridge v0.3 — facts (Task 1)

Checked on 2026-11-18 with Claude Code 2.1.327 (`--model haiku`) and codex-cli 0.167.0 (`gpt-6-luna`, effort low) on Windows 10.
All runs used throwaway folders in the session scratchpad (`v03-facts/`). `~/.claude*` and `~/.codex` were not changed.

| ID | Question | Value |
|---|---|---|
| F1 | Claude Code accepts `"timeout": 86400` on an `asyncRewake` PostToolUse hook | `ACCEPTED` |
| F2 | Codex accepts `"timeout": 86400` in hooks.json | `ACCEPTED` |
| F3 | Agent app-server with `--disable hooks` does not run a Stop hook | `DISABLE_HOOKS_WORKS` (from `hooks/list`; see the evidence) |
| F4 | A value in Codex's MCP HTTP requests equals the Stop hook's `session_id` | `META sessionId` |
| F5 | Codex keeps `?client=codex` on every MCP request | `QUERY_KEPT` |
| F6 | `thread/resume` takes `model` | `YES (schema)` |
| F7 | Claude Code PostToolUse for an MCP tool that returns `isError` | `NOT_FIRED` |
| F8 | Matcher `mcp__codex-bridge__.*` matches tools of a server named `codex-bridge` | `MATCHES` |
| F9 | A quoted `/`-path with a space and non-ASCII works as hook command in both tools | `USE claude: "<exe>" hook claude …` / `codex: & "<exe>" hook codex …` |
| F10 | Japanese as UTF-8 bytes arrives intact (Claude stderr, Codex stdout JSON) | `UTF8_OK` |
| F11 | `model/list`: `id == model`; efforts; pages | `ID_EQUALS_MODEL` |

## Evidence

### F1, F7, F8, F9 (Claude), F10 (Claude)
- Project `cc/proj` with `.mcp.json` server `codex-bridge` (stdio, tools `send_task` and `fail_task`; `fail_task` returns `isError: true`).
- `.claude/settings.json`: `{"hooks":{"PostToolUse":[{"matcher":"mcp__codex-bridge__.*","hooks":[{"type":"command","command":"\"C:/…/v03-facts/cc/with space デスク/hook.cmd\"","asyncRewake":true,"timeout":86400}]}]}}`.
- `hook.cmd` runs `.venv` python on `hook.py`, which logs stdin, writes `日本語テスト: 完了` to stderr as UTF-8 bytes, and exits 2.
- Run: `claude -p --input-format stream-json --output-format stream-json --verbose --model haiku --setting-sources project --mcp-config proj/.mcp.json --strict-mcp-config --allowedTools mcp__codex-bridge__send_task,mcp__codex-bridge__fail_task`.
- Result:
  - The hook log has exactly one stdin: `tool_name = mcp__codex-bridge__send_task`. So F8 matches, and F7: `fail_task` (`is_error: true` in the stream) did **not** fire PostToolUse.
  - The stdin keys were `cwd, duration_ms, effort, hook_event_name, mcp_server, permission_mode, prompt_id, session_id, tool_input, tool_name, tool_response, tool_use_id, transcript_path`.
  - `tool_response` was `[{"type":"text","text":"{\n  \"task_id\": \"7cef23b2\",\n  \"status\": \"accepted\"\n}"}]`.
  - The woken model replied `知らせ: 日本語テスト: 完了` (F10 for Claude). The quoted `/`-path with a space and `デスク` ran (F9 for Claude).
- Control: the same run with `"timeout": "x"` → no hook ran at all (no log file), and no settings error showed in the stream. So a bad value disables the hook silently, and F1's run is meaningful.

### F2
- Temp `CODEX_HOME` with an empty `config.toml` and `hooks.json` = one Stop hook `{"type":"command","command":"echo hi","timeout":86400}`. Driver: `codex app-server`, then `initialize`, `initialized`, `hooks/list`.
- Result: one hook with `"timeoutSec":86400`, `"warnings":[]`, `"errors":[]`.
- Control `"timeout":"x"`: `hooks: []`, `errors: []`, and `warnings: ["failed to parse hooks config …\\hooks.json: invalid type: string \"x\", expected u64 at line 1 column 81"]`.
  - **A parse failure shows in `warnings`, not in `errors`.**

### F3
- `codex app-server` has no `--dangerously-bypass-hook-trust` flag (`codex app-server --help`).
- A project `.codex/hooks.json` was not listed by `hooks/list` for the project cwd, even with `git init` and `-c projects.'<lower-case path>'.trust_level="trusted"`.
- So the hook came from `-c hooks.Stop=[…]` (source `sessionFlags`). It stays `trustStatus: untrusted`, and `-c hooks.state.'<key>'.trusted_hash=…` is ignored for session flags ("unrecognized configuration setting").
  - The positive control therefore could not fire: an untrusted hook does not run in app-server. A real turn without `--disable hooks` wrote no marker.
- `hooks/list` comparison:
  - Without the flag: 1 hook (`C:\<session-flags>\config.toml:stop:0:0`, enabled).
  - With the agent's real `launch_args("http://127.0.0.1:9")` (contains `--disable hooks`): 0 hooks.
- With the agent's args, a real turn (`thread/start` + `turn/start`, 「OK とだけ答えて」) completed and wrote no marker.

### F4, F5
- Throwaway HTTP MCP server: `MCPServer("cb")` with `streamable_http_app(json_response=True, stateless_http=True)` on `127.0.0.1:47811`, wrapped in an ASGI logger.
- Project Stop hook in `.codex/hooks.json`: `& "<python>" "<stoplog.py>"`.
- Run: `codex exec --json --ephemeral -m gpt-6-luna -c model_reasoning_effort=low -s danger-full-access --dangerously-bypass-hook-trust -c projects.'<dir>'.trust_level="trusted" -c mcp_servers.cb.url="http://127.0.0.1:47811/mcp?client=codex" -c mcp_servers.cb.default_tools_approval_mode="approve" "<call send_task once>" < /dev/null`.
- Requests: `initialize`, `notifications/initialized`, `tools/list`, `tools/call`. All four had query `client=codex` (F5).
- Headers: only `user-agent: codex-mcp-client/0.167.0`, `mcp-protocol-version`, `accept`, `content-type`. No session header.
- `tools/call` `params._meta` (trimmed):
  ```json
  {"callId": "exec-…", "x-codex-turn-metadata": {"session_id": "01a1ed4e-2bd0-…", "thread_id": "01a1ed4e-2bd0-…", "turn_id": "01a1ed4e-2dbe-…", "model": "gpt-6-luna", …},
   "threadId": "01a1ed4e-2bd0-7442-a77a-45b1c5da8718", "sessionId": "01a1ed4e-2bd0-7442-a77a-45b1c5da8718", "windowId": "…:0", "itemId": "…", "progressToken": 1}
  ```
- The Stop hook stdin had `"session_id":"01a1ed4e-2bd0-7442-a77a-45b1c5da8718"`, which equals `_meta.sessionId` (and `threadId`). `initialize` / `tools/list` carry no session.

### F6
- `ThreadResumeParams.json`: `"model": {"type": ["string","null"], "description": "Configuration overrides for the resumed thread, if any."}`.

### F9 (Codex), F10 (Codex)
- **Codex on Windows runs hook commands through PowerShell.** One run with four Stop hooks in project `.codex/hooks.json`:
  - A: `C:/…/hook_a.cmd` (no quotes, no space) → ran.
  - B: `"C:/…/with space デスク/hook_b.cmd"` (quoted path alone) → did **not** run. PowerShell treats it as a string.
  - C: `"<python>" "<mark.py>" C` → did **not** run.
  - D: `& "<python>" "<mark.py>" D` → ran.
- `& "C:/…/with space デスク/hook_ascii.cmd"` and `& "…/hook_utf8.cmd"` both ran.
  - The hook printed `{"decision":"block","reason":"次の文を…: 日本語テスト: 完了"}`, once as `ensure_ascii=True` and once as raw UTF-8 bytes.
  - In both cases the model's next message was `知らせ: 日本語テスト: 完了`.
- Claude Code ran the quoted form without `&` (see F1). `& "…"` is not valid in its shell, so the two tools need different forms.

### F11
- `model/list` with `includeHidden: true`: 1 page, 12 entries, `id == model` for all.
- Hidden: `gpt-reserve` and `codex-auto-review`. Default: `gpt-6.2-sol` (`isDefault: true`).
- Efforts are `low…ultra` for most entries and `low…max` for luna and the hidden ones. `defaultReasoningEffort` is `low` or `medium`.
- One entry (trimmed): `{"id":"gpt-6.2-sol","model":"gpt-6.2-sol","displayName":"GPT-6.2-Sol","hidden":false,"supportedReasoningEfforts":[{"reasoningEffort":"low","description":"…"},{"reasoningEffort":"medium","description":"…"}, …],"defaultReasoningEffort":"low","isDefault":true, …}`.

## Consequences
- F1/F2 ACCEPTED → Task 9/10 keep `HOOK_TIMEOUT = 86400`.
- F2: a broken hooks.json shows only in `hooks/list` **warnings**. Anything that checks a written hooks.json must not rely on `errors` alone.
- F3 DISABLE_HOOKS_WORKS → Task 3 keeps `--disable hooks` and needs no extra args. Also, untrusted hooks never run in app-server.
- F4 META sessionId → Task 8 reads `params._meta["sessionId"]` of `tools/call` as `caller_session`. Task 9 sends `session = payload["session_id"]`. Task 7 filters Codex notices by session. Task 12 does not say that two Codex conversations may get each other's notices.
- F5 QUERY_KEPT → Task 8/10 use `?client=codex`. No `/claude-codex` mount.
- F6 YES → Task 3 `thread_resume` may pass `model`.
- F7 NOT_FIRED → a failed `send_task` never wakes Claude through PostToolUse. Task 9's "no task id" path stays only as a guard.
- F8 MATCHES → Task 10 uses matcher `mcp__codex-bridge__.*`.
- F9 → Task 10 `hook_command` builds different forms:
  - Claude: `"<exe>" hook claude --port N` (quoted, `/` separators).
  - Codex: `& "<exe>" hook codex --port N`. PowerShell needs the call operator.
  - The source (non-frozen) form follows the same rule: `"<python>" -m codex_bridge hook claude …` for Claude, and `& "<python>" -m codex_bridge hook codex …` for Codex.
- F10 UTF8_OK → Task 9 writes UTF-8 bytes. Codex JSON with `ensure_ascii=True` also works and stays the plan's choice.
- F11 ID_EQUALS_MODEL → Task 3 uses `e["model"]`, one page in practice, but keeps the cursor loop. Hidden entries exist and are dropped.
- Codex hooks need trust: a hook that install-mcp adds stays `untrusted` until the user trusts it with `/hooks` (Task 12 docs).
