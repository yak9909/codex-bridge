# codex-bridge v0.2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 複数の共有フォルダ（絶対パス）・複数の使わせる側と振り分け・使わせる側の画面と設定（上限・機能の制限・事前承認・使い捨て・保存場所）・Codex の作業ログ・両画面の共通化を実装し、v0.2.0 としてリリースする。

**Architecture:** host は「許可フォルダの一覧」で権限を判定し、使わせる側ごとの接続（招待コードの名前で識別）を持ってタスクを振り分ける。agent は設定ストアに従って app-server の起動引数とスレッドの開始方法（ephemeral）を決め、app-server の通知からタスクごとの作業ログを作って保存・送信する。画面の部品（CSS/JS）は `codex_bridge/common/web/` に置き、host と agent の両方が配信する。

**Tech Stack:** v0.1 と同じ（Python 3.12・uv・mcp 2.x・FastAPI・uvicorn・websockets・PyInstaller・素の HTML/CSS/JS）。

**Spec:** `docs/2026-11-14-codex-bridge-v2-design.md`（v0.1 の設計は `docs/2026-11-14-codex-bridge-design.md`）

## Global Constraints

- 既存の 272 件のテストを壊さない。v0.1 の動き（相対パス・v0.1 の exe との接続）は互換のまま残す。
- テストは `uv run pytest`。本番の host が動いていて `.venv` の exe が使用中なら `uv run --no-sync pytest`。
- 画面の言葉: 使う側＝「依頼主」、使わせる側＝「Codex を貸している人」（画面の中）。手順書では「使う側／使わせる側」。
- 既定値: 使わせる側の上限 80%／80%・強制停止オフ、機能の制限（Web 検索・画像生成・サブエージェント・目標）はすべて止める、事前承認オフ（待ち時間 30 分）、使い捨てオフ、作業ログの保管 30 日、`progress` の送信は 2 秒に 1 回まで。
- 通信の互換: `hello` に `protocol: 2` と `capabilities` がなければ v0.1 の exe とみなす（続きあり・事前承認なし）。
- 使わせる側の PC で、使う側から変えられる値は増やさない（設定は使わせる側の画面だけで変える）。
- Windows の改行: 新しいファイルは LF。既存ファイルの改行コードを変えない（スクリプトで書き換えるときは `read_bytes`/`write_bytes`）。

## Review Focus

1. **絶対パスの抜け道**: 登録フォルダの前方一致で別のフォルダに一致する（`D:/work` を登録して `D:/workspace` が見える）→ パスの区切りで判定する。（Task 2 にテスト）
2. **複数の使わせる側の取り違え**: A の Codex の道具の呼び出しが B のタスクの取り消し用保存・承認待ちに入る → 合言葉ごとにタスクを結び付ける。（Task 5 にテスト）
3. **使い捨てのセッションの続き**: 使い捨ての人への `continue_task_id` が送られてしまう → host が接続時の `capabilities` で先に断る。（Task 4・9 にテスト）
4. **事前承認待ちのまま接続が切れる／exe を終了する** → 再接続時に `awaiting_lender` を送り直すか、拒否扱いにして使う側が固まらない。（Task 10 にテスト）
5. **保存場所の変更中の失敗**（移動先に書けない・途中で失敗）→ 元の場所のまま動き続ける。（Task 9 にテスト）

---

## ファイルの構成（新規・主な変更）

```
src/codex_bridge/
  common/web/                      # 共通の画面部品（Task 7）
    ui.css, ui.js                  #   帯・カード・使用量のバー・状態の色札・承認の欄・タスク一覧＋詳細・説明つき選択肢
  host/
    policy.py                      # v2: folders（絶対パス）と判定（Task 1）
    policy_migrate.py              # v1→v2 の変換（Task 1）
    paths.py                       # 絶対・相対の解決、表示用のパス、登録フォルダの一覧（Task 2）
    fs_read.py, search.py, runner.py, fs_write.py, patch.py   # 複数フォルダ対応（Task 2）
    policy_form.py                 # v2 のフォーム（Task 3）
    lenders.py                     # 使わせる側ごとの状態と振り分け（Task 4）
    tasks.py                       # 複数の接続・タスクの担当・通し番号の判定（Task 4）
    codex_mcp.py, mcp_util.py      # 合言葉ごとの現在のタスク（Task 5）
    claude_mcp.py, web/app.py, web/static/*   # 道具・API・画面（Task 3・6・7）
  agent/
    settings.py                    # 使わせる側の設定ストア（Task 9）
    worklog.py                     # 作業ログの記録・保管（Task 8）
    runner.py, appserver.py        # 通知の取り込み・ephemeral・機能の制限・事前承認（Task 8・9・10）
    logview.py → ui.py             # 使わせる側の画面と API（Task 11）
    main.py, setup.py              # 保存場所・作業フォルダ（Task 9）
```

---

### Task 1: 確かめる（設計 §7）と、権限の規則 v2

**Files:**
- Create: `src/codex_bridge/host/policy_migrate.py`, `tests/host/test_policy_v2.py`
- Modify: `src/codex_bridge/host/policy.py`

**Interfaces:**
- Produces:
  - `Folder(path: Path, level: Level)`、`Policy.folders: tuple[Folder, ...]`、`Policy.main: Path | None`（最初の `read`/`write` のフォルダ）
  - `Policy.level_for_abs(p: Path) -> Level | None`（どのフォルダにも含まれなければ `None`）
  - `Policy.level_for(rel: str)` は v0.1 互換（メインのフォルダからの相対）
  - `migrate_v1(raw: dict, share_root: Path) -> dict`（v2 の辞書を返す）
  - `PolicyStore` は v1 の内容を読んだら v2 に変換して保存し直す（`share_root` は `HostConfig` から渡す）

- [ ] **Step 1: 本物で確かめる（コードは書かない）** — 次を `scratchpad` のスクリプトで調べ、結果を ledger に書く:
  - Web 検索・画像生成・サブエージェント・目標を止める設定の名前（`codex features list`、`-c web_search="disabled"` など）と、それらを止めても bridge の `list_dir` が呼べること（v0.1 の `code_mode_host` の件と同じ確かめ方）
  - `ephemeral: true` のスレッドで `turn/steer` と 2 回目の `turn/start` が使えること
  - 一般ユーザーで `C:\ProgramData\codex-bridge-agent\work-xxxx` が作れて書き込めること
- [ ] **Step 2: 失敗するテストを書く**

```python
def test_deepest_folder_wins_and_hidden_always_wins(tmp_path): ...
    # folders: D/work write, D/work/vendor read, D/work/secret hidden, D/tools read
    # level_for_abs(D/work/a)=write, (D/work/vendor/x)=read, (D/work/secret/x)=hidden, (D/tools/x)=read
def test_prefix_is_not_a_parent(tmp_path):        # D/work 登録で D/workspace/x は None
def test_outside_all_folders_is_none(tmp_path):
def test_relative_uses_main_folder(tmp_path):      # level_for("a/b") == level_for_abs(main/"a/b")
def test_migrate_v1(tmp_path):
    # share_root + default read + rules [{raw/** write},{private hidden}] →
    # folders [{share_root, read}, {share_root/raw, write}, {share_root/private, hidden}]、glob 規則は advanced_rules
def test_store_converts_v1_file_on_load(tmp_path): # 読み込み後のファイルに folders: がある
```

- [ ] **Step 3: 失敗を確かめる** — `uv run pytest tests/host/test_policy_v2.py -q` → FAIL
- [ ] **Step 4: 実装する** — 判定は `Path.resolve()` 後の `normcase` で、`parts` の前方一致（文字列の前方一致は使わない）。`hidden` は祖先も含め常に優先。`advanced_rules`（ワイルドカード）はメインのフォルダからの相対として v0.1 と同じに扱う。
- [ ] **Step 5: 通ることを確かめる** — 同上 → PASS、全体も PASS
- [ ] **Step 6: Commit** — `feat(host): policy v2 with absolute folders and v1 migration`

---

### Task 2: 複数フォルダでのパスの解決と道具

**Files:**
- Modify: `host/paths.py`, `host/fs_read.py`, `host/search.py`, `host/runner.py`, `host/fs_write.py`, `host/patch.py`
- Test: `tests/host/test_multi_folder.py`（`conftest.py` に 2 つ目の共有フォルダを持つ fixture `multi`）

**Interfaces:**
- Consumes: Task 1 の `Policy.level_for_abs`、`Policy.main`
- Produces:
  - `SafePaths.resolve(path: str, need) -> Path`（絶対パス可。相対はメインから）
  - `SafePaths.display(p: Path) -> str`（メインの中は相対、それ以外は絶対の `/` 区切り）
  - `SafePaths.roots() -> list[dict]`（`path, level, main`。`hidden` は出さない）
  - `ReadTools.list_dir("/")` は `roots()` を返す。`find_files`・`grep` はパス省略時にすべての見えるフォルダ
  - `SafePaths.set_root` は廃止し、`Policy.main` に従う（`change_share_root` は Task 3 で置き換え）

- [ ] **Step 1: 失敗するテストを書く**

```python
def test_absolute_path_in_second_folder_is_readable(multi):
def test_second_folder_read_only_refuses_write(multi):       # 「書き込みは許可されていません」
def test_not_registered_absolute_path_is_not_found(multi):   # メッセージにパスのみ（他の情報なし）
def test_prefix_sibling_folder_not_visible(multi):           # Review Focus 1
def test_list_root_shows_registered_folders(multi):          # hidden は出ない
def test_grep_and_find_cover_all_folders(multi):             # 結果のパス: メインは相対、他は絶対
def test_run_command_cwd_in_second_folder(multi):
def test_move_between_folders_respects_levels(multi):        # write→read のフォルダへは不可
def test_relative_paths_still_work(share):                   # v0.1 の share fixture のまま通る（既存テストも全部）
```

- [ ] **Step 2〜4: 失敗→実装→通過** — `uv run pytest tests/host -q`
- [ ] **Step 5: Commit** — `feat(host): tools work across multiple absolute shared folders`

---

### Task 3: 共有フォルダの管理（API・道具・フォーム）

**Files:**
- Modify: `host/policy_form.py`, `host/share.py`, `host/claude_mcp.py`, `host/web/app.py`, `host/main.py`（`SAMPLE_POLICY` を v2 に）
- Test: `tests/host/test_policy_form.py`, `tests/host/test_web.py`, `tests/host/test_claude_mcp.py`

**Interfaces:**
- Produces:
  - フォーム v2: `{"folders": [{"path","level"}], "advanced_rules": [...], "exec": {...}, "delete_needs_approval", "paused"}`
  - `add_folder(store, path, level)`, `remove_folder(store, path)`, `set_main_folder(store, path)`, `set_folder_level(store, path, level)`（絶対・相対どちらも）
  - API: `GET/PUT /admin/api/policy-form`（v2）、`POST /admin/api/folders/add {path, level}`、`POST /admin/api/folders/remove {path}`、`POST /admin/api/folders/main {path}`、`GET /admin/api/folders?path=<絶対>`（パス省略で登録フォルダの一覧）
  - 道具: `add_shared_folder(path, level)`, `remove_shared_folder(path)`, `set_main_folder(path)`、`set_folder_level` は絶対パス可、`set_share_root(path)` は `add_shared_folder(path,"write")`＋`set_main_folder(path)` の別名
  - 作業中のタスクがあるときの追加・削除・メイン変更は 409／ToolError（v0.1 と同じ）
  - データフォルダを含むフォルダの登録は拒否（v0.1 と同じ）

- [ ] **Step 1〜4: テスト（追加・削除・メイン変更・データフォルダの拒否・作業中の拒否・別名）→ 実装 → 通過**
- [ ] **Step 5: Commit** — `feat(host): manage multiple shared folders from the admin API and agents`

---

### Task 4: 複数の使わせる側と振り分け

**Files:**
- Create: `src/codex_bridge/host/lenders.py`, `tests/host/test_lenders.py`
- Modify: `host/tasks.py`, `host/control.py`, `host/tokens.py`（名前で照合して返す `match(token) -> str | None`）

**Interfaces:**
- Produces:
  - `Lender(name, send, connected, agent_version, codex_version, auth_mode, protocol, capabilities, limits, usage, current_task_id, last_assigned)`
  - `pick_lender(lenders, thresholds) -> Lender | None`（設計 §3.3 の余裕の計算）
  - `TaskManager.attach(name: str, send) -> int`、`on_agent_message(link_id, msg)`、`detach(link_id)`（名前ごとに接続を持つ。通し番号の判定は名前ごと）
  - `TaskManager.start(prompt, continue_task_id=None, lender: str | None = None) -> str`（`TaskRecord.lender` を記録）
  - `TaskManager.current_task_id(lender: str | None = None)`、`TaskManager.lenders() -> list[dict]`、`usage()` は使わせる側ごと
  - 続きの依頼は同じ使わせる側へ。いない・作業中・`capabilities.resumable == False` は ToolError（「前回の報告を添えて新しく依頼してください」）
  - `control.py` は合言葉から名前を引いて `attach(name, send)`

- [ ] **Step 1: 失敗するテストを書く**

```python
def test_pick_most_headroom(): ...          # A: 5h 70/上限 80、B: 5h 20/上限 80 → B
def test_pick_uses_lower_of_host_and_friend_limits():
def test_pick_skips_busy_offline_blocked():
def test_unknown_usage_goes_last():
async def test_two_lenders_work_in_parallel(mgr):
async def test_seq_dedupe_is_per_lender(mgr):        # 同じ seq でも別の人なら両方受け取る
async def test_continue_goes_to_same_lender(mgr):
async def test_continue_refused_for_disposable(mgr):  # capabilities.resumable False
async def test_v1_agent_without_capabilities_is_resumable(mgr):
```

- [ ] **Step 2〜4: 失敗→実装→通過**（既存の `test_tasks.py` は「名前 friend の 1 人」として通るように調整）
- [ ] **Step 5: Commit** — `feat(host): several lenders with headroom-based routing`

---

### Task 5: Codex の道具の呼び出しを、使わせる側のタスクに結び付ける

**Files:**
- Modify: `host/mcp_util.py`（`BearerAuth` が照合できた名前を `scope["state"]["lender"]` に入れる）, `host/codex_mcp.py`, `host/app.py`, `host/fs_write.py`, `host/runner.py`, `host/approvals.py`
- Test: `tests/host/test_codex_mcp.py`

**Interfaces:**
- Produces: `contextvars.ContextVar` `current_lender`（各道具の呼び出しの間だけ設定）、`task_id_fn = lambda: mgr.current_task_id(current_lender.get())`

- [ ] **Step 1: 失敗するテスト** — A と B の 2 つの合言葉で同時に書き込み → 取り消し用保存がそれぞれのタスクに入る／承認待ちの `task_id` が正しい（Review Focus 2）
- [ ] **Step 2〜4: 失敗→実装→通過**
- [ ] **Step 5: Commit** — `feat(host): attribute Codex tool calls to the calling lender's task`

---

### Task 6: 使わせる側の一覧・送り先（道具と API）

**Files:**
- Modify: `host/claude_mcp.py`（`list_lenders`、`send_task(..., lender)`、`get_usage` 使わせる側ごと、`GUIDE` の更新）, `host/web/app.py`（`GET /admin/api/lenders`、`POST /admin/api/tasks` に `lender`）
- Test: `tests/host/test_claude_mcp.py`, `tests/host/test_web.py`

- [ ] **Step 1〜4: テスト → 実装 → 通過**
- [ ] **Step 5: Commit** — `feat(host): list lenders and choose where a task goes`

---

### Task 7: 共通の画面部品と、管理画面の作り直し

**Files:**
- Create: `src/codex_bridge/common/web/ui.css`, `src/codex_bridge/common/web/ui.js`
- Modify: `host/web/app.py`（`/admin/common/` に共通部品を配信）, `host/web/static/index.html`, `app.js`, `style.css`（共通部品に置き換え）, `codex-bridge-host.spec`・`codex-bridge-agent.spec`（`common/web` を datas に）

**Interfaces（`ui.js` が公開する関数）:**
- `UI.meter(el, {used_percent, resets_at_local}, marks: [{value, label, kind}])`
- `UI.stateBadge(state) -> html`（`STATE_JA` と色を一元化。`awaiting_lender`／`rejected_by_lender` を含む）
- `UI.approvals(el, items, onDecide)`
- `UI.taskList(el, tasks, selectedId, onSelect)`
- `UI.taskDetail(el, task, {mode: "simple"|"advanced", onFollowup?, onCancel?, onUndo?, showLender?})`（作業中の欄：最新の `plan` と `progress`、記録：種類で絞り込み・検索）
- `UI.choiceCards(el, name, options, value, onChange)`

- [ ] **Step 1: 部品ごとの表示確認ページ** `common/web/demo.html`（固定の見本データで全部品を並べる。テストではなく目視用）
- [ ] **Step 2: 管理画面を部品に置き換え、使わせる側の表（Task 6 の API）と送り先の選択を加える**
- [ ] **Step 3: 既存の `tests/host/test_web.py` が通ること＋ `/admin/common/ui.js` が 200**
- [ ] **Step 4: Edge のヘッドレスで撮影して目視（タスク詳細の簡易／詳細、使わせる側の表）**
- [ ] **Step 5: Commit** — `feat(ui): shared UI parts; admin page rebuilt on them`

---

### Task 8: Codex の作業ログ

**Files:**
- Create: `src/codex_bridge/agent/worklog.py`, `tests/agent/test_worklog.py`
- Modify: `agent/runner.py`（通知の取り込み）, `host/tasks.py`（新しい `kind` の保存）, `tests/fakes/fake_appserver.py`（通知の台本）

**Interfaces:**
- Produces:
  - `WorkLog(dir: Path, retention_days=30)`: `add(task_id, kind, data)`, `task_events(task_id) -> list`, `tasks() -> list[dict]`, `prune()`, `clear()`
  - runner が作る `event` の `kind`: `plan`（`{steps: [{step, status}]}`）、`reasoning_summary`（`{text}`）、`tool_started`／`tool_finished`（`{tool, arguments, duration_ms, success, result_summary}`）、`token_usage`（`{input, output}`）、`progress`（`{text}`、送信は 2 秒に 1 回までにまとめ、記録しない）、既存の `agent_message`・`status`・`turn_completed`
- [ ] **Step 1: 失敗するテスト** — 偽の app-server に `turn/plan/updated`・reasoning の `item/completed`・`item/started`→`item/completed`（mcpToolCall）・`thread/tokenUsage/updated`・`item/agentMessage/delta` を流し、`WorkLog` と送信内容を確かめる。`progress` が 2 秒に 1 回までにまとめられること。保管期間を過ぎた記録が `prune()` で消えること。
- [ ] **Step 2: 本物で確かめる** — 本物の app-server の `turn/plan/updated`・`thread/tokenUsage/updated`・reasoning の通知の形を 1 回記録し、偽の台本と合わせる
- [ ] **Step 3〜4: 実装→通過**
- [ ] **Step 5: Commit** — `feat(agent): record Codex progress per task and send it to the host`

---

### Task 9: 使わせる側の設定（上限・機能の制限・使い捨て・保存場所・作業フォルダ）

**Files:**
- Create: `src/codex_bridge/agent/settings.py`, `tests/agent/test_settings.py`
- Modify: `agent/appserver.py`（機能の制限の引数、`thread_start(..., ephemeral)`）, `agent/runner.py`（使い捨ての後始末、`hello` の `capabilities`）, `agent/main.py`, `agent/setup.py`（`data_root()` が `location.txt` を読む）, `agent/logview.py`（`LimitsStore` を `settings` に統合）

**Interfaces:**
- Produces:
  - `AgentSettings`（JSON）: `limits {five_hour=80, weekly=80, force=False}`、`features {web_search=False, image_generation=False, multi_agent=False, goals=False}`、`pre_approval {enabled=False, timeout_min=30}`、`disposable=False`、`workdir: str | None`、`log_retention_days=30`
  - `SettingsStore(path)`: `get() -> dict`, `update(patch: dict) -> dict`（検証つき）
  - `launch_args(host_url, disabled_mcp, features: dict)`（Task 1 Step 1 で確かめた設定名で止める）
  - `AppServer.thread_start(instructions, ephemeral: bool)`
  - `default_workdir() -> Path`（`C:\ProgramData\codex-bridge-agent\work-<8文字>`、作れなければ保存場所の `workdir`）
  - `move_data_root(new: Path) -> None`（コピー→確認→`location.txt` 更新→元を削除。失敗したら元のまま。Review Focus 5）
  - `hello` に `protocol: 2`, `capabilities: {resumable: not disposable, pre_approval}`, `limits`
  - 設定の変更で app-server の引数が変わるときは、作業中でなければ起動し直す（作業中なら終わってから）
- [ ] **Step 1: 失敗するテスト** — 既定値、検証（上限 1〜100）、`launch_args` に機能の制限が入る、使い捨てで `ephemeral: true`＋完了後に一覧から消える、使い捨てで続きを頼まれたら `failed`、保存場所の移動の成功と失敗時のロールバック、`hello` の `capabilities`
- [ ] **Step 2〜4: 失敗→実装→通過**
- [ ] **Step 5: Commit** — `feat(agent): lender settings (limits, features, disposable, data location, workdir)`

---

### Task 10: 事前承認（B）

**Files:**
- Modify: `agent/runner.py`, `host/tasks.py`, `common/protocol.py`（`awaiting_lender`, `rejected_by_lender`）
- Test: `tests/agent/test_runner.py`, `tests/host/test_tasks.py`, `tests/integration/test_host_agent.py`

**Interfaces:**
- Produces:
  - `AgentRunner.pending_approval -> dict | None`（`task_id, prompt, kind: start|followup, created`）、`decide_pending(approve: bool, reason: str = "")`
  - オンのとき `task_start`・`task_followup` を保留し、`task_state awaiting_lender` を送る。許可で通常どおり開始、拒否・期限切れで `rejected_by_lender`（`report` に理由）
  - 接続が切れて戻ったときは、`hello` のあとに `awaiting_lender` を送り直す（Review Focus 4）。exe の終了時は保留中を拒否として送る
  - host は `awaiting_lender` を作業中の一種として扱う（同じ人への新しい依頼は断る）
- [ ] **Step 1〜4: テスト → 実装 → 通過**
- [ ] **Step 5: Commit** — `feat: lender pre-approval of requests`

---

### Task 11: 使わせる側の画面

**Files:**
- Create: `src/codex_bridge/agent/ui.py`（`logview.py` を置き換え）, `src/codex_bridge/agent/static/index.html`, `app.js`, `tests/agent/test_ui.py`
- Modify: `agent/main.py`, `codex-bridge-agent.spec`

**Interfaces（127.0.0.1 のみ・Host 検査・書き込みは `X-Codex-Bridge: 1`）:**
- `GET /api/overview`（接続先の名前・接続状態・状態・版・使用量・効いている上限）
- `GET /api/tasks`, `GET /api/tasks/{id}`（`WorkLog` から。表示範囲は設計 §4.9）
- `GET /api/pending`, `POST /api/pending {approve, reason}`
- `GET /api/sessions`（`thread_id, created, task_ids, active, disposable`）, `DELETE /api/sessions/{thread_id}`（作業中は 409）
- `GET /api/settings`, `PUT /api/settings`, `POST /api/settings/data-root {path}`, `POST /api/logs/clear`
- `POST /api/pause {paused}`
- 画面は設計 §4.1 の 6 つの欄を共通部品で並べる。事前承認待ちが出たらトレイの通知とブラウザの通知
- [ ] **Step 1〜4: API のテスト → 実装 → 通過**
- [ ] **Step 5: 撮影して目視**（作業中・事前承認待ち・セッション一覧・設定）
- [ ] **Step 6: Commit** — `feat(agent): lender screen on the shared UI parts`

---

### Task 12: 通しの確認・手順書・リリース

**Files:**
- Modify: `docs/operation-guide.md`, `docs/friend-guide.md`, `README.md`, `scripts/build_release.py`（版を 0.2.0 に）, `pyproject.toml`

- [ ] **Step 1: 本物で通しの確認**（この PC で host と exe を 2 つ動かす。exe はそれぞれ別の `CODEX_BRIDGE_AGENT_HOME` と招待コード）: 振り分け、並行作業、続きの依頼、使い捨て、事前承認、機能の制限（Web 検索が使えないこと）、作業ログが両方の画面に出ること、複数の共有フォルダへの絶対パスでの読み書き
- [ ] **Step 2: Claude Code と Codex に一文で頼む試験**（v0.1 と同じ）
- [ ] **Step 3: 手順書を更新**（複数の使わせる側、共有フォルダの追加、使わせる側の画面と設定）
- [ ] **Step 4: `uv run python scripts/build_release.py`、zip を展開して起動・停止を確認**
- [ ] **Step 5: main へまとめて push、`gh release create v0.2.0`**
