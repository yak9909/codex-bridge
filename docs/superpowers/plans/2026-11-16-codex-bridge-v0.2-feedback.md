# codex-bridge v0.2 利用者フィードバック対応 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** v0.2 のリリース前に、利用者が画面を触って出した指摘（画面の使いにくさ・招待の入れ直し・走査の遅さ・規則の詰まり・exe が 2 つあること）をすべて直す。

**Architecture:** 既存の feat/v0.2 ブランチ（HEAD 2677798）の上に積む。走査は「パスを 1 件ずつ解決する」のをやめ、リンクの所だけ解決する。画面は共通部品（common/web/ui.js）を直し、管理画面と貸す人の画面で同じ形にする。exe は 1 つ（`codex-bridge.exe`）にし、起動時のダイアログ（.bat からは引数）で役割を分ける。貸す側は 1 つの画面で接続先を複数持ち、作業は全体で 1 つずつ行う。

**Tech Stack:** Python 3.12 / FastAPI / uvicorn / asyncio、素の JS（共通部品 ui.js）、PyInstaller、Windows（NTFS のジャンクション、DPAPI）、ripgrep（vendor/rg.exe）

**Spec:** 設計書 `docs/2026-11-14-codex-bridge-v2-design.md`（変わらない部分の正本）＋この計画の「Requirements（利用者の指摘 2026-11-16）」節（今回の変更の正本。設計書と食い違えばこちらが勝つ）

## Requirements（利用者の指摘 2026-11-16、ほぼ原文）

- R1 管理画面: 「タスクの詳細」が広すぎ、表示されすぎ。展開やスクロールなど使って短くする。
- R2 管理画面: 招待コードのコピーが、コピーできているか分からない。
- R3 貸している人の画面: 使用量のバーと使用量の上限が分けられていて使いにくい。管理画面と同じにする（バーのすぐそばで上限を見て変えられる）。
- R4 貸している人の画面: 承認の待ち時間を「無し」（期限なし）にできる。
- R5 貸している人の画面: 招待コードを画面から入れ直せる（失効したときに exe の再設定が要らない）。
- R6 exe は 1 つにまとめる（利用者の決定）。貸す側と使う側は、起動時のダイアログで選べる（利用者の指示）。
- R11 貸す側は 1 つの exe・1 つの画面のまま、画面から接続先（招待コード）を複数追加できる（利用者の指示。複数起動はしない）。
  - 上限（使用量の上限と強制停止）は全体で共通（同じ Codex の利用枠のため）。
  - 使える機能の制限・事前承認・セッションの使い捨て・作業ログの見せ方は、接続先ごとに分ける。
  - 作業は全体で同時に 1 つだけ。ほかの接続先の作業中は、依頼主の host に「作業中」と伝わり、自動の振り分けから外れる。
  - 保存場所・作業フォルダ・記録の保管日数は全体で共通。
- R7 フォルダの走査を必ず速くする。目標: 5000 ファイルで 0.3 秒以内、10 万ファイルで 3 秒以内（この PC で計測。v0.2 の修正前は 5000 ファイル 3.36 秒、修正後 9.76 秒）。
- R8 「一時停止などができなくなる状態」は、画面にはっきり出す（そして直せるようにする）。
- R9 画面は撮影して見ながら作る。各画面の作業は、撮影画像の目視確認が終わるまで完了にしない。
- R10 管理者の権限が要るファイル（例: `C:\Program Files` の中、ほかのユーザーのファイル）を共有したとき: Windows が断った操作は、平易な日本語で Codex に返し、監査の記録にも残す。管理画面でフォルダを「書ける」で登録するとき、普通の権限で書けないフォルダなら、その場で知らせる。（今は `codex_mcp.run` が `ToolError` しか受け止めず、Windows の「アクセス拒否」は生のエラーのまま返り、記録にも残らない）

## Global Constraints

- v0.1 の exe との接続は互換のまま残す（v2 設計書の全体の決まり）。
- host のデータフォルダ（`config.data_dir`）とインストール先のファイルは、どの道具からも見えない・書けない（最終検査 C1 の修正を弱めない）。
- 利用者に見せる文は平易な日本語。画面の言葉は管理画面と貸す人の画面でそろえる。
- 改行は LF。テストは `uv run --no-sync pytest tests -q`。
- リポジトリの `host.yaml` と `data/` は利用者の本物。試験や撮影で使わない。

## Review Focus

1. 大きなフォルダ（数万ファイル、深い入れ子、ジャンクションやシンボリックリンクを含む）でも走査が速く、リンクの先がデータフォルダや共有外を指していれば今までどおり見えない → Task 1 のテスト（リンク経由の拒否）と計測。
2. 招待コードを入れ直す・接続先を外すとき、その接続先で作業中のタスクや承認待ちがあれば断る（途中で接続先が変わって記録が迷子にならない）→ Task 5 のテスト。
3. 承認の待ち時間「無し」のまま exe を終了・再接続しても、承認待ちが消えずに送り直される → Task 4 のテスト（v0.2 の Review Focus 4 と同じ動き）。
4. 2 つの接続先から同時に依頼が届いても、始まるのは 1 つだけで、もう一方の依頼主には「別の依頼主の作業中」と伝わり、作業中のまま固まらない。接続先ごとの事前承認・機能の制限・作業ログが混ざらない → Task 5 のテスト。
5. 規則が詰まった状態（データフォルダを含むフォルダが規則に残っている）でも、一時停止・コマンド実行の切り替え・そのフォルダを外す操作はできる → Task 2 のテスト。
6. 1 つにまとめた exe を、ダブルクリック（ダイアログ）と .bat（使う側）の両方で起動したとき、使う側のコマンドの文字がコンソールに出る。v0.2 までの貸す側の設定と招待コードが、最初の接続先としてそのまま残る → Task 5・6 のテストと Task 7 の確認。

---

### Task 1: フォルダの走査を速くする（R7）

**Files:**
- Modify: `src/codex_bridge/host/paths.py`（`level_of` / `visible` / `discoverable` と、`os.walk` を使う走査）
- Modify: `src/codex_bridge/host/fs_read.py:92` 付近（find_files の走査）、`src/codex_bridge/host/runner.py:180-197`（`_write_area_state`）、`src/codex_bridge/host/search.py`（grep の対象の列挙）
- Create: `scripts/bench_walk.py`（計測用。テストではない）
- Test: `tests/host/test_walk_speed.py`

**Interfaces:**
- Produces: `SafePaths.walk(root: Path, need: Literal["read","write"]) -> Iterator[tuple[Path, os.DirEntry]]` — 見えるファイルだけを返す走査。find_files・grep の対象列挙・`_write_area_state` はすべてこれを使う。

- [ ] **Step 1: 失敗するテスト** — `tests/host/test_walk_speed.py`
  - `test_walk_resolves_only_links`: 2000 ファイル・20 フォルダの木を作り、`Path.resolve` / `os.path.realpath` の呼び出し回数を数える（monkeypatch）。走査 1 回で、呼び出しは「共有フォルダの数 ＋ リンク（ジャンクション・シンボリックリンク）の数」以下。
  - `test_walk_hides_link_into_data_dir`: 共有フォルダの中にデータフォルダを指すジャンクション（Windows のみ、`mklink /J`）とシンボリックリンク（作れなければ skip）を置き、`walk` の結果にも find_files・grep の結果にもデータフォルダの中身が出ない。
  - `test_walk_same_results_as_before`: 入れ子・hidden・読むだけ・`.codex-bridge` を含む木で、`walk` の結果が今の find_files の結果（相対・絶対の表示を含む）と同じ。
- [ ] **Step 2: 失敗を確かめる** — `uv run --no-sync pytest tests/host/test_walk_speed.py -q` → `walk` が無いので失敗。
- [ ] **Step 3: 実装**
  - `os.scandir` で再帰し、親フォルダの「実際のパス」と「規則の段階（レベル）」を子に引き継ぐ。`DirEntry.is_symlink()` か `is_junction()`（Python 3.12）のときだけ `resolve` して、行き先で判定し直す。普通のエントリは文字列の連結だけで判定する。
  - データフォルダとインストール先の判定は、正規化した文字列の前方一致（区切り単位）を前もって作っておき、1 件ごとの `resolve` をしない。
  - grep は、`rg` に対象のフォルダと除外（hidden・データフォルダ・`.codex-bridge`）を `--glob` と引数で渡し、Python 側でファイルを 1 件ずつ列挙しない。rg が使えないときだけ `walk` に戻る。
  - `_write_area_state` は `DirEntry.stat()`（Windows では追加のシステムコールなし）で大きさと更新時刻を取る。
- [ ] **Step 4: 通過を確かめる** — 上のテストと既存の `tests/host` が全部通る。
- [ ] **Step 5: 計測** — `uv run --no-sync python scripts/bench_walk.py --files 5000` と `--files 100000`（一時フォルダに木を作り、find_files・grep・`_write_area_state` の時間を出して消す）。**5000 ファイル 0.3 秒以内、10 万ファイル 3 秒以内**。出力を報告に貼る。届かなければ BLOCKED で止めて理由を書く。
- [ ] **Step 6: Commit** — `perf(host): walk shared folders without resolving every entry`

### Task 2: 規則が詰まった状態を直せるようにし、画面に出す（R8）。Windows に断られた操作を平易に伝える（R10）

**Files:**
- Modify: `src/codex_bridge/host/policy.py:290-312`（`save_text` / `update` の検査）、`src/codex_bridge/host/policy_form.py:151`、`src/codex_bridge/host/share.py`
- Modify: `src/codex_bridge/host/web/app.py`（overview に `policy_problem`）、`src/codex_bridge/host/web/static/app.js` / `index.html`（警告の帯）
- Modify: `src/codex_bridge/host/claude_mcp.py`（get_overview にも同じ値）
- Test: `tests/host/test_policy_v2.py`, `tests/host/test_web.py`

**Interfaces:**
- Produces: `PolicyStore.problems() -> list[dict]`（`{"folder": str, "reason": "contains_data_dir"}` の一覧。空なら問題なし）。overview の JSON に `policy_problem: list[dict]`。

- [ ] **Step 1: 失敗するテスト**
  - `test_pause_and_exec_mode_work_with_a_bad_folder_stored`: データフォルダの親を規則に直接書いた状態で、`update(paused=True)`・`update(exec_mode="off")` が通る。
  - `test_bad_folder_can_be_removed_or_hidden`: その状態で、そのフォルダを外す（remove_shared_folder・管理画面のフォルダ削除）と、`hidden` にする操作が通る。
  - `test_adding_a_folder_that_contains_data_dir_is_still_refused`: 新しく read / write で足す操作は今までどおり拒否（最終検査 C1 を弱めない）。
  - `test_overview_reports_policy_problem`: overview の `policy_problem` にそのフォルダが出る。外したあとは空。
- [ ] **Step 2: 失敗を確かめる**
- [ ] **Step 3: 実装** — 検査は「新しく足す・変える・段階を上げる（read / write にする）フォルダ」だけにする。外す・hidden にする・規則以外の値（paused、exec_mode）の変更は検査しない。
- [ ] **Step 4: 画面** — 管理画面の上部の帯の下に、`policy_problem` があるときだけ赤い帯を出す。文: 「共有フォルダの設定に、codex-bridge 自身のデータ（設定と記録）を含むフォルダがあります: <フォルダ>。このままでは安全のため一部の操作ができないことがあります。」と「このフォルダを共有から外す」ボタン（押すと確認のあと外す）。
- [ ] **Step 5: 通過を確かめ、撮影** — 問題がある状態とない状態の管理画面を Edge のヘッドレスで撮る（撮影の決まりは Task 3 の Step 5）。
- [ ] **Step 6: Windows に断られた操作（R10）**
  - テスト `test_permission_denied_is_reported_plainly`（`tests/host/test_codex_mcp.py`）: 書き込み先のファイルを読み取り専用属性にするか、`open` を monkeypatch して `PermissionError(13, ...)` を出させ、write_file・edit_file・delete_path・move_path の結果が `isError` で、文に「Windows がこの操作を断りました（管理者の権限が要る場所か、ほかのプログラムが使用中の可能性があります）: <パス>」が入り、監査の記録に `ok=False` で残る。取り消し用の保存に、書けなかったファイルの記録が残らない。
  - 実装: `codex_mcp.run` で `PermissionError` を受け止めて上の文にする。ほかの `OSError` も「ファイルの操作に失敗しました: <理由>」として受け止め、記録する。
  - テスト `test_register_unwritable_folder_warns`（`tests/host/test_web.py`）: 普通の権限で書けないフォルダ（テストでは書き込み可否の確かめ方を monkeypatch）を「書ける」で登録すると、応答に `warning` が入る。管理画面はそれを黄色の帯で「このフォルダは、今のユーザーの権限では書き込めません。Codex が書こうとしても Windows に断られます。」と出す（登録そのものは止めない）。書き込み可否は、フォルダに一時ファイルを作って消すことで確かめる。
- [ ] **Step 7: Commit** — `fix(host): a stored folder holding the data dir no longer blocks pause, and is shown; access denied is reported plainly`

### Task 3: 管理画面のタスク詳細を短くし、招待コードのコピーが分かるようにする（R1, R2, R9）

**Files:**
- Modify: `src/codex_bridge/common/web/ui.js`（`UI.taskDetail`）、`src/codex_bridge/common/web/ui.css`
- Modify: `src/codex_bridge/host/web/static/app.js:372-395`（招待コード）、`index.html`, `style.css`
- Test: `tests/host/test_web.py`（静的ファイルの文言の確認だけ。見た目は撮影で確かめる）

**Interfaces:**
- Consumes: `UI.taskDetail(el, task, opts)`（既存。呼び出し側の変更が要らない形で直す）

- [ ] **Step 1: タスク詳細を短くする**（共通部品なので貸す人の画面にも効く）
  - 詳細の欄全体の高さの上限を画面の高さに合わせ（例: `max-height: calc(100vh - 160px)`）、中をスクロールにする。一覧の欄と同じ高さに並ぶ。
  - 依頼文と最終報告は 4 行で切り、「すべて表示」で展開する。
  - 「作業中の様子」は作業中のときだけ開いた状態、それ以外は閉じた `<details>`。
  - 記録は最新 20 件だけ出し、「さらに表示」で 20 件ずつ足す。記録の欄は高さの上限つきでスクロール。
- [ ] **Step 2: 招待コードのコピー** — 押したら、ボタンの文字を 2 秒間「コピーしました ✓」に変える。`navigator.clipboard` が使えない・失敗したときは、コードを選択状態にして「選択しました。Ctrl+C でコピーしてください」と出す。
- [ ] **Step 3: 文言のテスト** — `test_web.py` に、配信される app.js に「コピーしました」が入っていることの確認を足す。
- [ ] **Step 4: 通過を確かめる**
- [ ] **Step 5: 撮影と目視（R9。これが終わるまで完了にしない）**
  - 偽のデータで管理画面を起動し（リポジトリの host.yaml・data/ は使わない。一時フォルダの設定）、Edge のヘッドレスで 1400×900 と 1400×3000 を撮る。状態: 記録 60 件・長い依頼文・長い最終報告のタスクを選んだところ、展開したところ、コピーを押した直後。
  - 撮影画像を自分で見て、次を確かめて報告に書く: タスク詳細が 1 画面の高さに収まる／一覧と横に並ぶ／展開で全文が読める／コピーの合図が見える。直したら撮り直す。
  - 撮影画像は `.superpowers/sdd/2026-11-16-feedback/shots/task-3/` に置く（リポジトリには入れない）。
- [ ] **Step 6: Commit** — `fix(ui): shorter task detail; invite copy shows it worked`

### Task 4: 貸している人の画面 — 使用量と上限を 1 か所に、承認の待ち時間「無し」（R3, R4, R9）

**Files:**
- Modify: `src/codex_bridge/agent/static/index.html`, `app.js`, `settings.js`, `style.css`
- Modify: `src/codex_bridge/agent/settings.py:48,86`（`timeout_min` に None を許す）、`src/codex_bridge/agent/pre_approval.py:67,106`（None なら期限なし）
- Test: `tests/agent/test_settings.py`, `tests/agent/test_pre_approval.py`

- [ ] **Step 1: 失敗するテスト**
  - `test_timeout_none_is_allowed_and_default_stays_30`: `update({"pre_approval": {"timeout_min": None}})` が通る。既定は 30 のまま。0 や負は今までどおり拒否。
  - `test_no_timeout_means_never_expires`: 待ち時間が None の承認待ちは、時計を 1 年進めても期限切れにならない。
  - `test_no_timeout_pending_is_resent_after_reconnect`: その承認待ちが再接続のあと `hello` の後に送り直される。
- [ ] **Step 2: 失敗を確かめる**
- [ ] **Step 3: 実装** — `timeout_min` の検証を「None か 1〜MAX_TIMEOUT_MIN」に。期限の計算は None なら行わない。
- [ ] **Step 4: 画面**
  - 使用量の欄を管理画面と同じ形にする: 各枠のバーのすぐ右に「あなたの上限 [ 80 ] %」の入力、欄の下に「超えたらすぐ強制的に止める」と「保存」。設定の欄から上限の入力は消す（二重に置かない）。バーの線（依頼主の線・あなたの線）は今のまま。
  - 事前承認の待ち時間: 「[ 30 ] 分」の入力の隣に「待ち時間なし（期限なしで待つ）」のチェック。チェックすると入力を無効にし、None を保存。
- [ ] **Step 5: 通過を確かめる**
- [ ] **Step 6: 撮影と目視（R9。これが終わるまで完了にしない）** — Task 3 の Step 5 と同じ決まり。状態: 通常（バーと上限の入力が並ぶ）、上限超え、待ち時間なしにチェック。管理画面の「Codex を貸している人」の欄の撮影と並べて、バーと上限の並びが同じ考え方になっていることを確かめて報告に書く。撮影画像は `.superpowers/sdd/2026-11-16-feedback/shots/task-4/`。
- [ ] **Step 7: Commit** — `feat(agent): usage and limits side by side; approval can wait without a time limit`

### Task 5: 貸す側の画面から接続先（招待コード）を追加・入れ直し・外す（R5, R11, R9）

**Files:**
- Create: `src/codex_bridge/agent/connections.py`（接続先の一覧の保存と読み込み）
- Modify: `src/codex_bridge/agent/main.py`（接続先ごとに `ControlClient` と Codex の app-server を持つ）、`src/codex_bridge/agent/runner.py` と `pre_approval.py`（作業は全体で 1 つ。接続先ごとの設定を使う）、`src/codex_bridge/agent/settings.py`（共通の設定と接続先ごとの設定に分ける）、`src/codex_bridge/agent/setup.py`（invite.bin を接続先ごとに）、`src/codex_bridge/agent/control_client.py`、`src/codex_bridge/agent/ui.py`、`src/codex_bridge/agent/ui_tasks.py`、`src/codex_bridge/agent/worklog.py`（記録に接続先を持たせる）、`src/codex_bridge/agent/static/*`
- Modify: `src/codex_bridge/host/tasks.py`, `src/codex_bridge/host/lenders.py`（貸す人が「ほかの依頼主の作業中」のとき振り分けない）、`src/codex_bridge/common/protocol.py`
- Test: `tests/agent/test_connections.py`, `tests/agent/test_ui.py`, `tests/host/test_lenders.py`, `tests/integration/test_host_agent.py`

**Interfaces:**
- Produces:
  - `Connection(id: str, invite: Invite, settings: dict)` — `id` は追加のときに作る短いランダムな文字列（パスに使える文字だけ）。`settings` は接続先ごとの部分: `features`、`pre_approval`、`disposable`、`log_visibility`。
  - 共通の設定（`AgentSettings` に残すもの）: `limits`、`workdir`、`log_retention_days`。保存場所の移動も共通。
  - `ConnectionStore(home: Path)`: `list() -> list[Connection]`、`add(code: str) -> Connection`（読めないコードは ValueError、同じ接続先（URL と名前が同じ）がすでにあれば ValueError）、`replace_invite(id, code) -> Connection`、`remove(id) -> None`、`update_settings(id, patch) -> dict`（今の `SettingsStore.update` と同じ検証）。保存は `<保存場所>/connections/<id>/`（`invite.bin` は DPAPI、`settings.json`）。
  - 移行: 起動時、`<保存場所>/invite.bin` があって `connections/` がなければ、それと今の設定の接続先ごとの部分を最初の接続先に移す（元の invite.bin は移したあとに消す。途中で失敗したら元のまま）。
  - 作業は全体で 1 つ: どれかの接続先で作業中・承認待ちのあいだは、ほかの接続先から来た `task_start` / `task_followup` を `task_state rejected_by_lender`（report「貸している人は、ほかの依頼主の作業をしています。あとでもう一度頼むか、ほかの人に頼んでください。」）で断る。あわせて、作業の始まりと終わりで、ほかの接続先に `lender_status {busy_elsewhere: bool}` を送る。
  - host: `lender_status` を受けたら、その人を `busy_elsewhere` の間は自動の振り分けから外し、名前を指定した依頼には今の「作業中」と同じ答えを返す。v0.1 の exe は送ってこないので何も変わらない。管理画面の「Codex を貸している人」の表では、その人の作業中のタスクの列に「ほかの依頼主の作業中」と出す。
  - 貸す側の API（書き込みは `X-Codex-Bridge: 1` が必須）: `GET /api/connections` → `[{id, host_name, connected, invite_problem: null|"rejected", busy}]`、`POST /api/connections {code}` → 201／400、`PUT /api/connections/{id}/invite {code}` → 200／400／409（その接続先で作業中・承認待ち）、`DELETE /api/connections/{id}` → 200／409（同じ）、`GET/PUT /api/connections/{id}/settings`。タスク・承認待ち・セッションの API の応答に `connection_id` と `host_name` を足す。
  - 招待が無効（接続が 401/403 で断られている）とき、その接続先の `invite_problem` を "rejected" にし、再接続は控えめに続ける（今と同じ待ち時間の伸ばし方）。

- [ ] **Step 1: 失敗するテスト**
  - `test_add_two_connections_and_both_connect`: 2 つの偽の host に 2 つの招待コードで接続先を足すと、両方に `hello` が届く。
  - `test_only_one_task_at_a_time`: A から作業が始まっている間に B から `task_start` が来ると、B に `rejected_by_lender`（上の report）が返り、B に `lender_status {busy_elsewhere: true}` が届いている。A の作業が終わると B に `busy_elsewhere: false`。承認待ちのあいだも同じ。
  - `test_settings_are_per_connection_but_limits_are_shared`: A だけ事前承認をオンにすると、A の依頼は承認待ちに、B の依頼はすぐ始まる。上限を変えると両方に効く。機能の制限が接続先ごとに Codex の起動の引数に入る。
  - `test_replace_invite_and_remove_refused_while_that_connection_is_busy`: 409。ほかの接続先が作業中でも、自分が作業中でなければ入れ直し・外しはできる。
  - `test_rejected_invite_is_reported`: 接続が 403 で断られている接続先の `invite_problem` が "rejected"。新しいコードを入れると "rejected" が消えてつながる。
  - `test_migrates_single_invite_to_first_connection`: v0.2 までの `invite.bin` と設定がある保存場所で起動すると、最初の接続先として同じ招待と設定が残り、作業ログも見える。
  - `test_worklog_and_sessions_show_their_connection`: タスク一覧・詳細・セッションに `host_name` が入る。
  - host: `test_busy_elsewhere_lender_is_skipped_by_auto_routing` と `test_named_busy_elsewhere_lender_gets_busy_answer`（`tests/host/test_lenders.py`）。
- [ ] **Step 2: 失敗を確かめる**
- [ ] **Step 3: 実装（貸す側の中身）** — 接続先ごとに `ControlClient` と Codex の app-server（接続先の URL と合言葉を引数に持つため）を持つ。app-server は依頼が来たときに起動し、しばらく使わなければ止めてよい。作業中かどうかの判定と 1 つずつの制御は 1 か所（runner のロック）にまとめる。
- [ ] **Step 4: 実装（host）** — `lender_status` の受け取りと振り分けからの除外。`protocol.py` に型を足す。
- [ ] **Step 5: 画面**
  - 「今の状態」の欄を「接続先」の欄にする。接続先ごとに 1 行: 依頼主（接続先のアドレス）、接続状態、作業中か、「設定」「招待コードを入れ直す」「外す」。欄の下に「接続先を追加」（招待コードを貼る欄と「追加」ボタン）。
  - `invite_problem` が "rejected" の行は赤くし、「招待コードが無効になっています。依頼主から新しいコードをもらって入れ直してください。」と、入れ直しの欄を最初から開いて出す。
  - 接続先の「設定」を押すと、その接続先の設定（使える機能の制限・事前承認・セッションの使い捨て・道具の記録の見せ方）が開く。共通の設定（保存場所・作業フォルダ・記録の保管日数）は今の設定の欄に残し、上限は使用量の欄（Task 4）に置く。
  - タスク一覧・承認待ちの欄・セッションの一覧に、どの依頼主のものかを出す。接続先で絞り込める。
- [ ] **Step 6: 通過を確かめる** — 全テスト。
- [ ] **Step 7: 撮影と目視（R9。これが終わるまで完了にしない）** — Task 3 の Step 5 と同じ決まり。状態: 接続先 2 つ（片方作業中）、招待が無効の行、接続先の設定を開いたところ、接続先を追加する欄。撮影画像は `.superpowers/sdd/2026-11-16-feedback/shots/task-5/`。
- [ ] **Step 8: 文書** — `docs/friend-guide.md` に、接続先の追加・入れ直し・外し方、設定のどれが共通でどれが接続先ごとか、作業は同時に 1 つであることを書く。`docs/operation-guide.md` に、貸す人がほかの依頼主の作業中のときの見え方を書く。
- [ ] **Step 9: Commit** — `feat(agent): several hosts from one lender screen; one task at a time`

### Task 6: exe を 1 つにまとめ、起動時のダイアログで役割を選ぶ（R6）

**Files:**
- Create: `src/codex_bridge/entry.py`（役割の振り分け）、`src/codex_bridge/launcher.py`（起動時のダイアログ。tkinter。今の `agent.main.ask_invite` と同じ作り）、`scripts/codex_bridge_entry.py`, `codex-bridge.spec`
- Delete: `scripts/host_entry.py`, `scripts/agent_entry.py`, `codex-bridge-host.spec`, `codex-bridge-agent.spec`
- Modify: `scripts/make_bats.py`（と生成される `setup.bat` `start.bat` `stop.bat` `install-mcp.bat`）、`scripts/build_release.py`, `pyproject.toml`（`[project.scripts]` に `codex-bridge` を足す。開発用の `codex-bridge-host` / `codex-bridge-agent` は残す）
- Modify: `README.md`, `docs/operation-guide.md`, `docs/friend-guide.md`（exe の名前と起動のダイアログ）
- Test: `tests/test_entry.py`

**Interfaces:**
- Produces:
  - `codex_bridge.entry.main(argv: list[str]) -> int` — `argv` が空 → 起動のダイアログを出し、選ばれた役割で動く（キャンセルなら 0）。先頭が `host` → 残りを `host.main.main` に渡す（.bat 用。ダイアログを出さない）。先頭が `agent` → 貸す側をダイアログなしで動かす。それ以外 → 使い方を出して 2。
  - `codex_bridge.launcher.choose_role() -> Literal["host", "agent"] | None` — ダイアログに 2 つの大きなボタン: 「使う側 — 自分の PC のフォルダを、ほかの人の Codex に使ってもらう」「貸す側 — 自分の Codex を、ほかの人に使ってもらう」。閉じたら None。
  - 貸す側を選んだとき、すでに貸す側が動いていれば（今の単一起動の印で分かる）、新しく起動せずその画面（8766）をブラウザで開く。
  - 使う側を選んだとき: exe の隣に `host.yaml` がなければ `setup` を、あれば `start` を、コンソールの窓を開いて（`AllocConsole`）動かす。

- [ ] **Step 1: 失敗するテスト** — `tests/test_entry.py`: 引数なし → `choose_role` が呼ばれ、"host" なら host（`host.yaml` の有無で setup / start）、"agent" なら agent、None なら 0／`host start --config host.yaml` → host の main が `["start", "--config", "host.yaml"]` で呼ばれ、ダイアログは出ない／`agent` → ダイアログなしで agent／`foo` → 2。
- [ ] **Step 2: 実装** — exe はウィンドウなし（`console=False`）で作る。`host` のときは `ctypes.windll.kernel32.AttachConsole(-1)`（失敗したら `AllocConsole()`）でコンソールにつなぎ、`sys.stdout` / `sys.stderr` / `sys.stdin` を `CONOUT$` / `CONIN$` で開き直す。
- [ ] **Step 3: spec とビルド** — `codex-bridge.spec` 1 つ（両方の静的ファイルと hiddenimports を合わせる）。`build_release.py` は admin の zip に `codex-bridge.exe`＋.bat＋`vendor/rg.exe`＋docs、friend の zip に `codex-bridge.exe`＋`使い方.md`。.bat は `"%~dp0codex-bridge.exe" host <cmd> --config host.yaml`。
- [ ] **Step 4: 文書** — 3 つの文書の exe の名前と起動のしかたを直す（貸す人は「codex-bridge.exe をダブルクリックして『貸す側』」、使う人は .bat か『使う側』）。
- [ ] **Step 5: 通過を確かめ、ダイアログを撮影** — 全テスト。起動のダイアログをスクリーンショットで撮り、目で確かめる（`.superpowers/sdd/2026-11-16-feedback/shots/task-6/`）。
- [ ] **Step 6: Commit** — `build: one codex-bridge.exe with a start-up role dialog`

### Task 7: 本物での確認と撮影（Review Focus 1〜6、R9）

このタスクは実装ではなく確認。コードは直さず、失敗は記録する。

- [ ] **Step 1:** `uv run --no-sync python scripts/build_release.py`（前に `release/` の中身を一時フォルダへ退避し、あとで戻す）。zip を一時フォルダに展開し、`codex-bridge.exe` を引数なしで起動 → 起動のダイアログが出る。「貸す側」でトレイと 8766 の画面、もう一度起動して「貸す側」を選ぶと二重起動せず画面が開く。`setup.bat` / `start.bat` / `stop.bat` で使う側が動き、コンソールに文字が出る。貸す側の保存場所は試験用（`CODEX_BRIDGE_AGENT_HOME`）にし、利用者の本物の貸す側の設定に触らない。
- [ ] **Step 2:** 試験用の host を 2 つ（一時フォルダの host.yaml、別のポート）と貸す側 1 つで、貸す側の画面から接続先を 2 つ足す。片方の host から依頼 → 作業中に、もう片方の host の管理画面で「ほかの依頼主の作業中」と出て、自動の振り分けで選ばれない。片方の招待を失効 → 貸す側の画面でその行が赤くなる → 新しいコードを入れ直す → つながる。
- [ ] **Step 3:** 片方の接続先だけ事前承認・待ち時間なしにして依頼 → 長く待っても期限切れにならない → 許可で始まる。もう片方の接続先の依頼はすぐ始まる（作業が空いているとき）。
- [ ] **Step 4:** 2 万ファイル以上の共有フォルダで find_files・grep・コマンド実行の前後の走査の時間を測る。
- [ ] **Step 5:** 管理画面と貸す人の画面を撮り、Requirements R1〜R5・R8・R11 を目で確かめる。撮影画像は `.superpowers/sdd/2026-11-16-feedback/shots/task-7/`。
- [ ] **Step 6:** 起動したものをすべて止め、`git status` が空で、`release/` に v0.1.0 が残っていることを確かめる。
