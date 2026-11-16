# codex-bridge

知人の PC で動く Codex に、こちらの PC の作業フォルダを操作してもらうための道具です。

- フォルダを丸ごと送りません。Codex はこちらの道具（MCP）を通して、必要なファイルだけを探し・読み・書きします。
- v0.2 では、複数の知人に別々の依頼を並行して頼めます。新しい依頼は使用量にいちばん余裕のある人へ自動で送り、名前での指定もできます。続きは前と同じ人に届きます。
- 共有フォルダは、フルパス（絶対パス）で複数登録できます。相対パスの起点にする「メイン」も選べます。
- 権限（見せない／読むだけ／書ける、コマンド実行の扱い）は、動作中でもこちらで変えられます。
- こちらの Claude Code や Codex から、依頼・追加の指示・状態の確認・中止・使用量の確認・セッションの管理ができます。
- 知人の Codex の使用量（5 時間枠・週間枠）が上限に達すると、新しい依頼を止めます。作業中の Codex には区切りで止めて報告するよう頼み、知人の設定によっては強制的に止めます。
- 知人も自分の画面で、依頼や作業の記録を確認し、上限・機能の制限・事前承認・会話の保存・データの保存場所を決められます。

## 配布物（すぐ使う場合）

GitHub の **Releases** から zip を 1 つ取得します。v0.2.0 の配布物は次の名前です。Python や uv は要りません。依頼主も知人も、同じ zip を使います。

| zip | 誰が使う | 使い方 |
|---|---|---|
| `codex-bridge-v0.2.0.zip` | 依頼主と知人の両方 | 展開して、中の「はじめにお読みください.txt」を参照 |

- **知人（貸す側）**: 展開して `codex-bridge.exe` をダブルクリックし、「貸す側」を選んで、依頼主からもらった自分用の招待コードを貼ります。ほかのファイルは使いません。
- **依頼主（使う側）**: 展開して `codex-bridge.exe` をダブルクリックし、「使う側」を選びます。初回は初期設定のあと、そのまま起動して管理画面が開きます。管理画面で共有フォルダを選び、`install-mcp.bat` を実行し、招待コードを発行して、この zip と一緒に知人に渡します。2 回目からは exe で「使う側」を選ぶか `start.bat`、停止は `stop.bat` です。初期設定だけしたいときは `setup.bat` を使います。

- **以前の版から更新するとき**: 先に古いフォルダの `stop.bat` で止める（または古い黒いウィンドウを閉じる）。新しい zip は別のフォルダに展開されるので、古いフォルダの `host.yaml` と `data` フォルダを新しいフォルダにコピーしてから「使う側」を選ぶか `start.bat` を実行します。コピーしないと新しく初期設定が始まり、新しい招待コードが必要になり、権限の規則と共有フォルダも引き継がれません。

exe は全員が同じ `codex-bridge.exe` です。引数なしで開くと役割を選ぶ画面が出ます。「使う側」は exe の隣に `host.yaml` がなければ初期設定のあと起動し、あれば起動だけを行います。「貸す側」がすでに動いていれば、新しく起動せずブラウザでその画面を開きます。

v0.1 の exe も、v0.2 の依頼主側につなげられます。知人側の新しい画面と設定には v0.2 の exe が必要です。更新するときは依頼主側を先に更新してください。
メイン以外のフォルダを使うには、知人側の exe も v0.2 が必要です（v0.1 の exe はメインのフォルダだけで作業できます）。

リリースの作り方（開発者向け）: `uv run python scripts/build_release.py` で `release/` に zip が 1 つ（`codex-bridge-v{バージョン}.zip`）できます。

設計: [v0.2 の設計書](docs/2026-11-14-codex-bridge-v2-design.md)（[v0.1](docs/2026-11-14-codex-bridge-design.md)）
知人向けの手順: [docs/friend-guide.md](docs/friend-guide.md)
運用手順（毎回の流れ・Claude Code への伝え方）: [docs/operation-guide.md](docs/operation-guide.md)

## しくみ

```
[こちらの PC]                                   [知人の PC]
 Claude Code ── /claude/mcp ─┐                   codex-bridge.exe（貸す側）
 ブラウザ ───── /admin/ ─────┤ codex-bridge（使う側）├─ codex app-server（知人のログインのまま）
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
   - `share_root`: 最初のメインの共有フォルダ
   - `data_dir`: 記録・取り消し用の保存・トークンの置き場（**どの共有フォルダにも含めない**）
   - `public_bind`: `<Tailscale の IP>:8765`
6. `data/policy.yaml`（権限の規則）を書き換える。保存するとすぐに反映されます。Web 画面からも編集できます。
7. 起動: `uv run codex-bridge-host --config host.yaml`
8. 管理画面を開く: http://127.0.0.1:8770/admin/
9. Claude Code に登録する:
   ```
   claude mcp add --transport http codex-bridge http://127.0.0.1:8770/claude/mcp
   ```

## 知人に渡すもの

1. `uv run pyinstaller codex-bridge.spec` で作った `dist/codex-bridge.exe`（ダブルクリックして「貸す側」を選びます）
2. Tailscale の共有の招待: Tailscale の管理画面 → Machines → このPC → Share → 招待リンクを作成
   （共有されたPCは、知人側からこちらへは接続できますが、こちらから知人のPCへは接続できません）
3. 招待コード: 管理画面の「招待コード」で知人ごとに別の名前を付けて「発行」（1 人に 1 つ。再表示できないので、その場でコピー）。この名前が依頼の送り先になります
4. [docs/friend-guide.md](docs/friend-guide.md)

## 送り先と共有フォルダ

管理画面の「送り先」で「自動（使用量に余裕のある人）」か、知人の名前を選べます。自動では、接続中・作業中でない・上限で止められていない人から選びます。余裕は、こちらのしきい値と本人の上限の低いほうから今の使用率を引き、5 時間枠・週間枠のうち小さいほうで比べます。作業は 1 人につき同時に 1 つです。

続きは前と同じ人にしか送れません。未接続・作業中・上限による停止中なら待つか、前回の報告を添えて新しく依頼してください。使い捨てのセッションや削除した会話には、終わったあとの続きは頼めません。

共有フォルダは「共有フォルダと権限」で、フルパスを入れるか「参照…」から選び、「読むだけ／書ける」を選んで追加します。「メインにする」で相対パスの起点を変えられます。フォルダの追加・一覧からの削除・メインの切り替えはすぐに反映されますが、作業中や承認待ちのタスクがある間は変えられません。権限は「保存して反映」で、作業中でも変えられます。
依頼には、メインの中を `src/app.py`、それ以外を `D:/tools/common-lib/helper.py` のように書きます。

## 知人の画面と設定

知人はトレイの CB アイコン →「画面を開く」で、`http://127.0.0.1:8766/` を開けます。「接続先」は依頼主の名前ではなく PC の住所です。タスクを選ぶと、途中経過・計画・最終報告を見られ、「簡易／詳細」で記録を切り替えられます。今の Codex では、考えの要約は空のことがよくあります。

- **上限と機能**: 上限の初期値は 5 時間枠・週間枠とも 80%、強制停止はオフ。Web 検索・Web の取得、画像生成、サブエージェント、目標機能は初期値で止める設定です。ただし、**サブエージェントの停止はベストエフォート**で、今の Codex では確実には止められません。機能の変更は、作業が終わってから Codex を起動し直して反映します。
- **事前承認**: 新しい依頼と追加の指示を、自分が許可してから Codex に渡す設定です。初期値はオフ。待ち時間は初期値 30 分で、過ぎると拒否します。
- **会話と記録**: 初期値は会話を残します。使い捨てなら作業中の追加の指示は受けられますが、終わったあとの続きは受けられません。使い捨てや会話の削除でも作業の記録は残り、初期値 30 日の保管日数を変えたり、「記録を消す」で消したりできます。依頼文・追加の指示・発言・計画・状態・最終報告はいつも見せ、道具の表示範囲だけを依頼主が決めます。保存ログには道具の記録も残ります。
- **データの保存場所**: 初期値は `%LOCALAPPDATA%\codex-bridge-agent`。招待コード（暗号化済み）・設定・セッションの一覧・ログ・取得した Codex などを置きます。作業や承認待ちがないとき、設定で空の移動先を指定すると、データを移したあと **exe が自分で再起動します**。元の場所の `location.txt` は残します。Codex 自身の `~/.codex` の記録の場所は変わりません。
- **作業フォルダ**: 既定は `C:\ProgramData\codex-bridge-agent\work-<ランダム>`。作れない場合はデータの保存場所の `workdir` を使い、設定から変更もできます。ユーザー名を含むパスが Codex を通して依頼主に伝わるのを避けるための場所です。

詳しい操作は [知人向けの手順](docs/friend-guide.md)を見てください。

## 権限の規則（policy.yaml）

```yaml
main: "D:/work/project"       # 相対パスの起点
folders:                     # 一覧に含まれない場所は見えない
  - {path: "D:/work/project", level: write}
  - {path: "D:/tools/common-lib", level: read}
  - {path: "D:/work/project/private", level: hidden}
  - {path: "D:/work/project/vendor", level: read}
exec:
  mode: allowlist             # off / allowlist / ask / all
  allow: ["python scripts/**/*.py *"]   # 最後の * は「任意の引数」
  confirm_modified_scripts: false       # true: タスク中に書き換えたファイルの実行は確認待ち
delete_needs_approval: true
paused: false
```

- 「見せない（hidden）」場所は、一覧・検索・エラー文のどれにも出ません。
- 中のフォルダに別の規則があれば、より深いものが優先します。ただし hidden は常に優先し、中身ごと隠します。v0.1 の規則は起動時に変換され、ワイルドカードを使った規則は `advanced_rules` に残ります。
- **コマンド実行は閉じ込めていません。** 実行されたスクリプトは、hidden の場所や共有フォルダの外も読めます（相手を信頼する前提の割り切り）。
- 確認待ち（コマンド実行・削除）は、Claude Code（`decide_approval`）か管理画面で許可／拒否します。30 分返事がなければ拒否になります。
- 長い出力はメインの共有フォルダの `.codex-bridge/outputs/` に保存され、Codex は read_file で読めます。

## Claude Code から使う道具

| 道具 | 内容 |
|---|---|
| `send_task(prompt, continue_task_id?, lender?)` | 依頼する。lender を省くと自動、指定するなら招待コードの名前。続きは前のタスクの ID を渡し、前と同じ人へ送る |
| `list_lenders()` | 知人ごとの接続・使用量・上限・担当タスク・事前承認・続きが可能かを確認 |
| `send_followup(task_id, text)` | 作業中の Codex に追加の指示 |
| `get_status(task_id?, wait_sec?)` / `get_result(task_id)` | 状態・最近の出来事・承認待ち（wait_sec で最大 50 秒待つ）／最終報告・変更ファイル |
| `cancel_task(task_id)` | 中止 |
| `get_usage()` / `set_thresholds(five_hour?, weekly?)` | 知人ごとの使用量と、こちらのしきい値 |
| `undo_task(task_id)` | ファイル操作の道具による変更を元に戻す（コマンド実行による変更は戻らない） |
| `list_sessions(lender?)` / `read_session(id, lender?)` / `delete_session(id, lender?)` | このツールで作ったセッションのみ。複数の人が接続中なら lender で指定 |
| `list_approvals()` / `decide_approval(id, approve)` | 承認待ちの一覧／許可・拒否 |
| `get_policy()` / `set_policy(text)` | 権限の規則を読む／全文で置き換える（誤りがあれば保存しない） |
| `set_exec_mode(mode)` / `set_paused(paused)` / `set_log_visibility(value)` | 実行モード・一時停止・知人に見せるログの範囲 |
| `list_tasks()` / `get_overview()` / `get_guide()` | タスクの一覧／全体の状態／エージェント向けの使い方 |
| `set_folder_level(path, level)` | フォルダごとの権限。フルパスか、メインからの相対パス |
| `add_shared_folder(path, level)` / `remove_shared_folder(path)` / `set_main_folder(path)` | フルパスで共有フォルダを追加／登録を外す／メインにする。旧名 `set_share_root(path)` も使えます |
| `issue_invite(name)` / `revoke_invite(name)` / `list_invites()` | 招待コード |

使用量の上限で中断したタスク（`suspended_quota`）は、会話が残っていれば `resume_at` 以降に `send_task(..., continue_task_id=<そのタスク>)` で同じ人に続けてもらえます。使い捨てなら、報告を添えて新しく依頼してください。

## 開発

- テスト: `uv run pytest`
- 共通の exe: `uv run pyinstaller codex-bridge.spec`

## 注意（規約）

OpenAI の規約は、アカウントを他人に使わせることを禁じていると読めます。この道具は認証情報を渡しませんが、知人の Codex を遠隔で動かす形なので、グレーであることに変わりはありません。困るのは知人のアカウントなので、事前に知人の了解を得てください。
