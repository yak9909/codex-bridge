# codex-bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 知人の PC で動く Codex に、こちらの PC の作業フォルダを MCP 経由で探索・読み書き・実行させ、こちらの Claude Code から依頼・追加指示・使用量監視・セッション管理ができるようにする。

**Architecture:** こちらの PC で常駐する `codex-bridge-host`（FastAPI 1 プロセス）が 3 つの口を持つ。(1) Tailscale 側に向けた Codex 用 MCP（streamable HTTP、トークン認証）と制御用 WebSocket、(2) localhost の Claude 用 MCP、(3) localhost の Web 管理画面。知人の PC の `codex-bridge-agent`（exe）は host へ WebSocket で接続し、`codex app-server` を stdio の JSON-RPC で操作する。Codex はこちらの MCP の道具だけを使う。

**Tech Stack:** Python ≥3.12（uv で管理）、`mcp`（公式 Python SDK、FastMCP）、FastAPI、uvicorn、`websockets`、PyYAML、pytest、pytest-asyncio、ripgrep（`rg.exe`）、PyInstaller。

**Spec:** `docs/2026-11-14-codex-bridge-design.md`（この計画は設計書の 4〜7.5 章を実装する。実装者は両方を読む）

## Global Constraints

- リポジトリのルートは `A:\MyProjects\Others\codex-bridge`。パッケージは `src/codex_bridge/`（`common/`・`host/`・`agent/`）、テストは `tests/`。
- 両側とも Windows で動くこと。パスの判定は大文字小文字を区別しない（`os.path.normcase`）。
- Codex に見せるパスはすべて共有ルートからの相対パス（区切りは `/`）。絶対パスを道具の結果に出さない。
- `hidden` の場所は、一覧・検索・エラー文のどれにも現れない。存在しないものと同じエラー `not found: <要求されたパス>` を返す。
- トークンはディスクに平文で書かない（host は SHA-256 だけを保存。agent は Windows の DPAPI で暗号化して保存）。agent はトークンを環境変数 `CODEX_BRIDGE_TOKEN` で app-server に渡す。
- 既定値: 実行の時間上限 1800 秒、出力上限 1 MiB、同期待ち 60 秒、確認待ちの自動拒否 1800 秒、使用量の取得間隔 60 秒、しきい値 5 時間枠 90% / 週間枠 95%、穏やかな中断の猶予 600 秒、`read_file` の 1 回の上限 256 KiB、`grep` の上限 500 件。
- 使用量の枠は `windowDurationMins` で見分ける（300 = 5 時間枠、10080 = 週間枠）。`primary` / `secondary` の位置で決めつけない。
- app-server の起動: `codex app-server --disable shell_tool -c mcp_servers.bridge.url="<host>/codex/mcp" -c mcp_servers.bridge.bearer_token_env_var="CODEX_BRIDGE_TOKEN" -c mcp_servers.bridge.tool_timeout_sec=120`。`thread/start` は `cwd`=agent の空フォルダ、`sandbox`="read-only"、`approvalPolicy`="never"、`developerInstructions`=前置き文。
- タスクは同時に 1 つ。Claude Code は確認待ちを承認できない（承認は Web 画面だけ）。
- 道具の結果・UI の文言は日本語。コード中の識別子は英語。
- 知人の `~/.codex/config.toml` は書き換えない。

## Review Focus

1. **ジャンクション・シンボリックリンクで共有ルートの外や `hidden` に出る** → 実パスに解決してから判定し、`not found` を返す。（Task 2 にテスト）
2. **`hidden` の名前が grep の結果・`find_files`・エラー文・`apply_patch` の失敗文から漏れる** → どの道具でも出ない。（Task 3・4 にテスト）
3. **作業中に host と agent の接続が切れる** → agent は Codex を止めずに再接続し、切断中のイベントを送り直す。host はタスクを「接続待ち」と表示する。（Task 11 にテスト）
4. **巨大ファイル・バイナリファイル・UTF-8 でないファイル（Shift_JIS など）を読む／編集する** → 読み取りは上限で切って続きの位置を返す。バイナリは 16 進の一部。編集は元の文字コードと改行を保つ。判別できなければ編集を断る。（Task 2・4 にテスト）
5. **しきい値を超えたときに作業中のターンがない、または `turn/steer` が失敗する** → 新しい依頼を止めるだけにする。steer が失敗したら、次のターンの開始時に中断指示を入れる。（Task 10 にテスト）

---

## ファイルの構成

```
codex-bridge/
  pyproject.toml                  # uv プロジェクト。scripts: codex-bridge-host / codex-bridge-agent
  vendor/rg.exe                   # Task 3 で取得（git には入れない。取得スクリプトを入れる）
  scripts/fetch_rg.ps1
  src/codex_bridge/
    common/
      protocol.py                 # host⇔agent のメッセージ型（Task 8）
      invite.py                   # 招待コードの作成・解読（Task 8）
      audit.py                    # JSON Lines の記録（Task 1）
    host/
      config.py                   # host の設定（共有ルート、待ち受け先など）（Task 1）
      policy.py                   # 権限の規則と判定・即時反映（Task 1）
      paths.py                    # 相対パス⇔実パスの安全な変換（Task 2）
      fs_read.py                  # list_dir / find_files / stat / read_file（Task 2）
      search.py                   # grep（Task 3）
      snapshots.py                # 書き込み前の保存と取り消し（Task 4）
      fs_write.py                 # write_file / edit_file / move_path / delete_path（Task 4）
      patch.py                    # apply_patch の解釈と適用（Task 5）
      approvals.py                # 確認待ちの保管・自動拒否（Task 6）
      runner.py                   # run_command / get_command_result（Task 6）
      codex_mcp.py                # Codex 用 MCP（Task 7）
      tasks.py                    # タスク・セッションの状態管理（Task 8）
      control.py                  # 制御用 WebSocket（Task 8）
      claude_mcp.py               # Claude 用 MCP（Task 9）
      web/app.py, web/static/*    # Web 管理画面（Task 12）
      main.py                     # 起動（Task 12）
    agent/
      appserver.py                # codex app-server の JSON-RPC クライアント（Task 10）
      usage.py                    # 使用量の判定と中断の状態機械（Task 10）
      sessions.py                 # 自分が作ったスレッドの記録（Task 11）
      runner.py                   # タスクの実行と host との連携（Task 11）
      control_client.py           # WebSocket の接続・再接続（Task 11）
      setup.py                    # 招待コード・codex の検出と取得・ログイン確認（Task 13）
      logview.py                  # 知人向けログ画面（Task 13）
      main.py                     # 起動（Task 13）
  tests/
    fakes/fake_appserver.py       # 偽の app-server（Task 10）
    ...
```

---

### Task 1: 土台・設定・権限の規則・記録

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `src/codex_bridge/__init__.py`, `src/codex_bridge/host/config.py`, `src/codex_bridge/host/policy.py`, `src/codex_bridge/common/audit.py`
- Test: `tests/host/test_policy.py`, `tests/common/test_audit.py`

**Interfaces:**
- Produces:
  - `Level = Literal["hidden","read","write"]`
  - `ExecMode = Literal["off","allowlist","ask","all"]`
  - `@dataclass HostConfig(share_root: Path, data_dir: Path, public_bind: str, local_port: int, rg_path: Path)`、`load_host_config(path: Path) -> HostConfig`
  - `class Policy`: `level_for(rel: str) -> Level`、`exec_mode: ExecMode`、`allow_patterns: list[str]`、`paused: bool`、`confirm_modified_scripts: bool`、`delete_needs_approval: bool`（既定 True）、`limits: Limits`（Global Constraints の既定値）
  - `class PolicyStore(path: Path)`: `current() -> Policy`（ファイルの更新日時が変わっていれば読み直す）、`save(policy_dict: dict) -> None`、`as_dict() -> dict`
  - `class AuditLog(path: Path)`: `write(kind: str, **fields) -> None`（1 行 1 JSON、`ts` を ISO 8601 で付ける）

- [ ] **Step 1: リポジトリを作る**

`git init`。`uv init --package --name codex-bridge`。依存: `mcp fastapi uvicorn websockets pyyaml pystray pillow`、開発用: `pytest pytest-asyncio httpx pyinstaller`。`.gitignore` に `vendor/`・`.venv/`・`dist/`・`build/`・`data/`。

- [ ] **Step 2: 失敗するテストを書く**

```python
# tests/host/test_policy.py
POLICY = """
default: read
rules:
  - {path: "raw/**", level: write}
  - {path: "raw/secret/**", level: hidden}
  - {path: "out", level: write}
exec: {mode: allowlist, allow: ["python scripts/**/*.py *"]}
"""
def test_later_more_specific_rule_wins(tmp_path):
    store = write_policy(tmp_path, POLICY)
    p = store.current()
    assert p.level_for("raw/a.txt") == "write"
    assert p.level_for("raw/secret/x.bin") == "hidden"
    assert p.level_for("RAW/Secret/x.bin") == "hidden"   # 大文字小文字を区別しない
    assert p.level_for("docs/readme.md") == "read"
    assert p.level_for("out/sub/f.csv") == "write"        # フォルダ指定は配下にも効く

def test_hidden_parent_hides_children_even_if_child_rule_says_write(tmp_path):
    store = write_policy(tmp_path, POLICY + '  - {path: "raw/secret/ok.txt", level: write}\n')
    assert store.current().level_for("raw/secret/ok.txt") == "hidden"

def test_reload_on_change(tmp_path):
    store = write_policy(tmp_path, POLICY)
    assert store.current().exec_mode == "allowlist"
    rewrite(tmp_path, POLICY.replace("allowlist", "off"))
    assert store.current().exec_mode == "off"

def test_broken_yaml_keeps_last_good_policy(tmp_path):
    store = write_policy(tmp_path, POLICY)
    store.current()
    rewrite(tmp_path, "rules: [")
    assert store.current().exec_mode == "allowlist"
```

```python
# tests/common/test_audit.py
def test_audit_appends_json_lines(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl")
    log.write("tool_call", tool="read_file", path="x.txt")
    log.write("tool_call", tool="grep")
    lines = (tmp_path / "a.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["tool"] == "read_file" and "ts" in json.loads(lines[0])
```

- [ ] **Step 3: 失敗を確かめる** — Run: `uv run pytest tests/host/test_policy.py tests/common/test_audit.py -v` / Expected: FAIL（import error）

- [ ] **Step 4: 実装する**

規則の判定: パターンは glob（`**` 対応、`fnmatch` ではなく自前で `/` 区切りに対応させる）。パターンにワイルドカードがなければ「そのパスと配下」を意味する。一致した規則のうち最後のものを使う。ただし祖先が `hidden` に一致したら、配下は常に `hidden`。

- [ ] **Step 5: 通ることを確かめる** — Run: 同上 / Expected: PASS

- [ ] **Step 6: Commit** — `git add -A && git commit -m "feat: project scaffold, policy rules, audit log"`

---

### Task 2: 安全なパス変換と読み取りの道具

**Files:**
- Create: `src/codex_bridge/host/paths.py`, `src/codex_bridge/host/fs_read.py`
- Test: `tests/host/test_paths.py`, `tests/host/test_fs_read.py`, `tests/conftest.py`（共有ルートの見本を作る fixture `share`）

**Interfaces:**
- Consumes: `PolicyStore`, `Level`（Task 1）
- Produces:
  - `class ToolError(Exception)`（道具が返す日本語のエラー。文言は利用者に見せてよい内容だけ）
  - `class SafePaths(root: Path, policy: PolicyStore)`: `resolve(rel: str, need: Literal["read","write"]) -> Path`（外に出る・hidden・権限不足は `ToolError`。hidden と外は `not found: <rel>`）、`to_rel(p: Path) -> str`、`visible(p: Path) -> bool`
  - `class ReadTools(paths: SafePaths, limits)`: `list_dir(path=".") -> list[dict]`（`name,type,size,mtime`）、`find_files(pattern: str, path=".", limit=1000) -> list[str]`、`stat(path) -> dict`、`read_file(path, offset_line=1, max_lines=2000, byte_offset: int|None=None, max_bytes=None) -> dict`（`text` または `hex`、`encoding`、`next_offset_line` / `next_byte_offset`、`truncated`）

- [ ] **Step 1: 失敗するテストを書く**

```python
def test_dotdot_escape_is_not_found(share):
    with pytest.raises(ToolError, match="not found: ../outside.txt"):
        share.paths.resolve("../outside.txt", "read")

def test_junction_to_outside_is_not_found(share):      # mklink /J で外を指すジャンクションを作る
    make_junction(share.root / "link", share.outside)
    with pytest.raises(ToolError, match="not found"):
        share.paths.resolve("link/outside.txt", "read")

def test_hidden_is_indistinguishable_from_missing(share):
    e1 = raises_msg(lambda: share.read.read_file("raw/secret/k.txt"))
    e2 = raises_msg(lambda: share.read.read_file("raw/nope.txt"))
    assert e1 == "not found: raw/secret/k.txt" and e2 == "not found: raw/nope.txt"

def test_list_dir_and_find_skip_hidden(share):
    assert "secret" not in [e["name"] for e in share.read.list_dir("raw")]
    assert all("secret" not in p for p in share.read.find_files("**/*"))

def test_read_write_on_read_only_area_is_refused(share):
    with pytest.raises(ToolError, match="書き込みは許可されていません: docs/readme.md"):
        share.paths.resolve("docs/readme.md", "write")

def test_read_large_text_is_paged(share):              # 10 万行のファイル
    r = share.read.read_file("raw/big.txt", max_lines=100)
    assert r["truncated"] and r["next_offset_line"] == 101 and r["text"].count("\n") == 100

def test_read_shift_jis(share):
    r = share.read.read_file("raw/sjis.txt")
    assert r["encoding"] == "cp932" and "日本語" in r["text"]

def test_binary_returns_hex_slice(share):
    r = share.read.read_file("raw/data.bin", max_bytes=64)
    assert "hex" in r and len(bytes.fromhex(r["hex"])) == 64 and r["next_byte_offset"] == 64

def test_paused_blocks_everything(share):
    share.set_policy(paused=True)
    with pytest.raises(ToolError, match="停止中"):
        share.read.list_dir(".")
```

- [ ] **Step 2: 失敗を確かめる** — Run: `uv run pytest tests/host/test_paths.py tests/host/test_fs_read.py -v` / Expected: FAIL

- [ ] **Step 3: 実装する**

`resolve` は `Path.resolve(strict=False)` の結果が `root.resolve()` の配下かを `normcase` で比較し、そのあと相対パスで `level_for` を判定する。文字コードの判定は utf-8（BOM 付き含む）→ cp932 の順に試す。NUL バイトを含むか、どちらでも解読できなければバイナリ扱い。

- [ ] **Step 4: 通ることを確かめる** — Run: 同上 / Expected: PASS

- [ ] **Step 5: Commit** — `git commit -am "feat: safe path resolution and read tools"`

---

### Task 3: grep（ripgrep）

**Files:**
- Create: `scripts/fetch_rg.ps1`（ripgrep の公式 GitHub リリースから Windows 版 zip を取得し `vendor/rg.exe` に置く）, `src/codex_bridge/host/search.py`
- Test: `tests/host/test_search.py`

**Interfaces:**
- Consumes: `SafePaths`, `ToolError`（Task 2）、`HostConfig.rg_path`
- Produces: `class SearchTools(paths: SafePaths, rg_path: Path, limits)`: `grep(pattern: str, path=".", glob: str|None=None, ignore_case=False, context=0, limit=500) -> dict`（`matches: list[{path,line,text}]`、`truncated: bool`）

- [ ] **Step 1: 失敗するテストを書く**

```python
def test_grep_finds_and_reports_relative_paths(share):
    r = share.search.grep("needle")
    assert {"path": "raw/a.txt", "line": 3, "text": "the needle here"} in r["matches"]

def test_grep_never_returns_hidden(share):            # raw/secret/k.txt にも needle がある
    assert all(not m["path"].startswith("raw/secret") for m in share.search.grep("needle")["matches"])

def test_grep_hidden_dir_as_target_is_not_found(share):
    with pytest.raises(ToolError, match="not found: raw/secret"):
        share.search.grep("x", path="raw/secret")

def test_grep_limit(share):                            # 1000 件一致するファイル
    r = share.search.grep("row", limit=500)
    assert len(r["matches"]) == 500 and r["truncated"]

def test_grep_bad_regex_message_does_not_leak_paths(share):
    msg = raises_msg(lambda: share.search.grep("("))
    assert "正規表現" in msg and str(share.root) not in msg
```

- [ ] **Step 2: 失敗を確かめる** — Run: `uv run pytest tests/host/test_search.py -v` / Expected: FAIL

- [ ] **Step 3: 実装する**

`rg --json` を共有ルートを作業フォルダにして実行する。hidden の規則は `--glob !<pattern>` でも除くが、判定の正本は結果ごとの `level_for` による後段の絞り込み。`rg` の標準エラーは利用者に返さず、正規表現の誤りだけを「正規表現が正しくありません」に変換する。

- [ ] **Step 4: 通ることを確かめる** — Run: `powershell scripts/fetch_rg.ps1; uv run pytest tests/host/test_search.py -v` / Expected: PASS

- [ ] **Step 5: Commit** — `git commit -am "feat: grep via ripgrep with hidden filtering"`

---

### Task 4: 書き込みの道具と取り消し

**Files:**
- Create: `src/codex_bridge/host/snapshots.py`, `src/codex_bridge/host/fs_write.py`
- Test: `tests/host/test_snapshots.py`, `tests/host/test_fs_write.py`

**Interfaces:**
- Consumes: `SafePaths`, `ToolError`（Task 2）、`AuditLog`（Task 1）
- Produces:
  - 保存先は host の `data_dir/snapshots/<task_id>/`（設計書の「共有ルート内 `.codex-bridge/snapshots`」から変更。Codex に見えず、書き換えられないようにするため）。共有ルート内の `.codex-bridge/` は `outputs/` だけを置き、規則に関係なく `read` 扱いにする。
  - `class Snapshots(data_dir: Path)`: `before_change(task_id: str, rel: str, abs_path: Path) -> None`（そのタスクで最初に触る時だけ元を保存。存在しなかったことも記録）、`undo(task_id: str) -> list[str]`（戻したパス）
  - `class WriteTools(paths, snapshots, audit, task_id_fn: Callable[[], str|None])`: `write_file(path, content) -> dict`、`edit_file(path, old, new, replace_all=False) -> dict`、`move_path(src, dst) -> dict`、`delete_path(path) -> dict`（`delete_needs_approval` が真なら Task 6 の承認を通す。Task 4 では `approve_fn: Callable[[str], Awaitable[bool]]` を引数で受ける）
  - 文字コード・改行の保持: `read_text_preserving(abs) -> (text, encoding, newline)`、`write_text_preserving(abs, text, encoding, newline)`

- [ ] **Step 1: 失敗するテストを書く**

```python
def test_edit_preserves_cp932_and_crlf(share):
    share.write.edit_file("raw/sjis_crlf.txt", "日本語", "にほんご")
    b = (share.root / "raw/sjis_crlf.txt").read_bytes()
    assert b.decode("cp932").count("\r\n") == 3 and "にほんご" in b.decode("cp932")

def test_edit_requires_unique_match(share):
    with pytest.raises(ToolError, match="2 か所"):
        share.write.edit_file("raw/dup.txt", "same", "x")

def test_write_to_read_only_area_refused(share):
    with pytest.raises(ToolError, match="書き込みは許可されていません"):
        share.write.write_file("docs/new.md", "x")

def test_move_into_hidden_is_not_found(share):
    with pytest.raises(ToolError, match="not found: raw/secret/x.txt"):
        share.write.move_path("raw/a.txt", "raw/secret/x.txt")

def test_undo_restores_edit_create_and_delete(share):
    share.task("t1")
    share.write.edit_file("raw/a.txt", "needle", "pin")
    share.write.write_file("raw/new.txt", "n")
    share.approve_all(); share.write.delete_path("raw/b.txt")
    restored = share.snapshots.undo("t1")
    assert (share.root/"raw/a.txt").read_text() == ORIGINAL_A
    assert not (share.root/"raw/new.txt").exists() and (share.root/"raw/b.txt").exists()
    assert sorted(restored) == ["raw/a.txt", "raw/b.txt", "raw/new.txt"]

def test_delete_denied_keeps_file(share):
    share.deny_all()
    with pytest.raises(ToolError, match="拒否"):
        share.write.delete_path("raw/b.txt")
    assert (share.root/"raw/b.txt").exists()

def test_binary_edit_refused(share):
    with pytest.raises(ToolError, match="テキストとして扱えません"):
        share.write.edit_file("raw/data.bin", "a", "b")
```

- [ ] **Step 2: 失敗を確かめる** — Run: `uv run pytest tests/host/test_snapshots.py tests/host/test_fs_write.py -v` / Expected: FAIL
- [ ] **Step 3: 実装する** — 書き込みは一時ファイル＋`os.replace` で行う。改行はファイル内で最も多い種類を保つ。
- [ ] **Step 4: 通ることを確かめる** — Run: 同上 / Expected: PASS
- [ ] **Step 5: Commit** — `git commit -am "feat: write tools with snapshots and undo"`

---

### Task 5: apply_patch

**Files:**
- Create: `src/codex_bridge/host/patch.py`
- Test: `tests/host/test_patch.py`

**Interfaces:**
- Consumes: `WriteTools`（Task 4。実際の書き込み・保存・権限判定は必ず WriteTools 経由）
- Produces: `parse_patch(text: str) -> list[PatchOp]`（`AddFile(path, content)`・`DeleteFile(path)`・`UpdateFile(path, move_to: str|None, hunks: list[Hunk])`）、`apply_patch(write: WriteTools, text: str) -> dict`（`changed: list[str]`）

書式は Codex の `*** Begin Patch` / `*** Add File:` / `*** Delete File:` / `*** Update File:` / `*** Move to:` / `@@` / ` `・`-`・`+` 行 / `*** End Patch`。

- [ ] **Step 1: 失敗するテストを書く**

```python
def test_update_hunk_applies(share):
    apply_patch(share.write, "*** Begin Patch\n*** Update File: raw/a.txt\n@@\n line2\n-the needle here\n+the pin here\n*** End Patch\n")
    assert "the pin here" in (share.root/"raw/a.txt").read_text()

def test_all_or_nothing_when_second_file_fails(share):
    with pytest.raises(ToolError):
        apply_patch(share.write, PATCH_A_OK_THEN_B_CONTEXT_MISMATCH)
    assert (share.root/"raw/a.txt").read_text() == ORIGINAL_A

def test_patch_into_hidden_reports_not_found_only(share):
    msg = raises_msg(lambda: apply_patch(share.write, "*** Begin Patch\n*** Add File: raw/secret/z.txt\n+x\n*** End Patch\n"))
    assert msg == "not found: raw/secret/z.txt"

def test_context_mismatch_message_names_file_and_line(share):
    msg = raises_msg(lambda: apply_patch(share.write, PATCH_BAD_CONTEXT))
    assert "raw/a.txt" in msg and "一致する箇所がありません" in msg
```

- [ ] **Step 2: 失敗を確かめる** — Run: `uv run pytest tests/host/test_patch.py -v` / Expected: FAIL
- [ ] **Step 3: 実装する** — 全操作を先に検証（読み込み・照合・権限）してから書き込む。照合は完全一致→行末空白を無視、の順。
- [ ] **Step 4: 通ることを確かめる** — Run: 同上 / Expected: PASS
- [ ] **Step 5: Commit** — `git commit -am "feat: apply_patch support"`

---

### Task 6: 確認待ちとコマンド実行

**Files:**
- Create: `src/codex_bridge/host/approvals.py`, `src/codex_bridge/host/runner.py`
- Test: `tests/host/test_approvals.py`, `tests/host/test_runner.py`

**Interfaces:**
- Consumes: `PolicyStore`, `SafePaths`, `AuditLog`
- Produces:
  - `class Approvals`: `request(kind: str, summary: str, task_id: str|None) -> Approval`（`id`、`status: pending|approved|denied|expired`、`created_at`）、`decide(id, approve: bool) -> None`、`wait(id, timeout: float) -> str`、`pending() -> list[Approval]`、`on_change: Callable`（Web 画面と Claude 用 MCP への通知用）。`limits.approval_timeout_sec` を過ぎた pending は `expired`（拒否扱い）。
  - `class CommandRunner(paths, policy, approvals, audit, task_id_fn)`: `run_command(command: str, cwd=".", timeout_sec: int|None=None) -> dict`、`get_command_result(job_id: str, wait_sec: int=30) -> dict`
  - 戻り値: `{"status": "done"|"running"|"waiting_approval"|"denied"|"timeout", "job_id", "exit_code", "stdout", "stderr", "output_file", "changed_files"}`
  - `match_allow(command: str, patterns: list[str]) -> bool`

- [ ] **Step 1: 失敗するテストを書く**

```python
def test_allowlist_match():
    assert match_allow("python scripts/a/b.py --in raw/x", ["python scripts/**/*.py *"])
    assert not match_allow("python other.py", ["python scripts/**/*.py *"])
    assert not match_allow("python scripts/a.py && del x", ["python scripts/**/*.py *"])  # 連結記号を含むものは一致させない

async def test_mode_off_refuses(host): ...           # status == "denied", 「実行は許可されていません」
async def test_ask_mode_waits_then_runs_after_approval(host):
    r = await host.runner.run_command("python -c \"print(1)\"")
    assert r["status"] == "waiting_approval"
    host.approvals.decide(host.approvals.pending()[0].id, True)
    r2 = await host.runner.get_command_result(r["job_id"], wait_sec=10)
    assert r2["status"] == "done" and r2["stdout"].strip() == "1"

async def test_unanswered_approval_expires(host):    # approval_timeout_sec を 1 に
    r = await host.runner.run_command("python -c 1")
    await asyncio.sleep(1.5)
    assert (await host.runner.get_command_result(r["job_id"], 1))["status"] == "denied"

async def test_long_command_returns_job_id_then_result(host):   # sync_wait_sec を 1 に
    r = await host.runner.run_command("python -c \"import time;time.sleep(2);print('ok')\"")
    assert r["status"] == "running"
    assert (await host.runner.get_command_result(r["job_id"], 5))["stdout"].strip() == "ok"

async def test_timeout_kills_process_tree(host): ...  # timeout_sec=1、子プロセスも終わる。status == "timeout"
async def test_large_output_spills_to_file(host):    # 2 MiB 出力 → stdout は 1 MiB 未満、output_file は共有ルート内 .codex-bridge/outputs/ の相対パス
async def test_changed_files_reported(host):         # write 領域にファイルを作るスクリプト → changed_files に入る
async def test_cwd_outside_or_hidden_refused(host):  # cwd="raw/secret" → not found
async def test_modified_script_needs_approval_when_enabled(host):
    host.set_policy(confirm_modified_scripts=True); host.task("t1")
    host.write.write_file("scripts/new.py", "print(1)")
    assert (await host.runner.run_command("python scripts/new.py x"))["status"] == "waiting_approval"
```

- [ ] **Step 2: 失敗を確かめる** — Run: `uv run pytest tests/host/test_approvals.py tests/host/test_runner.py -v` / Expected: FAIL
- [ ] **Step 3: 実装する** — 実行は `asyncio.create_subprocess_shell`（`cmd.exe`）＋ Windows の Job Object（`CREATE_NEW_PROCESS_GROUP` と `taskkill /T /F` でも可）で子ごと止める。`changed_files` は実行前後で write 領域の `(mtime, size)` を比べる（hidden は除く）。`&&`・`||`・`|`・`;`・`&`・`>`・`<`・改行を含むコマンドは許可リストに一致させない。
- [ ] **Step 4: 通ることを確かめる** — Run: 同上 / Expected: PASS
- [ ] **Step 5: Commit** — `git commit -am "feat: approvals and command runner"`

---

### Task 7: Codex 用 MCP

**Files:**
- Create: `src/codex_bridge/host/codex_mcp.py`
- Test: `tests/host/test_codex_mcp.py`

**Interfaces:**
- Consumes: `ReadTools`, `SearchTools`, `WriteTools`, `apply_patch`, `CommandRunner`, `AuditLog`
- Produces: `build_codex_mcp(services: HostServices, token_sha256: Callable[[], set[str]]) -> ASGIApp`（streamable HTTP。`/codex/mcp` に mount される前提）。`@dataclass HostServices(paths, read, search, write, runner, approvals, snapshots, policy, audit, tasks)`（`tasks` は Task 8 で埋める。ここでは `current_task_id() -> str|None` だけ使う）

道具の名前と引数は設計書 5.1(a) の表と Task 2〜6 の関数のとおり（`list_dir, find_files, grep, stat, read_file, write_file, edit_file, apply_patch, move_path, delete_path, run_command, get_command_result`）。各道具の説明文は日本語で、パスが共有ルートからの相対であることを明記する。`ToolError` は MCP のエラー結果（`isError: true`、本文はメッセージだけ）にする。

- [ ] **Step 1: 失敗するテストを書く**

```python
async def test_requires_bearer_token(client):
    assert (await client.post("/codex/mcp", json=INIT)).status_code == 401
    assert (await client.post("/codex/mcp", json=INIT, headers=auth("wrong"))).status_code == 401

async def test_tool_list_matches_spec(mcp_session):
    names = {t.name for t in (await mcp_session.list_tools()).tools}
    assert names == {"list_dir","find_files","grep","stat","read_file","write_file","edit_file",
                     "apply_patch","move_path","delete_path","run_command","get_command_result"}

async def test_tool_error_is_mcp_error_without_paths(mcp_session, share):
    r = await mcp_session.call_tool("read_file", {"path": "raw/secret/k.txt"})
    assert r.isError and r.content[0].text == "not found: raw/secret/k.txt"

async def test_every_call_is_audited(mcp_session, share):
    await mcp_session.call_tool("list_dir", {"path": "raw"})
    assert last_audit(share)["tool"] == "list_dir"

async def test_revoked_token_rejected_immediately(client, tokens): ...
```

- [ ] **Step 2: 失敗を確かめる** — Run: `uv run pytest tests/host/test_codex_mcp.py -v` / Expected: FAIL
- [ ] **Step 3: 実装する** — FastMCP の streamable HTTP（stateless でよい）を ASGI として取り出し、前段にトークン照合の middleware を置く（`hashlib.sha256` で比較、`hmac.compare_digest`）。
- [ ] **Step 4: 通ることを確かめる** — Run: 同上 / Expected: PASS
- [ ] **Step 5: Commit** — `git commit -am "feat: codex-facing MCP endpoint"`

---

### Task 8: host⇔agent の約束事・招待コード・タスク管理・制御用 WebSocket

**Files:**
- Create: `src/codex_bridge/common/protocol.py`, `src/codex_bridge/common/invite.py`, `src/codex_bridge/host/tasks.py`, `src/codex_bridge/host/control.py`
- Test: `tests/common/test_invite.py`, `tests/host/test_tasks.py`, `tests/host/test_control.py`

**Interfaces:**
- Produces:
  - 招待コード: `make_invite(url: str, token: str, name: str) -> str`（`cbx1.` + base64url(JSON)）、`parse_invite(code: str) -> Invite(url, token, name)`
  - `class TokenStore(data_dir)`: `issue(name) -> str`（平文は返すだけで保存は SHA-256）、`revoke(name)`、`hashes() -> set[str]`
  - メッセージ（JSON、`type` で区別。`protocol.py` に dataclass と `encode/decode`）:
    - host→agent: `task_start{task_id, prompt, continue_thread_id|null}`、`task_followup{task_id, text}`、`task_cancel{task_id}`、`session_list{req_id}`、`session_read{req_id, thread_id}`、`session_delete{req_id, thread_id}`、`settings{thresholds:{five_hour, weekly}, log_visibility: "summary"|"files"|"all"}`、`ack{seq}`
    - agent→host: `hello{agent_version, codex_version, auth_mode}`、`event{seq, task_id, kind, data}`（`kind`: `started|agent_message|tool_call|turn_completed|status|final_report|error`）、`usage{five_hour:{used_percent, resets_at}|null, weekly:{...}|null, ts}`、`task_state{seq, task_id, state, thread_id, report|null, resume_at|null}`、`reply{req_id, ok, data|error}`
    - `seq` 付きのものは agent が再接続後に未 `ack` 分を送り直す。host は `seq` で重複を捨てる。
  - `TaskState = Literal["queued","running","waiting_approval","completed","suspended_quota","failed","cancelled","agent_offline"]`
  - `class TaskManager`: `start(prompt, continue_task_id=None) -> str`（作業中なら `ToolError("作業中のタスクがあります")`、枠超過中なら `ToolError("使用量の上限のため受け付けません。再開可能: <ISO時刻>")`）、`followup(task_id, text)`、`cancel(task_id)`、`status(task_id) -> dict`、`result(task_id) -> dict`、`current_task_id() -> str|None`、`usage() -> dict`、`set_thresholds(five_hour: float|None, weekly: float|None)`、`sessions_list() / session_read(thread_id) / session_delete(thread_id)`（agent への往復。30 秒で時間切れ）
  - `control_ws_endpoint(manager, tokens)` → FastAPI の WebSocket ルート `/agent/ws`（接続時に `Authorization: Bearer`）

- [ ] **Step 1: 失敗するテストを書く**

```python
def test_invite_roundtrip():
    c = make_invite("http://100.64.0.1:8765", "tok", "friend")
    assert c.startswith("cbx1.") and parse_invite(c) == Invite("http://100.64.0.1:8765", "tok", "friend")

def test_invite_garbage_is_clear_error():
    with pytest.raises(ValueError, match="招待コード"): parse_invite("hello")

async def test_one_task_at_a_time(mgr):
    await mgr.start("a")
    with pytest.raises(ToolError, match="作業中"): await mgr.start("b")

async def test_quota_blocks_new_tasks(mgr):
    mgr.on_usage(usage(five_hour=95, resets_at=1_800_000_000))
    with pytest.raises(ToolError, match="再開可能"): await mgr.start("a")

async def test_duplicate_seq_ignored_and_acked(ws_pair): ...
async def test_disconnect_marks_agent_offline_and_reconnect_restores(ws_pair):
    # 作業中に切断 → status は "agent_offline"、再接続して task_state{running} が届けば "running" に戻る
async def test_continue_task_passes_thread_id(mgr, fake_agent):
    t1 = await mgr.start("a"); fake_agent.finish(t1, thread_id="th1")
    await mgr.start("b", continue_task_id=t1)
    assert fake_agent.last("task_start")["continue_thread_id"] == "th1"
```

- [ ] **Step 2: 失敗を確かめる** — Run: `uv run pytest tests/common/test_invite.py tests/host/test_tasks.py tests/host/test_control.py -v` / Expected: FAIL
- [ ] **Step 3: 実装する** — タスクとイベントは `data_dir/tasks/<task_id>.jsonl` に追記し、host の再起動後も `status`/`result` を返せるようにする。
- [ ] **Step 4: 通ることを確かめる** — Run: 同上 / Expected: PASS
- [ ] **Step 5: Commit** — `git commit -am "feat: host-agent protocol, invites, task manager, control websocket"`

---

### Task 9: Claude 用 MCP

**Files:**
- Create: `src/codex_bridge/host/claude_mcp.py`
- Test: `tests/host/test_claude_mcp.py`

**Interfaces:**
- Consumes: `TaskManager`（Task 8）、`Approvals`、`Snapshots`
- Produces: `build_claude_mcp(services) -> ASGIApp`（`/claude/mcp`、127.0.0.1 からの接続だけ受ける）。道具: `send_task(prompt, continue_task_id?)`、`send_followup(task_id, text)`、`get_status(task_id?)`（省略時は現在のタスク。`pending_approvals` の件数と要約を含む）、`get_result(task_id)`、`cancel_task(task_id)`、`get_usage()`、`set_thresholds(five_hour?, weekly?)`、`undo_task(task_id)`、`list_sessions()`、`read_session(thread_id)`、`delete_session(thread_id)`。承認の道具は作らない。

- [ ] **Step 1: 失敗するテストを書く**

```python
async def test_tool_names(claude_session):
    assert {t.name for t in (await claude_session.list_tools()).tools} == {
        "send_task","send_followup","get_status","get_result","cancel_task","get_usage",
        "set_thresholds","undo_task","list_sessions","read_session","delete_session"}

async def test_non_loopback_rejected(client):
    assert (await client.post("/claude/mcp", json=INIT, headers={"x-test-peer": "100.64.0.2"})).status_code == 403

async def test_status_shows_pending_approval(claude_session, host):
    host.approvals.request("command", "python x.py", task_id="t1")
    s = json.loads((await claude_session.call_tool("get_status", {"task_id": "t1"})).content[0].text)
    assert s["pending_approvals"][0]["summary"] == "python x.py" and "Web 画面" in s["note"]
```

- [ ] **Step 2〜4: 失敗→実装→通過を確かめる** — Run: `uv run pytest tests/host/test_claude_mcp.py -v`
- [ ] **Step 5: Commit** — `git commit -am "feat: claude-facing MCP endpoint"`

---

### Task 10: agent の app-server クライアントと使用量による中断

**Files:**
- Create: `src/codex_bridge/agent/appserver.py`, `src/codex_bridge/agent/usage.py`, `tests/fakes/fake_appserver.py`
- Test: `tests/agent/test_appserver.py`, `tests/agent/test_usage.py`

**Interfaces:**
- Produces:
  - `class AppServer(codex_path: Path, host_url: str, token: str, workdir: Path)`: `start()`（Global Constraints の起動引数。`initialize` を送る）、`thread_start(developer_instructions: str) -> str`、`turn_start(thread_id, text) -> str`、`turn_steer(thread_id, turn_id, text) -> None`、`turn_interrupt(thread_id, turn_id)`、`rate_limits() -> UsageSnapshot`、`thread_read(thread_id) -> dict`、`thread_delete(thread_id)`、`account() -> dict`、`events: AsyncIterator[dict]`（通知をそのまま）、`stop()`
  - サーバーからの要求への自動応答: `item/commandExecution/requestApproval`・`item/fileChange/requestApproval`・`execCommandApproval`・`applyPatchApproval` → 拒否。`mcpServer/elicitation/request` → `bridge` サーバーからのものだけ承諾、他は拒否。`item/tool/requestUserInput` → 空の返答で拒否。
  - `UsageSnapshot(five_hour: Window|None, weekly: Window|None)`、`Window(used_percent: float, resets_at: int)`、`snapshot_from_rate_limits(resp: dict) -> UsageSnapshot`（`rateLimits` の `primary`/`secondary` を `windowDurationMins` で振り分け）
  - `class QuotaGuard(thresholds, grace_sec=600, clock)`: `on_usage(s) -> list[Action]`、`on_turn_started(turn_id) -> list[Action]`、`on_turn_completed() -> list[Action]`、`tick() -> list[Action]`。`Action` は `Steer(text)`・`Interrupt()`・`Suspend(resume_at: int)`・`BlockNew(resume_at: int)`・`Unblock()`
  - 中断の指示文（そのまま使う）: 「使用量の上限が近づいています。今の作業を区切りのよいところで止め、終えたこと・残っていること・次にやるべきことを報告してください。報告のあとは新しい作業を始めないでください。」

- [ ] **Step 1: 偽の app-server を作る** — `tests/fakes/fake_appserver.py`: stdio の JSON-RPC。台本（JSON）に従い通知・要求を出し、受けた要求を記録する。`python -I tests/fakes/fake_appserver.py <script.json>` で起動。

- [ ] **Step 2: 失敗するテストを書く**

```python
def test_windows_identified_by_duration_not_position():
    s = snapshot_from_rate_limits({"rateLimits": {
        "primary":   {"usedPercent": 40, "windowDurationMins": 10080, "resetsAt": 2000},
        "secondary": {"usedPercent": 91, "windowDurationMins": 300,   "resetsAt": 1000}}})
    assert s.five_hour == Window(91, 1000) and s.weekly == Window(40, 2000)

def test_threshold_steers_once_during_turn():
    g = QuotaGuard({"five_hour": 90, "weekly": 95}, clock=FakeClock())
    g.on_turn_started("u1")
    assert g.on_usage(snap(five=91, reset=1000)) == [Steer(STOP_TEXT), BlockNew(1000)]
    assert g.on_usage(snap(five=92, reset=1000)) == []

def test_suspend_after_turn_completes():
    ...; assert g.on_turn_completed() == [Suspend(1000)]

def test_interrupt_after_grace():
    ...; clock.advance(601); assert g.tick() == [Interrupt(), Suspend(1000)]

def test_no_active_turn_only_blocks():
    g = QuotaGuard(...); assert g.on_usage(snap(five=91, reset=1000)) == [BlockNew(1000)]

def test_steer_failure_falls_back_to_next_turn_start():
    # Steer 実行が失敗したと知らせたら、次の on_turn_started で Steer を再度出す

def test_unblock_when_usage_drops():
    ...; assert g.on_usage(snap(five=10, reset=5000)) == [Unblock()]

async def test_launch_args_and_thread_params(fake):
    await fake.app.start(); tid = await fake.app.thread_start("前置き")
    assert "--disable" in fake.argv and "shell_tool" in fake.argv
    assert 'mcp_servers.bridge.bearer_token_env_var="CODEX_BRIDGE_TOKEN"' in fake.argv
    p = fake.received("thread/start")
    assert p["sandbox"] == "read-only" and p["approvalPolicy"] == "never" and p["developerInstructions"] == "前置き"
    assert fake.env["CODEX_BRIDGE_TOKEN"] == "tok"

async def test_command_approval_requests_are_declined(fake): ...
async def test_elicitation_only_accepted_for_bridge(fake): ...
```

- [ ] **Step 3: 失敗を確かめる** — Run: `uv run pytest tests/agent -v` / Expected: FAIL
- [ ] **Step 4: 実装する**
- [ ] **Step 5: 通ることを確かめる** — Run: 同上 / Expected: PASS
- [ ] **Step 6: 本物の app-server で形を確かめる（こちらの PC）** — Run: `uv run python -m codex_bridge.agent.appserver --probe`（`initialize` → `account/read` → `account/rateLimits/read` を送り、`snapshot_from_rate_limits` の結果を表示） / Expected: 5 時間枠と週間枠の両方に値が入る
- [ ] **Step 7: Commit** — `git commit -am "feat: app-server client and quota guard"`

---

### Task 11: agent のタスク実行・セッション記録・接続

**Files:**
- Create: `src/codex_bridge/agent/sessions.py`, `src/codex_bridge/agent/runner.py`, `src/codex_bridge/agent/control_client.py`
- Test: `tests/agent/test_sessions.py`, `tests/agent/test_runner.py`, `tests/integration/test_host_agent.py`

**Interfaces:**
- Consumes: `AppServer`, `QuotaGuard`（Task 10）、`protocol`（Task 8）
- Produces:
  - `class SessionRegistry(path: Path)`: `add(thread_id, task_id)`、`owns(thread_id) -> bool`、`list() -> list[dict]`、`remove(thread_id)`
  - `class AgentRunner(app, guard, sessions, send: Callable[[dict], Awaitable])`: `handle(msg: dict)`（host からのメッセージ全種）、app-server のイベントを `event`/`task_state` に変換して `send`。使用量は 60 秒ごとの `rate_limits()` と、通知 `account/rateLimits/updated`・`turn/completed` の後の取得で `guard.on_usage` に渡し、同じ値を `usage` メッセージで host に送る。`guard.tick()` は 5 秒ごと。
  - `class ControlClient(invite, runner)`: `run_forever()`（指数的に待ち時間を延ばして再接続、最大 60 秒。未 `ack` の `seq` 付きメッセージを再送）
  - 前置き文（`developerInstructions`）: 「ファイルの一覧・検索・読み書き・コマンド実行は、MCP サーバー bridge の道具だけを使ってください。パスは共有フォルダからの相対パスです。作業フォルダ（cwd）は空で、使いません。」

- [ ] **Step 1: 失敗するテストを書く**

```python
async def test_session_ops_limited_to_own_threads(runner, fake):
    await runner.handle({"type": "session_read", "req_id": "r1", "thread_id": "someone_elses"})
    assert sent(runner, "reply")["ok"] is False and fake.received("thread/read") is None

async def test_delete_session_calls_thread_delete_and_forgets(runner, fake): ...
async def test_followup_uses_turn_steer_with_active_turn(runner, fake):
    # 作業中: turn/steer(expectedTurnId=現在のターン)。作業中でない（ターン完了後）: turn/start で同じスレッドに続ける
async def test_quota_flow_end_to_end(runner, fake):
    # usage 91% → turn/steer に STOP_TEXT → turn/completed → task_state{state:"suspended_quota", resume_at:1000, report:最後の agent_message}
async def test_cancel_interrupts_then_reports_cancelled(runner, fake): ...
```

```python
# tests/integration/test_host_agent.py（本物の host を uvicorn で立て、agent は偽の app-server を使う）
async def test_full_round_trip(live_host, live_agent):
    tid = await live_host.claude.call("send_task", {"prompt": "x"})
    ...
    assert (await live_host.claude.call("get_result", {"task_id": tid}))["state"] == "completed"

async def test_reconnect_resends_unacked_events(live_host, live_agent):
    # 作業中に WebSocket を切る → host は agent_offline → 再接続後に切断中のイベントが届き、重複しない
```

- [ ] **Step 2: 失敗を確かめる** — Run: `uv run pytest tests/agent tests/integration -v` / Expected: FAIL
- [ ] **Step 3: 実装する**
- [ ] **Step 4: 通ることを確かめる** — Run: 同上 / Expected: PASS
- [ ] **Step 5: Commit** — `git commit -am "feat: agent task runner, session registry, control client"`

---

### Task 12: host の Web 管理画面と起動

**Files:**
- Create: `src/codex_bridge/host/web/app.py`, `src/codex_bridge/host/web/static/index.html`, `.../app.js`, `.../style.css`, `src/codex_bridge/host/main.py`
- Test: `tests/host/test_web.py`

**Interfaces:**
- Consumes: すべての host の部品
- Produces:
  - `create_app(config: HostConfig) -> FastAPI`（`/codex/mcp`・`/agent/ws` は `public_bind` で、`/claude/mcp`・`/admin/*` は 127.0.0.1 で待ち受ける。2 つの uvicorn サーバーを 1 プロセスで動かす）
  - 管理用 API（127.0.0.1 のみ）: `GET/PUT /admin/api/policy`、`GET /admin/api/approvals`、`POST /admin/api/approvals/{id}`（`{"approve": bool}`）、`GET /admin/api/tasks`、`GET /admin/api/tasks/{id}/events`、`GET /admin/api/usage`、`PUT /admin/api/thresholds`、`PUT /admin/api/log-visibility`、`POST /admin/api/invites`（`{"name"}` → `{"code"}`）、`DELETE /admin/api/invites/{name}`、`POST /admin/api/pause`（`{"paused": bool}`）、`GET /admin/api/events`（Server-Sent Events。確認待ち・タスクの変化を即時に流す）
  - 起動: `codex-bridge-host --config host.yaml`。初回は `host.yaml` と `policy.yaml` の見本を作って終了する。
  - Claude Code への登録方法を README に書く: `claude mcp add --transport http codex-bridge http://127.0.0.1:<local_port>/claude/mcp`

画面は 1 ページで、上から「全体停止スイッチ・使用量・接続状態」「確認待ち（許可／拒否のボタン、新着時にブラウザ通知）」「タスク一覧とログ」「権限の規則（YAML の編集欄と、実行モード・しきい値・知人に見せるログの範囲の切り替え）」「招待コードの発行・失効」。実行は閉じ込めていない旨を権限の欄に常に表示する。

- [ ] **Step 1: 失敗するテストを書く**

```python
async def test_admin_api_not_served_on_public_bind(public_client):
    assert (await public_client.get("/admin/api/policy")).status_code == 404

async def test_policy_put_validates_and_applies(admin_client, host):
    assert (await admin_client.put("/admin/api/policy", content="rules: [")).status_code == 400
    assert (await admin_client.put("/admin/api/policy", content=VALID_OFF)).status_code == 200
    assert host.policy.current().exec_mode == "off"

async def test_approve_via_api(admin_client, host): ...
async def test_invite_issue_and_revoke(admin_client, host): ...
async def test_first_run_writes_sample_configs(tmp_path): ...
```

- [ ] **Step 2〜4: 失敗→実装→通過を確かめる** — Run: `uv run pytest tests/host/test_web.py -v`
- [ ] **Step 5: 画面を目で確かめる** — host を起動し、ブラウザで `http://127.0.0.1:<local_port>/admin/` を開く。確認待ちを 1 件作り（`ask` モードで偽の実行）、許可ボタンで消えることを確かめる。
- [ ] **Step 6: Commit** — `git commit -am "feat: host admin web UI and entrypoint"`

---

### Task 13: agent の初回設定・ログ画面・exe 化

**Files:**
- Create: `src/codex_bridge/agent/setup.py`, `src/codex_bridge/agent/logview.py`, `src/codex_bridge/agent/main.py`, `codex-bridge-agent.spec`（PyInstaller）
- Test: `tests/agent/test_setup.py`, `tests/agent/test_logview.py`

**Interfaces:**
- Consumes: `parse_invite`（Task 8）、`AppServer`（Task 10）、`ControlClient`（Task 11）
- Produces:
  - データの置き場: `%LOCALAPPDATA%\codex-bridge-agent\`（`invite.bin`（DPAPI で暗号化）、`sessions.json`、`workdir\`（空のまま）、`bin\codex.exe`、`logs\`）
  - `find_codex() -> Path|None`（`PATH` → `bin\codex.exe` の順）
  - `download_codex(dest: Path) -> Path`（GitHub `openai/codex` の最新リリースから Windows x64 の CLI を取得。取得前に知人へ確認を出す）
  - `ensure_login(codex: Path) -> Literal["chatgpt","apikey","none"]`（`none` なら `codex login` を新しいコンソールで開いて待つ。`apikey` なら host に `hello.auth_mode="apikey"` を送り、QuotaGuard を無効にする）
  - 起動の流れ: 招待コードがなければ小さな入力画面（tkinter）で貼ってもらう → codex の確認 → ログイン確認 → 接続 → タスクトレイ常駐（`pystray`）。トレイのメニューは「ログ画面を開く」「一時停止」「終了」。
  - `logview`: `http://127.0.0.1:8766/`。host の `settings.log_visibility` に従い、`summary`=依頼文と最終報告、`files`=加えて道具の名前と対象パス、`all`=加えて道具の結果の本文。
  - exe: `uv run pyinstaller codex-bridge-agent.spec` → `dist/codex-bridge-agent.exe`（1 ファイル、コンソールなし）

- [ ] **Step 1: 失敗するテストを書く**

```python
def test_invite_stored_encrypted(tmp_path):
    save_invite(tmp_path, parse_invite(CODE))
    raw = (tmp_path / "invite.bin").read_bytes()
    assert b"tok" not in raw and load_invite(tmp_path).token == "tok"

def test_find_codex_prefers_path_then_bundled(tmp_path, monkeypatch): ...
def test_apikey_login_disables_quota_guard(...): ...

async def test_logview_respects_visibility(logview_client, store):
    store.set_visibility("summary")
    page = (await logview_client.get("/api/events")).json()
    assert all(e["kind"] in ("task_prompt", "final_report") for e in page)
```

- [ ] **Step 2〜4: 失敗→実装→通過を確かめる** — Run: `uv run pytest tests/agent/test_setup.py tests/agent/test_logview.py -v`
- [ ] **Step 5: exe を作って起動を確かめる** — Run: `uv run pyinstaller codex-bridge-agent.spec` / Expected: `dist/codex-bridge-agent.exe` ができ、ダブルクリックで招待コードの入力画面が出る
- [ ] **Step 6: Commit** — `git commit -am "feat: agent setup, log view, packaging"`

---

### Task 14: 本番に近い通し確認と手順書

**Files:**
- Create: `README.md`（こちら向け: 起動・Claude Code への登録・Tailscale のノード共有の手順）、`docs/friend-guide.md`（知人向け: Tailscale を入れる→共有の招待を受ける→exe を起動→招待コードを貼る→ログイン。画面ごとの説明を日本語で）

- [ ] **Step 1: こちらの PC だけで本物の Codex を使って通しで動かす** — host を起動し、`dist/codex-bridge-agent.exe` もこちらで起動（招待コードの URL は `http://127.0.0.1:<public_port>`）。Claude Code から `send_task`（「raw の中で needle を含むファイルを探し、件数を out/count.txt に書いて」）→ `get_status` → `get_result`。Expected: `out/count.txt` ができ、監査ログに `grep`・`write_file` が残り、Codex の内蔵シェルが使われていない（イベントに `commandExecution` がない）
- [ ] **Step 2: 追加指示・取り消し・セッション操作を確かめる** — `send_followup`、`undo_task`、`list_sessions` / `read_session` / `delete_session`。Expected: ツールで作ったスレッドだけが一覧に出る
- [ ] **Step 3: しきい値の動きを確かめる** — `set_thresholds(five_hour=1)` にして依頼 → Expected: Codex が報告して止まり、状態が `suspended_quota`、`resume_at` が入る。新しい依頼は「再開可能」の文で断られる。
- [ ] **Step 4: 手順書を書いて Commit** — `git add README.md docs/friend-guide.md && git commit -m "docs: setup guides"`
