# codex-bridge v0.2 利用者フィードバック対応 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** v0.2 のリリース前に、利用者が画面を触って出した指摘（画面の使いにくさ・招待の入れ直し・走査の遅さ・規則の詰まり・exe が 2 つあること）をすべて直す。

**Architecture:** 既存の feat/v0.2 ブランチ（HEAD 2677798）の上に積む。走査は「パスを 1 件ずつ解決する」のをやめ、リンクの所だけ解決する。画面は共通部品（common/web/ui.js）を直し、管理画面と貸す人の画面で同じ形にする。exe は 1 つ（`codex-bridge.exe`）にし、引数で役割を分ける。

**Tech Stack:** Python 3.12 / FastAPI / uvicorn / asyncio、素の JS（共通部品 ui.js）、PyInstaller、Windows（NTFS のジャンクション、DPAPI）、ripgrep（vendor/rg.exe）

**Spec:** 設計書 `docs/2026-11-14-codex-bridge-v2-design.md`（変わらない部分の正本）＋この計画の「Requirements（利用者の指摘 2026-11-16）」節（今回の変更の正本。設計書と食い違えばこちらが勝つ）

## Requirements（利用者の指摘 2026-11-16、ほぼ原文）

- R1 管理画面: 「タスクの詳細」が広すぎ、表示されすぎ。展開やスクロールなど使って短くする。
- R2 管理画面: 招待コードのコピーが、コピーできているか分からない。
- R3 貸している人の画面: 使用量のバーと使用量の上限が分けられていて使いにくい。管理画面と同じにする（バーのすぐそばで上限を見て変えられる）。
- R4 貸している人の画面: 承認の待ち時間を「無し」（期限なし）にできる。
- R5 貸している人の画面: 招待コードを画面から入れ直せる（失効したときに exe の再設定が要らない）。
- R6 exe は 1 つにまとめる（利用者の決定）。貸す側と使う側は、起動時のダイアログで選べる（利用者の指示）。
- R11 貸す側は、1 台の PC で複数同時に起動できる（利用者の指示）。それぞれが自分の招待コード・設定・作業ログ・画面を持つ。
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
2. 招待コードを入れ直したとき、作業中のタスクや承認待ちがあれば断る（途中で接続先が変わって記録が迷子にならない）→ Task 4 のテスト。
3. 承認の待ち時間「無し」のまま exe を終了・再接続しても、承認待ちが消えずに送り直される → Task 4 のテスト（v0.2 の Review Focus 4 と同じ動き）。
4. 1 つにまとめた exe を、ダブルクリック（ダイアログ）と .bat（使う側）の両方で起動したとき、使う側のコマンドの文字がコンソールに出る。貸す側を 2 つ同時に動かしても、設定・招待・作業ログ・画面のポートが混ざらない → Task 5 のテスト（`test_profiles`）と Task 6 の確認。
5. 規則が詰まった状態（データフォルダを含むフォルダが規則に残っている）でも、一時停止・コマンド実行の切り替え・そのフォルダを外す操作はできる → Task 2 のテスト。

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

### Task 4: 貸している人の画面 — 使用量と上限を 1 か所に、待ち時間「無し」、招待の入れ直し（R3, R4, R5, R9）

**Files:**
- Modify: `src/codex_bridge/agent/static/index.html`, `app.js`, `settings.js`, `style.css`
- Modify: `src/codex_bridge/agent/settings.py:48,86`（`timeout_min` に None を許す）、`src/codex_bridge/agent/pre_approval.py:67,106`（None なら期限なし）
- Modify: `src/codex_bridge/agent/ui.py`（`POST /api/invite`、overview に `invite_problem`）、`src/codex_bridge/agent/control_client.py`（接続先の差し替え）、`src/codex_bridge/agent/main.py`（差し替えのとき Codex の app-server を起動し直す）
- Test: `tests/agent/test_settings.py`, `tests/agent/test_pre_approval.py`, `tests/agent/test_ui.py`

**Interfaces:**
- Consumes: `parse_invite(text) -> Invite`（common/invite.py）、`save_invite(directory, invite)`（agent/setup.py）
- Produces: `ControlClient.replace_invite(invite: Invite) -> None`（今の接続を閉じ、新しい接続先で繋ぎ直す）。`POST /api/invite {code: str}` → 200 `{host_name}`／400（読めないコード）／409（作業中・承認待ちあり）。overview に `invite_problem: null | "rejected"`（接続が 401/403 で断られているとき "rejected"）。

- [ ] **Step 1: 失敗するテスト**
  - `test_timeout_none_is_allowed_and_default_stays_30`: `update({"pre_approval": {"timeout_min": None}})` が通る。既定は 30 のまま。0 や負は今までどおり拒否。
  - `test_no_timeout_means_never_expires`: 待ち時間が None の承認待ちは、時計を 1 年進めても期限切れにならない。
  - `test_no_timeout_pending_is_resent_after_reconnect`: その承認待ちが再接続のあと `hello` の後に送り直される。
  - `test_replace_invite_reconnects_and_saves`: `POST /api/invite` で新しいコードを入れると、invite.bin が新しい内容になり、`replace_invite` が呼ばれ、overview の `host_name` が変わる。
  - `test_replace_invite_refused_while_busy`: 作業中または承認待ちのときは 409、何も変わらない。
  - `test_replace_invite_bad_code`: 読めないコードは 400、何も変わらない。
  - `test_invite_problem_shown_when_rejected`: 接続が 403 で断られている間、overview の `invite_problem` が "rejected"。
  - 書き込みは今までどおり `X-Codex-Bridge: 1` が必須。
- [ ] **Step 2: 失敗を確かめる**
- [ ] **Step 3: 実装（設定と承認）** — `timeout_min` の検証を「None か 1〜MAX_TIMEOUT_MIN」に。期限の計算は None なら行わない。
- [ ] **Step 4: 実装（招待の入れ直し）** — 保存 → `replace_invite` → Codex の app-server は接続先の URL と合言葉を引数で持つので、作業中でなければ新しい値で起動し直す。
- [ ] **Step 5: 画面**
  - 使用量の欄を管理画面と同じ形にする: 各枠のバーのすぐ右に「あなたの上限 [ 80 ] %」の入力、欄の下に「超えたらすぐ強制的に止める」と「保存」。設定の欄から上限の入力は消す（二重に置かない）。バーの線（依頼主の線・あなたの線）は今のまま。
  - 事前承認の待ち時間: 「[ 30 ] 分」の入力の隣に「待ち時間なし（期限なしで待つ）」のチェック。チェックすると入力を無効にし、None を保存。
  - 今の状態の欄に「接続先の招待コードを入れ直す」ボタン。押すと入力欄と「つなぎ直す」が開く。`invite_problem` が "rejected" のときは、欄の上に赤い帯「招待コードが無効になっています。依頼主から新しいコードをもらい、ここに入れてください。」を出し、入力欄を最初から開く。
- [ ] **Step 6: 通過を確かめる**
- [ ] **Step 7: 撮影と目視（R9。これが終わるまで完了にしない）** — Task 3 の Step 5 と同じ決まり。状態: 通常（バーと上限の入力が並ぶ）、上限超え、待ち時間なしにチェック、招待が無効の帯、招待の入れ直し欄を開いたところ。管理画面の「Codex を貸している人」の欄の撮影と並べて、バーと上限の並びが同じ考え方になっていることを確かめて報告に書く。撮影画像は `.superpowers/sdd/2026-11-16-feedback/shots/task-4/`。
- [ ] **Step 8: Commit** — `feat(agent): usage and limits together; no-timeout approval; re-enter invite on screen`

### Task 5: exe を 1 つにまとめ、起動時のダイアログで役割を選ぶ。貸す側は複数起動できる（R6, R11）

**Files:**
- Create: `src/codex_bridge/entry.py`（役割の振り分け）、`src/codex_bridge/launcher.py`（起動時のダイアログ。tkinter。今の `agent.main.ask_invite` と同じ作り）、`src/codex_bridge/agent/profiles.py`（貸す側のプロファイル）、`scripts/codex_bridge_entry.py`, `codex-bridge.spec`
- Delete: `scripts/host_entry.py`, `scripts/agent_entry.py`, `codex-bridge-host.spec`, `codex-bridge-agent.spec`
- Modify: `src/codex_bridge/agent/main.py`（プロファイルを受け取る。画面のポートと単一起動の印をプロファイルごとに）、`src/codex_bridge/agent/setup.py`（`data_root()` をプロファイルごとに）、`scripts/make_bats.py`（と生成される `setup.bat` `start.bat` `stop.bat` `install-mcp.bat`）、`scripts/build_release.py`, `pyproject.toml`（`[project.scripts]` に `codex-bridge` を足す。開発用の `codex-bridge-host` / `codex-bridge-agent` は残す）
- Modify: `README.md`, `docs/operation-guide.md`, `docs/friend-guide.md`（exe の名前・起動のダイアログ・複数の貸す側）
- Test: `tests/test_entry.py`, `tests/agent/test_profiles.py`

**Interfaces:**
- Produces:
  - `codex_bridge.entry.main(argv: list[str]) -> int` — `argv` が空 → 起動のダイアログを出し、選ばれた役割で動く（キャンセルなら 0）。先頭が `host` → 残りを `host.main.main` に渡す（.bat 用。ダイアログを出さない）。先頭が `agent` → `agent [--profile <名前>]` で、その貸す側をダイアログなしで動かす。それ以外 → 使い方を出して 2。
  - `codex_bridge.launcher.choose_role(profiles: list[Profile]) -> Choice | None` — `Choice = ("host",) | ("agent", profile_name)`。ダイアログの中身: 「使う側（自分の PC のフォルダを Codex に使わせる）」のボタン 1 つ、「貸す側」の一覧（プロファイルごとに 1 行。名前・接続先・今動いているか）、「新しい貸す側を追加」（名前を入れる）。動いている貸す側を選んだら、新しく起動せず、その画面をブラウザで開く。
  - `codex_bridge.agent.profiles`:
    - `Profile(name: str, home: Path, ui_port: int)`
    - `list_profiles() -> list[Profile]`、`create_profile(name: str) -> Profile`（名前は 1〜32 文字、ファイル名に使えない文字は拒否、同じ名前は拒否）
    - 既定のプロファイル `default` は、今の保存場所（`%LOCALAPPDATA%\codex-bridge-agent` と location.txt の仕組み）と画面のポート 8766 をそのまま使う（v0.2 までの利用者の設定・招待がそのまま残る）。
    - 追加のプロファイルは `%LOCALAPPDATA%\codex-bridge-agent\profiles\<名前>\` を自分の保存場所の起点にし（location.txt もその中）、画面のポートは 8767 から空いている番号を選んでプロファイルに保存する（次からも同じ番号）。
    - 単一起動の印（Mutex）はプロファイルごと。同じプロファイルの二重起動は、すでに動いている方の画面を開いて終わる。違うプロファイルは同時に動く。
  - 使う側を選んだとき: exe の隣に `host.yaml` がなければ `setup` を、あれば `start` を、コンソールの窓を開いて（`AllocConsole`）動かす。
  - `CODEX_BRIDGE_AGENT_HOME`（試験用の環境変数）は今までどおり効く（プロファイルより優先）。

- [ ] **Step 1: 失敗するテスト**
  - `tests/test_entry.py`: 引数なし → `choose_role` が呼ばれ、`("host",)` なら host、`("agent","work")` なら agent が `work` で動く、`None` なら 0／`host start --config host.yaml` → host の main が `["start", "--config", "host.yaml"]` で呼ばれ、ダイアログは出ない／`agent --profile work` → ダイアログなしで agent／`foo` → 2。
  - `tests/agent/test_profiles.py`: 既定のプロファイルの保存場所と 8766 が今と同じ／追加したプロファイルが自分の保存場所と 8767 以降の空きポートを持ち、次に読んでも同じ／ポートが使用中なら次の番号／名前の検証／2 つのプロファイルの設定・招待・作業ログが混ざらない。
- [ ] **Step 2: 実装（振り分けとダイアログ）** — exe はウィンドウなし（`console=False`）で作る。`host` のときは `ctypes.windll.kernel32.AttachConsole(-1)`（失敗したら `AllocConsole()`）でコンソールにつなぎ、`sys.stdout` / `sys.stderr` / `sys.stdin` を `CONOUT$` / `CONIN$` で開き直す。
- [ ] **Step 3: 実装（プロファイル）** — agent の main・setup・画面のポート・Mutex・トレイの表示（「codex-bridge（貸す側: <名前>）」）・画面の見出しにプロファイル名を通す。Codex のログイン（`~/.codex`）はプロファイルの間で共通（v0.2 の範囲では分けない。文書にそう書く）。
- [ ] **Step 4: spec とビルド** — `codex-bridge.spec` 1 つ（両方の静的ファイルと hiddenimports を合わせる）。`build_release.py` は admin の zip に `codex-bridge.exe`＋.bat＋`vendor/rg.exe`＋docs、friend の zip に `codex-bridge.exe`＋`使い方.md`。.bat は `"%~dp0codex-bridge.exe" host <cmd> --config host.yaml`。
- [ ] **Step 5: 文書** — 3 つの文書に、exe の名前、起動のダイアログ、貸す側を複数動かす方法（「新しい貸す側を追加」、それぞれに招待コード、画面のポートは 8767 から）を書く。
- [ ] **Step 6: 通過を確かめ、ダイアログを撮影** — 全テスト。起動のダイアログ（プロファイル 0 個・2 個のとき）をスクリーンショットで撮り、目で確かめる（`.superpowers/sdd/2026-11-16-feedback/shots/task-5/`）。
- [ ] **Step 7: Commit** — `build: one codex-bridge.exe with a start-up role dialog; several lender profiles`

### Task 6: 本物での確認と撮影（Review Focus 1〜5、R9）

このタスクは実装ではなく確認。コードは直さず、失敗は記録する。

- [ ] **Step 1:** `uv run --no-sync python scripts/build_release.py`（前に `release/` の中身を一時フォルダへ退避し、あとで戻す）。zip を一時フォルダに展開し、`codex-bridge.exe` を引数なしで起動 → 起動のダイアログが出る。貸す側を 2 つ（既定と追加 1 つ）同時に動かし、トレイが 2 つ、画面が 8766 と 8767 で別々に開く。同じ貸す側をもう一度選ぶと、二重起動せず画面が開く。`setup.bat` / `start.bat` / `stop.bat` で使う側が動き、コンソールに文字が出る。保存場所は試験用（`CODEX_BRIDGE_AGENT_HOME` または一時フォルダ）にし、利用者の本物の貸す側の設定に触らない。
- [ ] **Step 2:** 試験用の host（一時フォルダの host.yaml）と貸す側 1 つで、招待を失効 → 貸す人の画面に赤い帯 → 新しいコードを画面から入れる → つながる。
- [ ] **Step 3:** 待ち時間なしの事前承認で依頼 → 長く待っても期限切れにならない → 許可で始まる。
- [ ] **Step 4:** 2 万ファイル以上の共有フォルダで find_files・grep・コマンド実行の前後の走査の時間を測る。
- [ ] **Step 5:** 管理画面と貸す人の画面を撮り、Requirements R1〜R5・R8 を目で確かめる。撮影画像は `.superpowers/sdd/2026-11-16-feedback/shots/task-6/`。
- [ ] **Step 6:** 起動したものをすべて止め、`git status` が空で、`release/` に v0.1.0 が残っていることを確かめる。
