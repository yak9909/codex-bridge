# codex-bridge

知人の PC で動く Codex に、こちらの PC の作業フォルダを操作してもらうための道具です。

- フォルダを丸ごと送りません。Codex はこちらの道具（MCP）を通して、必要なファイルだけを探し・読み・書きします。
- 権限（見せない／読むだけ／書ける、コマンド実行の扱い）は、動作中でもこちらで変えられます。
- こちらの Claude Code から、依頼・追加の指示・状態の確認・中止・使用量の確認・セッションの管理ができます。
- 知人の Codex の使用量（5 時間枠・週間枠）がしきい値を超えると、作業中の Codex に「区切りで止めて報告して」と指示し、穏やかに中断します。

## 配布物（すぐ使う場合）

GitHub の **Releases** から 2 つの zip を取得します。Python や uv は要りません。

| zip | 誰が使う | 使い方 |
|---|---|---|
| `codex-bridge-admin-v*.zip` | 依頼主（あなた） | 展開して `setup.bat` → `install-mcp.bat` → 以後は `start.bat` / `stop.bat`。Claude Code か Codex に「codex-bridge で○○を依頼して」と頼むだけで使えます。中の「はじめにお読みください.txt」を参照 |
| `codex-bridge-friend-v*.zip` | 知人 | 展開して `codex-bridge-agent.exe` を起動し、招待コードを貼る。中の「使い方.md」を参照 |

リリースの作り方（開発者向け）: `uv run python scripts/build_release.py` で `release/` に 2 つの zip ができます。

設計: [docs/2026-11-14-codex-bridge-design.md](docs/2026-11-14-codex-bridge-design.md)
知人向けの手順: [docs/friend-guide.md](docs/friend-guide.md)
運用手順（毎回の流れ・Claude Code への伝え方）: [docs/operation-guide.md](docs/operation-guide.md)

## しくみ

```
[こちらの PC]                                   [知人の PC]
 Claude Code ── /claude/mcp ─┐                   codex-bridge-agent.exe
 ブラウザ ───── /admin/ ─────┤ codex-bridge-host    ├─ codex app-server（知人のログインのまま）
                             └─ /codex/mcp ◀──────┤   Codex はこちらの道具だけを使う
                                /agent/ws  ◀──────┘   （接続はすべて知人側から。Tailscale 経由）
```

## こちらの準備

**ふつうは `setup.bat`（最初に 1 回）・`start.bat`（起動）・`stop.bat`（停止）をダブルクリックするだけで済みます。** 詳しくは [docs/operation-guide.md](docs/operation-guide.md)。以下は手作業で行う場合の手順です。

1. 依存関係を入れる: `uv sync`
2. ripgrep を取得する: `powershell -ExecutionPolicy Bypass -File scripts/fetch_rg.ps1`
3. Tailscale を入れてログインし、このPCの IP を調べる: `tailscale ip -4`（例: `100.101.102.103`）
4. 初回起動で設定の見本を作る: `uv run codex-bridge-host --config host.yaml`
5. `host.yaml` を書き換える
   - `share_root`: Codex に触らせるフォルダ
   - `data_dir`: 記録・取り消し用の保存・トークンの置き場（**share_root の外**）
   - `public_bind`: `<Tailscale の IP>:8765`
6. `data/policy.yaml`（権限の規則）を書き換える。保存するとすぐに反映されます。Web 画面からも編集できます。
7. 起動: `uv run codex-bridge-host --config host.yaml`
8. 管理画面を開く: http://127.0.0.1:8770/admin/
9. Claude Code に登録する:
   ```
   claude mcp add --transport http codex-bridge http://127.0.0.1:8770/claude/mcp
   ```

## 知人に渡すもの

1. `uv run pyinstaller codex-bridge-agent.spec` で作った `dist/codex-bridge-agent.exe`
2. Tailscale の共有の招待: Tailscale の管理画面 → Machines → このPC → Share → 招待リンクを作成
   （共有されたPCは、知人側からこちらへは接続できますが、こちらから知人のPCへは接続できません）
3. 招待コード: 管理画面の「招待コード」→「発行」（再表示できないので、その場でコピー）
4. [docs/friend-guide.md](docs/friend-guide.md)

## 権限の規則（policy.yaml）

```yaml
default: read                 # 規則に当たらない場所の扱い
rules:                        # 後に書いた規則が優先。ただし hidden は順番に関係なく常に優先（中身ごと隠れる）
  - {path: "analysis/**", level: write}
  - {path: "private", level: hidden}
exec:
  mode: allowlist             # off / allowlist / ask / all
  allow: ["python scripts/**/*.py *"]   # 最後の * は「任意の引数」
  confirm_modified_scripts: false       # true: タスク中に書き換えたファイルの実行は確認待ち
delete_needs_approval: true
paused: false
```

- 「見せない（hidden）」場所は、一覧・検索・エラー文のどれにも出ません。
- **コマンド実行は閉じ込めていません。** 実行されたスクリプトは、hidden の場所や共有フォルダの外も読めます（相手を信頼する前提の割り切り）。
- 確認待ち（コマンド実行・削除）は、Claude Code（`decide_approval`）か管理画面で許可／拒否します。30 分返事がなければ拒否になります。
- 長い出力は共有フォルダの `.codex-bridge/outputs/` に保存され、Codex は read_file で読めます。

## Claude Code から使う道具

| 道具 | 内容 |
|---|---|
| `send_task(prompt, continue_task_id?)` | 依頼する。続きなら前のタスクの ID を渡す |
| `send_followup(task_id, text)` | 作業中の Codex に追加の指示 |
| `get_status(task_id?, wait_sec?)` / `get_result(task_id)` | 状態・最近の出来事・承認待ち（wait_sec で最大 50 秒待つ）／最終報告・変更ファイル |
| `cancel_task(task_id)` | 中止 |
| `get_usage()` / `set_thresholds(five_hour?, weekly?)` | 使用量としきい値 |
| `undo_task(task_id)` | ファイル操作の道具による変更を元に戻す（コマンド実行による変更は戻らない） |
| `list_sessions()` / `read_session(id)` / `delete_session(id)` | このツールで作ったセッションのみ |
| `list_approvals()` / `decide_approval(id, approve)` | 承認待ちの一覧／許可・拒否 |
| `get_policy()` / `set_policy(text)` | 権限の規則を読む／全文で置き換える（誤りがあれば保存しない） |
| `set_exec_mode(mode)` / `set_paused(paused)` / `set_log_visibility(value)` | 実行モード・一時停止・知人に見せるログの範囲 |
| `list_tasks()` / `get_overview()` / `get_guide()` | タスクの一覧／全体の状態／エージェント向けの使い方 |
| `set_folder_level(path, level)` / `set_share_root(path)` | フォルダごとの権限／共有フォルダの変更 |
| `issue_invite(name)` / `revoke_invite(name)` / `list_invites()` | 招待コード |

使用量の上限で中断したタスク（`suspended_quota`）は、`resume_at` 以降に `send_task(..., continue_task_id=<そのタスク>)` で続けられます。

## 開発

- テスト: `uv run pytest`
- 知人側の exe: `uv run pyinstaller codex-bridge-agent.spec`

## 注意（規約）

OpenAI の規約は、アカウントを他人に使わせることを禁じていると読めます。この道具は認証情報を渡しませんが、知人の Codex を遠隔で動かす形なので、グレーであることに変わりはありません。困るのは知人のアカウントなので、事前に知人の了解を得てください。
