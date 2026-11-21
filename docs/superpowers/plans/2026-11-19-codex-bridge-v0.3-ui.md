# codex-bridge v0.3 — one resident app and the redesigned screen — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Double-clicking `codex-bridge.exe` starts one tray-resident app whose browser screen has the tabs 「使う」「貸す」「ログ」 and looks and behaves like `design/mock-app.html`; the settings behind the screen are moved to where the redesign puts them.

**Architecture:** A new `resident.py` owns the tray icon, one local UI server (127.0.0.1:8766) and two roles: the borrower's host (started in-process when `host.yaml` exists) and the lender's agent runtime (always running, possibly with zero connections). The UI server mounts the existing lender API under `/lend` and the existing admin router under `/admin`, plus a small `/app/api` (state, pause, logs, host setup). The page is one static app in `common/web/app/` built from `design/ui.css` and the mock's HTML; the old host and agent pages are removed. Backend changes (per-lender delete approval and log visibility, no borrower thresholds, two pre-approval switches, the lender's settings mirrored to the borrower) come first so the page has data.

**Tech Stack:** Python 3.12 (uv), FastAPI/uvicorn, pystray + Pillow, websockets, pytest + pytest-asyncio; plain JS (ES modules, no build step, CSP `script-src 'self'`).

**Spec:** `docs/superpowers/specs/2026-11-18-ui-redesign.md` — sections 0.4 (overrides everything), 0.7, 0.8 and the dated revisions 1–6 under 0.8. The look is `design/mock-app.html` + `design/ui.css` + `design/ui.js` (read them; they are the reference, not a sketch). Where the mock and older sections (1–6) disagree, the mock wins.

**Who does what (user's instruction 2026-11-19: Claude models, not Codex):** Tasks 1–8 are implemented by **Claude subagents (Sonnet 5.5)**, each reading only its own task brief, and reviewed by **opus**. The implementer runs the focused tests of its task and commits with the task's message plus:

```
Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QdRVgBF6N47dnhLcCKY5UF
```

The **controller** runs the full suite (`uv run --no-sync pytest tests -q -p no:cacheprovider`, must pass) after each task and takes the screenshots.

Task 9 is done by the controller.

## Global Constraints

- Every user-facing string is Japanese in the existing plain tone. Code, identifiers and comments stay English.
- Words (spec §2): 依頼主 = borrower (runs the host), 貸す人 = lender (runs the agent), 依頼, 会話, 承認待ち, 上限, コマンド実行. Never 知人・使わせる側 on the screen.
- The look follows spec 0.4 exactly: type 14/20 body, 12/16 caption, 10/14 meter helper only, 14 bold names, 16 bold pane titles, nothing ≥ 20px; space 4/8/12/16 by role; controls 32px / 24px small. Copy `design/ui.css` and `design/ui.js` into the product and change them only by adding rules the product needs; do not restyle.
- Settings apply as soon as they change; no save buttons (multi-line text applies on blur) (spec 0.8 table: ms-app-settings-guidelines).
- Read-only mirrors of the other person's settings use the same control, same content, same heading as the other side, in the read-only state (spec 改訂 2). Never paraphrase them into sentences.
- Exec modes and their labels: `off` 実行しない, `allowlist` 許可リストだけ自動, `ask` 毎回承認, `all` すべて自動. A newly issued invite gets `ask`.
- Log visibility (both sides) keeps its three stored values; the screen shows a combobox with two checkboxes 「道具の名前と対象」「道具の結果」. Mapping: none checked → `summary` (summary 「なし」), 名前と対象 → `files`, both → `all`. Checking 結果 also checks 名前と対象; unchecking 名前と対象 also unchecks 結果.
- Compatibility: a v0.3 host keeps working with a v0.2 / early-v0.3 agent (no `lender_view`: the mirror rows show 「—」). A v0.3 agent keeps working with a v0.2 host (it ignores the host's `thresholds`).
- Local API guards stay: Host header must be loopback; non-GET requests need `X-Codex-Bridge: 1`. The page sends that header on every write.
- New files use LF. Do not change the line endings of existing files.
- Never edit the real `~/.claude*`, `~/.codex`, `%LOCALAPPDATA%\codex-bridge` in tests; use `tmp_path` and `monkeypatch`.
- Out of scope: the release build and zip, `git push`, any release, running the real setup/firewall/UAC, changing MCP tool behavior beyond what a task names.

## Review Focus

1. **Host not configured, or it fails to start (port 8770 or the public bind in use, bad host.yaml):** the 使う tab shows the empty state or the error text, the 貸す tab keeps working, the app does not exit. (Task 4 tests)
2. **Zero lender connections:** the 貸す tab shows the invite-code input; adding the first code resolves Codex (download consent dialog) on a worker thread without freezing the UI server. (Task 4 test)
3. **Mixed versions:** a lender without `lender_view` → host rows have `lender_view: None`, the page shows 「—」; a legacy `pre_approval.enabled: true` in settings.json/connections → both `new_task` and `followup` true. (Task 2 tests)
4. **Second launch:** a second `codex-bridge.exe` opens `http://127.0.0.1:8766/` and exits 0 without starting anything. (Task 4 test)
5. **Removed settings still in old files:** a policy.yaml with a global `exec_mode`, `log_visibility` and `thresholds` in the host's saved settings still load; nothing crashes, existing lenders keep their effective exec mode. (Task 1 tests)

---

### Task 1: Host — per-lender settings, no borrower thresholds, invite reissue

**Files:**
- Modify: `src/codex_bridge/host/policy.py`, `host/fs_write.py:175`, `host/patch.py:204`, `host/tasks.py`, `host/web/app.py`, `host/claude_mcp.py`, `host/tokens.py`, `host/policy_form.py`
- Test: `tests/host/test_policy_v2.py`, `tests/host/test_tasks.py`, `tests/host/test_web.py`, `tests/host/test_claude_mcp.py`

**Interfaces:**
- Produces:
  - `Policy.lender_delete_approval: dict[str, bool]` (policy.yaml key `lender_delete_approval`), `Policy.delete_approval_for(lender: str | None) -> bool` (falls back to `delete_needs_approval`). `fs_write` and `patch` call it with the calling lender (`current_lender.get()` as `CommandRunner` does).
  - `PolicyStore.set_lender_delete_approval(name: str, value: bool) -> None`; `drop_lender` also drops this entry.
  - `TaskManager.set_log_visibility(value: str, lender: str) -> None` (per lender, persisted with the manager's settings; the old single value becomes the default for lenders without one). `_settings_msg(lender)` sends that lender's value and `thresholds={}`.
  - Borrower thresholds are gone: `TaskManager.set_thresholds`, `PUT /admin/api/thresholds`, MCP `set_thresholds` are removed. Routing headroom uses only the lender's own `limits` (no own limit → 100).
  - Global exec mode is no longer set from outside: `PUT /admin/api/exec-mode` is removed; MCP `set_exec_mode` requires `lender` (without it: ToolError `lender を指定してください`). `Policy.exec_mode` stays as the fallback for lenders without their own mode.
  - `POST /admin/api/invites {name}` also sets that lender's exec mode to `ask` when it has none. `POST /admin/api/invites/{name}/reissue` → `{"code": str}` (new token, the old code stops working; 404 for an unknown name).
  - `PUT /admin/api/lenders/{name}/delete-approval {"value": bool}`, `PUT /admin/api/lenders/{name}/log-visibility {"value": "summary"|"files"|"all"}` (404 unknown name, 400 bad value). `PUT /admin/api/log-visibility` is removed; MCP `set_log_visibility(value, lender)` requires `lender`.
  - `GET /admin/api/lenders` rows gain `delete_approval: bool`, `log_visibility: str`, `invite_created: float | None` (from `TokenStore.list()`), and `exec_mode` becomes the **effective** mode (`policy.exec_mode_for(name)`), never `None`.
  - Per-lender log visibility is stored in the existing `data_dir/settings.json` of `TaskManager` as `lender_log_visibility: {name: value}`; the old `log_visibility` key is the default for lenders without one. A stored `thresholds` key is ignored.

- [ ] **Step 1: Write the failing tests**
  - `test_delete_approval_per_lender`: policy with `delete_needs_approval: true`, `lender_delete_approval: {yamada: false}` → `delete_approval_for("yamada") is False`, `delete_approval_for("suzuki") is True`, `delete_approval_for(None) is True`; a delete through `WriteTools` as yamada does not create an approval, as suzuki does.
  - `test_drop_lender_drops_delete_approval`.
  - `test_log_visibility_per_lender`: `set_log_visibility("all", "yamada")` → the settings message sent to yamada has `log_visibility == "all"`, to suzuki the default; both have `thresholds == {}`; the value survives a new `TaskManager` on the same data dir.
  - `test_old_saved_thresholds_load`: a saved settings file with `thresholds: {five_hour: 50}` loads, and routing treats a lender at 60% with no own limit as available.
  - `test_invite_sets_ask` / `test_invite_keeps_existing_mode` / `test_reissue_invalidates_old_code` / `test_reissue_unknown_404`.
  - `test_lender_rows_have_new_fields` (`delete_approval`, `log_visibility`, `invite_created`).
  - `test_removed_endpoints_404` for `PUT /admin/api/thresholds`, `PUT /admin/api/exec-mode`, `PUT /admin/api/log-visibility`.
  - `test_mcp_set_exec_mode_needs_lender`, `test_mcp_has_no_set_thresholds`.
  - `test_old_global_exec_mode_still_applies`: policy.yaml with `exec: {mode: all}` and no per-lender modes → `exec_mode_for("yamada") == "all"`.
- [ ] **Step 2: Run them, expect FAIL** — `uv run --no-sync pytest tests/host -q -p no:cacheprovider -k "delete_approval or log_visibility or thresholds or invite or removed_endpoints or set_exec_mode or global_exec"`
- [ ] **Step 3: Implement** the interfaces above. Update the MCP tool descriptions in `claude_mcp.py` that mention thresholds / global exec mode / global log visibility (Japanese, short). `policy_form` round-trips `lender_delete_approval`.
- [ ] **Step 4: Run the focused tests, expect PASS**, then `uv run --no-sync pytest tests/host -q -p no:cacheprovider` (fix tests that relied on removed behavior by changing them to the new behavior, not by keeping the old API).
- [ ] **Step 5: Commit** — `feat(host): per-lender delete approval and log visibility, no borrower thresholds, invite reissue`

### Task 2: The lender's settings mirrored to the borrower; two pre-approval switches; per-connection log clear

**Files:**
- Modify: `src/codex_bridge/agent/settings.py`, `agent/connections.py`, `agent/pre_approval.py`, `agent/runner.py`, `agent/main.py` (where `hello` / `lender_models` are built and sent), `agent/ui.py`, `agent/usage.py`, `common/protocol.py`, `host/tasks.py`
- Test: `tests/agent/test_settings.py`, `tests/agent/test_pre_approval.py`, `tests/agent/test_connections.py`, `tests/agent/test_ui.py`, `tests/agent/test_usage.py`, `tests/host/test_tasks.py`, `tests/common/test_protocol.py`

**Interfaces:**
- Consumes: Task 1's `_settings_msg(lender)` with `thresholds={}`.
- Produces:
  - Per-connection setting `pre_approval = {"new_task": bool, "followup": bool, "timeout_min": int | None}` (defaults false/false/30). A stored `{"enabled": x}` loads as `new_task = followup = x`. `PreApproval.applies(kind: str) -> bool` replaces `enabled`; `runner.py:348/408` call `applies(START)` / `applies(FOLLOWUP)`. `capabilities.pre_approval` = `new_task or followup`.
  - `PROTOCOL = 4`. `hello` and `lender_models` carry `lender_view`:
    `{"features": {web_search, image_generation, multi_agent, goals: bool}, "pre_approval": {new_task, followup, timeout_min}, "resumable": bool, "over_limit": "report" | "stop", "max_effort": str, "models_all": [{"model": str, "allowed": bool, "is_default": bool}] | None}`
    (`over_limit` = `"stop"` when `limits.force`; `models_all` is the whole catalog with `allowed` from `allowed_models`, `None` when the catalog is unknown). `lender_models` is re-sent whenever a connection's settings or the shared limits change.
  - The agent ignores the host's `thresholds`: `UsageGuard` limits are the lender's own only.
  - Host: `normalize_lender_view(raw) -> dict | None` in `host/tasks.py` (anything malformed → `None`, never an exception); `Lender.view`; `/admin/api/lenders` rows gain `lender_view`.
  - Agent UI: `POST /api/connections/{id}/logs/clear` → `{"cleared": int}` (that connection's worklog only; 404 unknown id; 409 while moving data). `GET /api/connections/{id}/settings` returns the new `pre_approval` shape.

- [ ] **Step 1: Write the failing tests**
  - `test_pre_approval_legacy_enabled` (settings.json and connections store, both `true` and `false`), `test_pre_approval_only_followup` (a new task starts at once, a follow-up waits), `test_pre_approval_only_new_task`.
  - `test_hello_has_lender_view` (exact keys above; `models_all` marks a model outside `allowed_models` with `allowed: False`; `over_limit == "stop"` when force), `test_lender_models_resent_on_settings_change`.
  - `test_agent_ignores_host_thresholds`: host settings `thresholds: {five_hour: 10}` with own limit 80 → `effective_limits()["five_hour"] == 80`.
  - Host: `test_lender_view_normalized`, `test_lender_view_missing_is_none` (protocol 3 hello), `test_lender_view_garbage_is_none` (e.g. `"lender_view": "x"`, `models_all: [1]`).
  - `test_clear_one_connection_log` (the other connection's tasks stay), `test_clear_unknown_connection_404`.
- [ ] **Step 2: Run them, expect FAIL** — `uv run --no-sync pytest tests/agent tests/host/test_tasks.py tests/common -q -p no:cacheprovider -k "pre_approval or lender_view or thresholds or clear"`
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Focused tests PASS**, then `uv run --no-sync pytest tests/agent tests/host tests/common tests/integration -q -p no:cacheprovider`.
- [ ] **Step 5: Commit** — `feat: mirror the lender's settings to the borrower, two pre-approval switches, per-connection log clear`

### Task 3: One log for both sides

**Files:**
- Create: `src/codex_bridge/common/applog.py`
- Modify: `host/tasks.py`, `host/approvals.py`, `agent/main.py` (`on_log` in `run_agent`)
- Test: `tests/common/test_applog.py`, plus one call-site test each in `tests/host/test_tasks.py`, `tests/host/test_approvals.py`

**Interfaces:**
- Produces:
  - `class AppLog(path: Path, keep: int = 5000)`: `add(side: Literal["use", "lend"], level: Literal["info", "warn", "error"], text: str) -> None` (thread-safe, appends one JSON line `{"t": epoch float, "side", "level", "text"}`; on construction the file is trimmed to the last `keep` lines); `entries(side: str | None = None, min_level: str | None = None, q: str | None = None, limit: int = 1000) -> list[dict]` (oldest first, the last `limit` matches; `q` is a case-insensitive substring of `text`); `path` attribute.
  - Host: `TaskManager.log` and `Approvals.log`, each `Callable[[str, str], None]` taking `(level, text)`, default no-op. Lines (Japanese): lender connected / disconnected (`{name} とつながりました` / `{name} との接続が切れました`, warn), task sent (`{task_id} を {lender} に送りました`), final state (`{task_id} が終わりました（{state}）`, error for failed), approval requested (`承認待ち: {kind} {target}（{task_id}）`, warn) and decided (`{許可/拒否}: …`).
  - Agent: `run_agent(..., applog: AppLog | None = None)`; its `on_log` writes `error` messages as `error`, `task_state` changes as `info` (`{task_id} を受け付けました` / `… が終わりました`), connection state changes as `warn`.
- [ ] **Step 1: Failing tests** — `test_add_and_filter` (side, min_level warn includes error, q), `test_trim_on_open` (keep=3 after 5 adds → 3 lines), `test_concurrent_adds` (threads, no torn lines), `test_bad_line_skipped`; host: `test_task_sent_logged`, `test_approval_logged`.
- [ ] **Step 2: Run, expect FAIL** — `uv run --no-sync pytest tests/common/test_applog.py tests/host/test_tasks.py tests/host/test_approvals.py -q -p no:cacheprovider -k "applog or logged or add_and or trim or concurrent or bad_line"`
- [ ] **Step 3: Implement.** Do not log task prompts or file contents.
- [ ] **Step 4: Focused tests PASS.**
- [ ] **Step 5: Commit** — `feat: one app log for the borrower and lender sides`

### Task 4: The resident app (tray integration)

**Files:**
- Create: `src/codex_bridge/resident.py`, `src/codex_bridge/host/role.py`, `src/codex_bridge/common/web/app/index.html` (placeholder page with `<h1>codex-bridge</h1>`; Task 5 replaces it)
- Modify: `src/codex_bridge/entry.py`, `src/codex_bridge/agent/main.py`, `src/codex_bridge/host/app.py`, `src/codex_bridge/host/web/app.py`
- Delete: `src/codex_bridge/launcher.py` and `tests/test_launcher.py` (move anything still used elsewhere, e.g. the icon helper, into the module that uses it)
- Test: `tests/test_resident.py`, `tests/test_entry.py`

**Interfaces:**
- Consumes: Task 3 `AppLog`; Task 1/2 APIs unchanged.
- Produces:
  - `host/role.py`: `class HostRole(config_path: Path, applog: AppLog)`: `async start() -> None` (builds the host and serves the public app and the local app on 8770 like `host.main.serve`, without printing; raises `HostStartError(text)` with a Japanese reason when the config is invalid or a port is taken), `async stop() -> None`, `host: Host | None`, `running: bool`, `error: str | None`.
  - `agent/main.py`: `class AgentRole(home: Path, applog: AppLog, notify: Callable[[str, str], None])`: `async start()`, `async stop()`, `ui_app() -> FastAPI` (the existing `create_ui_app(...)` for its runtime), `runtime: AgentConnections`, `paused: bool`, `async set_paused(bool)`. It runs with zero connections. Codex is resolved (`choose_codex` / consent dialog / `download_codex`, all via `asyncio.to_thread`) when the first connection is added or at start when connections exist; until then no runner starts. The startup invite dialog (`ask_invite`) is no longer used by the exe. `run_agent` and its own `Tray` are replaced by `AgentRole` + `resident.Tray`.
  - `resident.py`:
    - `UI_PORT = 8766`; `host_config_path() -> Path` (next to the exe when frozen, else cwd) / `host.yaml`. Overrides for testing a second copy beside a real one (Task 9): env `CODEX_BRIDGE_UI_PORT` (UI port), `CODEX_BRIDGE_HOST_CONFIG` (host.yaml path), `CODEX_BRIDGE_MUTEX` (mutex name); the lender's data already follows `CODEX_BRIDGE_AGENT_HOME`. `AppLog` lives at `<agent home>/logs/app.jsonl`.
    - `create_resident_app(state: "Resident") -> FastAPI`: `/` and `/app/static/*` (from `common/web/app/`), `/lend` → `AgentRole.ui_app()` mounted, `/admin` → `admin_router(host)` when the host runs, else a stub where `GET /admin/api/overview` → `{"configured": bool, "running": false, "error": str | None}` and every other `/admin/api/*` → 503 `{"detail": "使う側は動いていません"}`; `GET /app/api/state` → `{"use": {"configured", "running", "error"}, "lend": {"connections": int}, "paused": bool}`; `POST /app/api/pause {paused: bool}` → both roles (host policy `paused`, agent `set_paused`); `POST /app/api/host/setup` → starts the setup in a new console window (`CREATE_NEW_CONSOLE`, env with `PYINSTALLER_RESET_ENVIRONMENT=1` as `start_again` does; 409 when the host runs or a setup is already running): frozen `[sys.executable, "host", "setup", "--from-app", "--config", path]`, else `[sys.executable, "-m", "codex_bridge", "host", "setup", "--from-app", "--config", path]`. The host role starts when **that process exits with code 0** (the setup writes host.yaml in its step 3, before the firewall step, so the file alone is not the signal); a non-zero exit is logged and shown as the overview `error`. `host setup --from-app` (new flag in `host/main.py` / `setup_cli.run_setup(from_app=True)`) ends with 「初期設定が終わりました。codex-bridge の画面に戻ってください。」 and the steps after it without the start.bat / 「使う側」 lines, then waits for Enter; `GET /app/api/logs?side=&level=&q=&limit=` → `AppLog.entries`; `GET /app/api/logs/export` → `text/plain` attachment `codex-bridge-log.txt` (one line per entry: `MM/DD HH:MM:SS<TAB>使う|貸す<TAB>情報|警告|失敗<TAB>text`); `POST /app/api/logs/open-folder` → `os.startfile(applog.path.parent)`. Same guards as the agent UI (loopback Host, `X-Codex-Bridge: 1` on writes, CSP `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'`).
    - `class Tray`: one pystray icon from `assets/codex-bridge.ico`; left click opens `http://127.0.0.1:8766/`; menu: one disabled state line `貸す人 {n}/{m}・依頼主 {k}/{l}・承認待ち {p}`, 「画面を開く」(default), 「一時停止」/「再開」, 「終了」. Balloon on a new lender pre-approval (existing text) and on a new host approval (`codex-bridge: 承認待ち`, the command or path).
    - `class Resident`: `run() -> bool` (True = relaunch after a data move, as `run_agent` did): starts the UI server, the agent role, the host role when `host.yaml` exists; starts the host after a successful setup (above); `/admin/api/shutdown` and 「終了」 stop everything.
  - `entry.main`: no arguments and `["agent"]` → acquire the single-instance mutex (`Local\codex-bridge-agent`, unchanged name) and run `Resident`; when the mutex is taken, open `http://127.0.0.1:8766/` and return 0. `host …` subcommands and `hook …` are unchanged.
  - The 8770 local app (`create_local_app`): `GET /admin/` redirects (302) to `http://127.0.0.1:<UI port>/` when the host runs inside the resident app (`create_local_app(host, page_url=...)`). In the console way (`codex-bridge.exe host start`) there is no resident: `GET /admin/` returns `common/web/app/index.html` and `/app/static/*` is mounted there. The page then finds `/app/api/state` missing (404) and runs in **console mode** (Task 5): only the 使う tab, pause through `/admin/api/pause`, no 貸す and ログ tabs.
- [ ] **Step 1: Failing tests** (fake roles injected into `Resident` / `create_resident_app`; no real tray, no real Codex):
  - `test_overview_stub_without_host_yaml`, `test_admin_api_503_without_host`, `test_host_start_error_shown` (`HostStartError("ポート 8770 は使われています")` → overview `error` has that text; `/lend/api/connections` still 200; `Resident` keeps running).
  - `test_setup_spawns_console` (monkeypatched `subprocess.Popen` gets `[sys.executable, "-m", "codex_bridge", "host", "setup", "--from-app", "--config", path]`, `CREATE_NEW_CONSOLE`, and `PYINSTALLER_RESET_ENVIRONMENT == "1"` in `env`), `test_setup_409_when_running`, `test_host_starts_after_setup_exit_0`, `test_setup_failure_shown` (exit 1 → host not started, overview `error` set), `test_admin_redirects_to_ui_when_resident`, `test_setup_from_app_text` (`run_setup(..., from_app=True)` output contains 「画面に戻ってください」 and not 「start.bat」; patch the Tailscale/firewall/rg helpers).
  - `test_overrides_from_env` (`CODEX_BRIDGE_UI_PORT`, `CODEX_BRIDGE_HOST_CONFIG`, `CODEX_BRIDGE_MUTEX`).
  - `test_pause_both_roles`, `test_state_counts`.
  - `test_logs_filter_and_export` (export lines use 使う/貸す and 情報/警告/失敗), `test_open_folder_calls_startfile`.
  - `test_guards` (foreign Host 403, POST without header 403, CSP header present).
  - `test_zero_connections_then_add`: adding the first invite code calls the codex resolver through a worker thread (`asyncio.to_thread` monkeypatched to record) and returns 201.
  - `tests/test_entry.py`: `test_no_args_runs_resident`, `test_agent_arg_runs_resident`, `test_second_launch_opens_browser` (mutex taken → `webbrowser.open("http://127.0.0.1:8766/")`, returns 0, `Resident` not built), `test_host_cli_unchanged`.
- [ ] **Step 2: Run, expect FAIL** — `uv run --no-sync pytest tests/test_resident.py tests/test_entry.py -q -p no:cacheprovider`
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Focused tests PASS**, then `uv run --no-sync pytest tests -q -p no:cacheprovider -x --deselect tests/test_launcher.py`.
- [ ] **Step 5: Commit** — `feat: one tray-resident app runs both roles behind one local screen`

### Task 5: The page shell, the shared look, the log tab

**Files:**
- Create in `src/codex_bridge/common/web/app/`: `index.html`, `ui.css` (copy of `design/ui.css` minus the `/* ---- mock page only ---- */` block), `ui.js` (copy of `design/ui.js`), `icons.svg` (copy of `design/icons.svg`), `api.js`, `app.js`, `log.js`
- Modify: `codex-bridge.spec` (add `src/codex_bridge/common/web/app`)
- Test: `tests/test_resident.py`, `tests/host/test_web.py`

**Interfaces:**
- Consumes: Task 4 routes.
- Produces (ES modules, loaded by `<script type="module" src="/app/static/app.js">`; `ui.js` is a classic script in `<head>`):
  - `api.js`: `get(path) -> Promise<any>`, `send(method, path, body?) -> Promise<any>` (adds `X-Codex-Bridge: 1` and JSON; a non-2xx rejects with `Error(detail)`), `onChange(fn)` (SSE `/admin/api/events` when the use side runs, plus a 3 s timer), `showError(text)` (one line in a `.note-warn` bar under the topbar, cleared on the next success), `esc(text) -> string`, `fmtTime(epoch) -> "HH:MM"`, `fmtDay(epoch) -> "MM/DD HH:MM"`, `ago(epoch) -> "3 分前"`.
  - `app.js`: renders the topbar exactly as the mock (brand, tabs 使う/貸す/ログ with `.count` = pending approvals of that side, hidden when 0; `.conn` text `貸す人 {a}/{b}・依頼主 {c}/{d} とつながっています`; 一時停止/再開 button → `POST /app/api/pause`); tab from `location.hash` (`#use` default, `#lend`, `#log`); calls `renderUse(root)`, `renderLend(root)`, `renderLog(root)` from `use.js` / `lend.js` / `log.js` (Tasks 6 and 7 create the first two; until then `app.js` shows an empty pane for them). Console mode (`/app/api/state` is 404, Task 4): only the 使う tab is shown, the pause button uses `/admin/api/pause`, the connection text counts lenders only.
  - `log.js`: the mock's ログ tab: side filter (すべて/使う側/貸す側), level filter (すべて/警告以上), search box, 最新に追従 checkbox, 書き出す (link to `/app/api/logs/export`), フォルダを開く (`POST /app/api/logs/open-folder`); refresh every 2 s.
- [ ] **Step 1: Failing tests** — `test_page_served_on_8766` (`/` 200, contains `/app/static/app.js` and `/app/static/ui.css`, no inline `<script>` body), `test_static_assets` (`ui.css` contains `--fs-cap2`, `ui.js` contains `--sbw`, `icons.svg` contains `i-send`), `test_admin_page_on_8770` (`GET /admin/` on the local app returns the same page), `test_spec_bundles_app_folder`.
- [ ] **Step 2: Run, expect FAIL** — `uv run --no-sync pytest tests/test_resident.py tests/host/test_web.py -q -p no:cacheprovider -k "page_served or static_assets or admin_page or bundles"`
- [ ] **Step 3: Implement.** Markup and class names come from `design/mock-app.html` (`.frame` is the whole window here: `height: 100vh`).
- [ ] **Step 4: Focused tests PASS.**
- [ ] **Step 5: Commit** — `feat(web): page shell, shared look and the log tab`

### Task 6: The 使う tab

**Files:**
- Create: `src/codex_bridge/common/web/app/use.js`
- Test: `tests/test_web_assets.py` (static checks only)

**Interfaces:**
- Consumes: `/admin/api/*` (Task 1 fields, Task 2 `lender_view`), `api.js`.
- Produces: `export function renderUse(root: HTMLElement): void` — the mock's 使う tab, from live data:
  - Empty state when `overview.configured` is false: one block 「この PC で Codex を借りるには、最初に準備が要ります。」 and the button 「準備を始める」 (`POST /app/api/host/setup`). When `error` is set: that text in `.note-warn` and the same button.
  - Left pane 「貸す人」 with count and 「招待」: cards per lender (`/admin/api/lenders`), header = name + state lozenge (作業中 `ok` / 待機中 `idle` / 準備中 `warn` / 未接続 `idle`) + warning badges (`exec_mode == "all"` → 「コマンド: すべて自動」; `capabilities.model_select` false → 「古い版: モデルを選べません」), caption = current task title or 「依頼はありません」(`.cap.low`) / 「最後につながったのは MM/DD HH:MM」; meters with helper `{HH:MM に戻る | MM/DD HH:MM に戻る}・上限 {n}%` and the limit mark. Open card: コマンド実行 select (4 modes), 削除の前に承認する toggle, 相手に見せる記録 combobox with the two checkboxes, 招待コード (発行日, 発行し直す → shows the new code with コピー, 取り消す → confirm), then the group 「{name} さんが決めた設定」 read-only from `lender_view`: 使えるモデル (combobox summary 「既定 {model}」, popover list with ✓ / dimmed / 既定 badge), 推論の強さの上限, 始める前の許可 (summary of checked items or 「なし」 + 分で取り消し), 続きの依頼, Web 検索, 画像の生成, サブエージェント, 目標, 上限を超えたとき (select: 止めて報告させる / すぐ強制終了). `lender_view` null → each value 「—」. Only one card open at a time is not required; open state survives refresh.
  - Invite: 「招待」 opens the mock's 「貸す人を招待」 block at the top of the pane (名前 → 招待コードを発行 → code + コピー; やめる closes).
  - Right area: approvals strip (`/admin/api/approvals`, 許可/拒否), tabs 依頼 / 共有フォルダ / 許可リスト. 依頼: list (`/admin/api/tasks`, newest first) + detail (title, 中止 when active, meta line: state lozenge, lender bold, `model・effort`, switched badge with the reason in `title`, elapsed; the prompt; 経過 timeline from `events` with the mock's icons; composer: follow-up for the selected task, or 「新しい依頼」 with 相手 (自動 + names) / モデル / 推論 selects from the chosen lender's `models`). 共有フォルダ and 許可リスト as the mock, using `/admin/api/folders*`, `/admin/api/pick-folder`, `/admin/api/policy-form`.
  - Re-render keeps scroll positions, open cards, the selected task, and any text being typed.
- [ ] **Step 1: Failing static test** — `test_use_js_exports_renderUse` and `test_use_js_uses_only_existing_routes` (every `/admin/api/...` string in `use.js` matches a route of `admin_router`).
- [ ] **Step 2: Run, expect FAIL** — `uv run --no-sync pytest tests/test_web_assets.py -q -p no:cacheprovider`
- [ ] **Step 3: Implement.**
- [ ] **Step 4: PASS.** The controller then screenshots `http://127.0.0.1:8766/#use` against `design/mock-app.html` (spec 0.5 checklist, 2x zoom) and sends differences back.
- [ ] **Step 5: Commit** — `feat(web): the 使う tab`

### Task 7: The 貸す tab

**Files:**
- Create: `src/codex_bridge/common/web/app/lend.js`
- Test: `tests/test_web_assets.py`

**Interfaces:**
- Consumes: `/lend/api/*` (Task 2 shapes), `api.js`.
- Produces: `export function renderLend(root: HTMLElement): void` — the mock's 貸す tab:
  - Empty state with no connections: 「招待コード」 input + 「つなぐ」 (`POST /lend/api/connections`).
  - Left: 使用量 block (meters from `/lend/api/overview` usage; the `.limits` rows 5時間の上限 / 週間の上限 (number, empty = なし) / 上限を超えたとき select → `PUT /lend/api/settings {limits}`); 依頼主 header with count, 「作業は同時に 1 つ」, 追加 (shows the invite input); one card per connection (`/lend/api/connections`): name = host address, state lozenge (作業中 / 待機中 / つなぎ直し中 / 招待コードが無効), caption; open card = the connection's settings (`/lend/api/connections/{id}/settings`): 使えるモデル (multi-select combobox with search, summary 「{default} ほか {n} 個」), 推論の強さの上限, 始める前の許可 (combobox with checkboxes 新しい依頼 / 追加の指示, plus 分で取り消し number shown only when one is checked), 続きの依頼 (toggle = not `disposable`), Web 検索, 画像の生成, サブエージェント, 目標 (toggles), この画面に出す記録 (combobox, two checkboxes), 記録 (件数 + 記録を消す → `POST /lend/api/connections/{id}/logs/clear`, confirm), つなぎ方 (招待コードを入れ直す / 外す, confirm). Changes save at once with `PUT /lend/api/connections/{id}/settings`.
  - Right: pending pre-approval strip (`/lend/api/pending`, reason input, 許可/拒否 with `created`), tabs 依頼 / この PC. 依頼: list with the 依頼主 filter + detail (same detail as the 使う tab, read-only, no composer). この PC: 作業フォルダ, 記録を残す日数, データの場所 (移す), codex-bridge version, as the mock's rowset.
- [ ] **Step 1: Failing static test** — `test_lend_js_exports_renderLend`, `test_lend_js_uses_only_existing_routes` (against the agent UI app's routes, prefixed `/lend`).
- [ ] **Step 2: Run, expect FAIL** — `uv run --no-sync pytest tests/test_web_assets.py -q -p no:cacheprovider`
- [ ] **Step 3: Implement.**
- [ ] **Step 4: PASS**; controller screenshot check against the mock's 貸す tab.
- [ ] **Step 5: Commit** — `feat(web): the 貸す tab`

### Task 8: Remove the old pages; docs

**Files:**
- Delete: `src/codex_bridge/host/web/static/`, `src/codex_bridge/agent/static/`, `src/codex_bridge/common/web/ui.js`, `ui.css`, `demo.html` (keep `common/web/app/`)
- Modify: `host/web/app.py` (`mount_static` without the old mounts), `agent/ui.py` (no `/` page, no `/static`), `codex-bridge.spec`, `README.md`, `docs/friend-guide.md`, `docs/operation-guide.md`, `scripts/build_release.py` README text (keep its `.format` rules: no `{`/`}`, no new backslashes)
- Test: `tests/host/test_web.py`, `tests/agent/test_ui.py`, `tests/test_release_readme.py`

**Interfaces:** none new.
- [ ] **Step 1:** Replace tests that read the old static files with checks against the new page (or delete them when the behavior moved to Tasks 4–7 tests).
- [ ] **Step 2:** Delete the files and mounts; update docs: double-click the exe → tray; the screen is `http://127.0.0.1:8766/`; first-time 使う preparation from the 使う tab; lenders paste the invite code in the 貸す tab; the role dialog is gone; `start.bat`/`stop.bat` and `codex-bridge.exe host …` still work for the console way.
- [ ] **Step 3:** `uv run --no-sync pytest tests -q -p no:cacheprovider` PASS; `rg -n "host/web/static|agent/static|demo.html" src tests codex-bridge.spec scripts` finds nothing.
- [ ] **Step 4: Commit** — `refactor(web): remove the old host and lender pages; docs for the resident app`

### Task 9: Real-screen check (controller)

- [ ] Run the resident app from source in a scratch folder, never against the user's real `host.yaml`, `data/` or `%LOCALAPPDATA%`: `CODEX_BRIDGE_UI_PORT=18766`, `CODEX_BRIDGE_HOST_CONFIG=<scratch>/host.yaml` (spare `local_port`, `public_bind` 127.0.0.1:<spare>), `CODEX_BRIDGE_MUTEX=Local\codex-bridge-check`, `CODEX_BRIDGE_AGENT_HOME=<scratch>/agent`; connect a second scratch agent home to it with an invite so both tabs have data; screenshot 1280px and 480px at 2x; compare with `design/mock-app.html` using the spec 0.5 checklist and `design/check-layout.js` (`?check&gaps` on the real page); fix differences through a Sonnet follow-up.
- [ ] Build the exe (`uv run --no-sync python scripts/build_release.py` after moving `release/` aside; restore it), start it, confirm: tray icon, left click opens the screen, second launch opens the browser, 終了 quits. Do not run the setup.
- [ ] Final whole-branch review (opus), one fix round, re-review; then report to the user with all `Ruling:` lines.
