# codex-bridge

貸す人の PC で動く Codex に、依頼主の PC の作業フォルダを操作してもらうための道具です。

- フォルダを丸ごと送りません。Codex は依頼主の道具（MCP）を通して、必要なファイルだけを探し・読み・書きします。
- 1 人にも複数の貸す人にも、別々の依頼を並行して頼めます。新しい依頼は、作業中の件数が少ない人へ送り、件数が同じなら使用量に余裕のある人へ送ります。名前で相手を指定することもできます。続きは前と同じ人に届きます。
- 共有フォルダはフルパスで複数登録でき、相対パスの起点にする「メイン」も選べます。
- コマンド実行・削除の承認・相手に見せる記録は、貸す人ごとのカードで決めます。新しい招待のコマンド実行は「毎回承認」です。
- 使用量の上限は貸す人が決めます。依頼主が自分で上限を設定する操作はありません。
- Claude Code や Codex から、依頼・追加の指示・状態の確認・中止・使用量の確認・会話の管理ができます。
- 承認待ちになったときと終わったときに、依頼した Claude Code や Codex に知らせが届きます。`install-mcp.bat` でフックも入り、Codex では `/hooks` でそのフックを信頼してから使います。
- 依頼ごとにモデルと推論の強さを指定できます。貸す人は依頼主ごとのカードで、使えるモデル・推論の強さ・機能・始める前の許可・続きの依頼を設定できます。

## 配布物（すぐ使う場合）

GitHub の **Releases** から `codex-bridge-v0.3.0.zip` を取得して展開します。Python や uv は要りません。依頼主も貸す人も、同じ zip を使います。

`codex-bridge.exe` をダブルクリックすると、タスクトレイに常駐します。トレイの CB アイコンを左クリックするか、右クリック →「画面を開く」で http://127.0.0.1:8766/ を開きます。画面には「使う」「貸す」「ログ」のタブがあります。もう一度 exe を開くと、動いているアプリの画面が開きます。

- **依頼主**: 「使う」タブの「準備を始める」を押します。Tailscale とファイアウォールを設定するコンソールが開き、終わると「使う」が自動で動き始めます。画面で共有フォルダを選び、`install-mcp.bat` を実行します。「使う」→「招待」→ 名前を入力 →「招待コードを発行」で、貸す人ごとに別のコードを作り、zip と一緒に渡します。
- **貸す人**: 「貸す」タブに招待コードを貼り、「つなぐ」を押します。依頼主ごとの設定は、それぞれのカードを開いて変えられます。
- **停止**: トレイの「終了」を使います。コンソールで起動する方法も使えます。`setup.bat` は初期設定、`start.bat` は起動、`stop.bat` は停止です。`codex-bridge.exe host setup|start|stop|install-mcp` も使えます。

Claude Code / Codex の接続先は引き続き `http://127.0.0.1:8770` です。`install-mcp.bat` の使い方は変わりません。コンソールで起動した場合は、同じ画面の「使う」を `http://127.0.0.1:8770/admin/` で開けます。

**以前の版から更新するとき**: 古いアプリをトレイの「終了」で止めます。コンソールで起動している場合は古いフォルダの `stop.bat` を使います。新しい zip を別のフォルダに展開し、古いフォルダの `host.yaml` と `data` をコピーしてから exe を開いてください。コピーしない場合は、共有フォルダ・権限・招待コードを引き継げません。新しいフォルダの `install-mcp.bat` を実行し直し、Codex では `/hooks` でフックを信頼してください。

v0.1 の exe も v0.3.0 の依頼主につなげられますが、この版の画面と設定には v0.3.0 の exe が必要です。依頼主から先に更新してください。v0.2 の exe ではモデルと推論の強さを指定できず、v0.1 の exe はメインの共有フォルダだけで作業できます。古い版から届かない設定は、カードに「—」と表示します。

リリースの作り方（開発者向け）: `uv run python scripts/build_release.py` で `release/` に zip ができます。

設計: [v0.2 の設計書](docs/2026-11-14-codex-bridge-v2-design.md)（[v0.1](docs/2026-11-14-codex-bridge-design.md)）
貸す人向けの手順: [docs/friend-guide.md](docs/friend-guide.md)
運用手順（毎回の流れ・Claude Code への伝え方）: [docs/operation-guide.md](docs/operation-guide.md)

## しくみ

- 依頼主と貸す人は、それぞれの PC で同じアプリを動かし、`http://127.0.0.1:8766/` の画面を使います。
- 依頼主の Claude Code / Codex は、依頼主の PC の `http://127.0.0.1:8770/claude/mcp` につなぎます。
- 貸す人の Codex は、Tailscale 経由で依頼主の `/codex/mcp` と `/agent/ws` につなぎます。依頼されたファイルは依頼主の PC に置いたまま扱います。

## ソースから動かす準備（開発者向け）

**ふつうは配布物の zip を使います（上の節）。以下はソースから動かす開発者向けの手順です。** zip 内の `setup.bat`（最初に 1 回）・`start.bat`（起動）・`stop.bat`（停止）の使い方は [docs/operation-guide.md](docs/operation-guide.md) を見てください。

1. 依存関係を入れる: `uv sync`
2. ripgrep を取得する: `powershell -ExecutionPolicy Bypass -File scripts/fetch_rg.ps1`
3. Tailscale を入れてログインし、この PC の IP を調べる: `tailscale ip -4`（例: `100.101.102.103`）
4. 初回起動で設定の見本を作る: `uv run codex-bridge-host --config host.yaml`
5. `host.yaml` を書き換える
   - `share_root`: 最初のメインの共有フォルダ
   - `data_dir`: 記録・取り消し用のデータ・トークンの置き場（**どの共有フォルダにも含めない**）
   - `public_bind`: `<Tailscale の IP>:8765`
6. `data/policy.yaml`（権限の規則）を書き換える。保存するとすぐに反映されます。Web 画面からも編集できます。
7. 起動: `uv run codex-bridge-host --config host.yaml`
8. 画面を開く（コンソールで起動した場合）: http://127.0.0.1:8770/admin/
9. Claude Code に登録する:
   ```
   claude mcp add --transport http codex-bridge http://127.0.0.1:8770/claude/mcp
   ```

## 貸す人に渡すもの（ソースから動かす場合）

1. `uv run pyinstaller codex-bridge.spec` で作った `dist/codex-bridge.exe`。ダブルクリックして画面を開き、「貸す」タブに招待コードを貼って「つなぐ」を押します。
2. Tailscale の共有の招待: Tailscale の画面 → Machines → この PC → Share → 招待リンクを作成。
3. 招待コード: 「使う」→「招待」→ 貸す人を見分ける名前 →「招待コードを発行」。その場でコピーしてください。この名前が依頼の送り先になります。
4. [docs/friend-guide.md](docs/friend-guide.md)

## 送り先と共有フォルダ

「使う」→「依頼」の入力欄で、相手を「自動」か貸す人の名前から選べます。自動では、接続中で、同時に受ける数と使用量に余裕のある人から、作業中の件数が少ない順に選びます。同じ件数なら、貸す人の使用量の上限から今の使用率を引き、5 時間枠・週間枠のうち小さいほうで比べます。同時に受ける数は、貸す人が「貸す」タブで 1〜8 の範囲で決めます（既定は 3）。この数には許可待ちの依頼も含み、上限はすべての依頼主に共通です。

続きは前と同じ人に届きます。相手が未接続のとき、同時に受ける数の上限に達しているとき、使用量の上限で止まっているとき、その会話の依頼が作業中のときは、待つか、前回の報告を添えて新しく依頼してください。使い捨ての会話や削除した会話には、終わったあとの続きは頼めません。

「使う」→「共有フォルダ」の「フォルダを追加」でフォルダを選び、権限を変えます。「メインにする」で相対パスの起点を変えられます。共有フォルダの追加と、登録を外す操作は、作業中でもできます。メインの切り替えは、作業が終わってから行ってください。設定は変更すると反映されます。許可リストのような複数行の入力は、欄からフォーカスを外すと反映されます。
依頼には、メインの中を `src/app.py`、それ以外を `D:/tools/common-lib/helper.py` のように書きます。

## 貸す人の画面と設定

「貸す」タブで、使用量と依頼主ごとのカードを確認できます。カードには依頼主の PC のアドレス（IP とポート）が出ます。「依頼」では依頼文・途中経過・計画・最終報告を、「この PC」では保存場所・作業フォルダ・記録を残す日数を確認できます。「ログ」タブにはアプリの動作の記録が出ます。

- **同時に受ける依頼**: 1〜8 件、既定 3 件。許可待ちを含め、つながっている依頼主すべての合計です。上限を下げても、すでに受けた依頼は続けます。
- **使用量の上限**: 初期値は 5 時間枠・週間枠とも 80%。貸す人が決め、すべての依頼主に共通です。上限を超えたときは「止めて報告させる」か「すぐ強制終了」を選べます。
- **依頼主ごとの設定**: 使えるモデル・推論の強さの上限・始める前の許可・続きの依頼・機能・この画面に出す記録を、各カードで変えられます。Web 検索・画像の生成・サブエージェント・目標は初期値でオフです。サブエージェントの制限はベストエフォートで、確実には止められません。
- **始める前の許可**: 新しい依頼と追加の指示を別々に選べます。初期値はどちらもオフ、待ち時間は 30 分です。
- **会話と記録**: 「続きの依頼」がオンなら会話を残します。オフなら新しい作業の会話を使い捨てにします。作業の記録は残り、カードの「記録を消す」で依頼主ごとに消せます。道具の表示範囲は「道具の名前と対象」「道具の結果」で選びます。
- **保存場所**: 初期値は `%LOCALAPPDATA%\codex-bridge-agent`。作業と承認待ちがないときに「この PC」から移せます。データを移すと exe が自分で起動し直します。Codex 自身の `~/.codex` は移しません。

詳しくは [貸す人向けの手順](docs/friend-guide.md)を見てください。

## 権限の規則（policy.yaml）

```yaml
main: "D:/work/project"       # 相対パスの起点
folders:                     # 一覧に含まれない場所は見えない
  - {path: "D:/work/project", level: write}
  - {path: "D:/tools/common-lib", level: read}
  - {path: "D:/work/project/private", level: hidden}
  - {path: "D:/work/project/vendor", level: read}
exec:
  allow: ["python scripts/**/*.py *"]   # 最後の * は「任意の引数」
  confirm_modified_scripts: false       # true: タスク中に書き換えたファイルの実行は確認待ち
lender_exec_modes:
  kimura: ask                 # Per lender; new invites use ask
lender_delete_approval:
  kimura: true
paused: false
```

- 「見せない（hidden）」場所は、一覧・検索・エラー文のどれにも出ません。
- 中のフォルダに別の規則があれば、より深いものが優先します。ただし hidden は常に優先し、中身ごと隠します。v0.1 の規則は起動時に変換され、ワイルドカードを使った規則は `advanced_rules` に残ります。
- **コマンド実行は閉じ込めていません。** 実行されたスクリプトは、hidden の場所や共有フォルダの外も読めます（相手を信頼する前提の割り切り）。
- 確認待ち（コマンド実行・削除）は、Claude Code（`decide_approval`）か「使う」タブで許可／拒否します。30 分返事がなければ拒否になります。
- 長い出力はメインの共有フォルダの `.codex-bridge/outputs/` に保存され、Codex は read_file で読めます。

## Claude Code から使う道具

| 道具 | 内容 |
|---|---|
| `send_task(prompt, continue_task_id?, lender?, model?, effort?)` | 依頼する。lender を省くと自動、指定するなら招待コードの名前。続きは前のタスクの ID を渡し、前と同じ人へ送る。model と effort は省くと、その人の Codex の既定（使える範囲内） |
| `list_lenders()` | 貸す人ごとの接続・使用量・上限・担当タスク・事前承認・続きが可能かを確認 |
| `send_followup(task_id, text)` | 作業中の Codex に追加の指示 |
| `get_status(task_id?, wait_sec?)` / `get_result(task_id)` | 状態・最近の出来事・承認待ち（wait_sec で最大 50 秒待つ）／最終報告・変更ファイル。ふつうは知らせで足ります。知らせが届かない環境のときだけ wait_sec で待ちます。 |
| `cancel_task(task_id)` | 中止 |
| `get_usage()` | 貸す人ごとの使用量と、貸す人が決めた上限 |
| `undo_task(task_id)` | ファイル操作の道具による変更を元に戻す（コマンド実行による変更は戻らない） |
| `list_sessions(lender?)` / `read_session(id, lender?)` / `delete_session(id, lender?)` | このツールで作ったセッションのみ。複数の人が接続中なら lender で指定 |
| `list_approvals()` / `decide_approval(id, approve)` | 承認待ちの一覧／許可・拒否 |
| `get_policy()` / `set_policy(text)` | 権限の規則を読む／全文で置き換える（誤りがあれば保存しない） |
| `set_exec_mode(mode, lender)` / `set_paused(paused)` / `set_log_visibility(value, lender)` | 貸す人ごとの実行モード・相手に見せる記録と、全体の一時停止。モードと記録には lender の指定が必要 |
| `list_tasks()` / `get_overview()` / `get_guide()` | タスクの一覧／全体の状態／エージェント向けの使い方 |
| `set_folder_level(path, level)` | フォルダごとの権限。フルパスか、メインからの相対パス |
| `add_shared_folder(path, level)` / `remove_shared_folder(path)` / `set_main_folder(path)` | フルパスで共有フォルダを追加／登録を外す／メインにする。旧名 `set_share_root(path)` も使えます |
| `issue_invite(name)` / `revoke_invite(name)` / `list_invites()` | 招待コード |

使用量の上限で中断したタスク（`suspended_quota`）は、会話が残っていれば `resume_at` 以降に `send_task(..., continue_task_id=<そのタスク>)` で同じ人に続けてもらえます。使い捨てなら、報告を添えて新しく依頼してください。

## 開発

- テスト: `uv run pytest`
- 共通の exe: `uv run pyinstaller codex-bridge.spec`

## 注意（規約）

OpenAI の規約は、アカウントを他人に使わせることを禁じていると読めます。この道具は認証情報を渡しませんが、貸す人の Codex を遠隔で動かす形なので、グレーであることに変わりはありません。困るのは貸す人のアカウントなので、事前に貸す人の了解を得てください。
