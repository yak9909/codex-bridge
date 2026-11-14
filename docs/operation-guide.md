# codex-bridge 運用手順書（管理者＝あなた向け）

知人の Codex に作業を頼むまでの手順と、新しい Claude Code のセッションで何を伝えればよいかをまとめます。
相手（知人）向けの手順は [friend-guide.md](friend-guide.md) です。

---

## A. 最初に一度だけやること

### A-1. 本番用の設定ファイルを作る

1. PowerShell で codex-bridge のフォルダに移ります。
   ```powershell
   cd A:\MyProjects\Others\codex-bridge
   ```
2. 一度起動すると、設定の見本ができます（起動はせずに終わります）。
   ```powershell
   uv run codex-bridge-host --config host.yaml
   ```
3. できた `host.yaml` を開いて、次の 3 か所を書き換えます。

   ```yaml
   share_root: "D:/path/to/your/analysis-folder"   # Codex に触らせるフォルダ（10GB 弱のフォルダ）
   data_dir: "data"                                 # 記録・取り消し用の保存・合言葉の置き場（share_root の外に置く）
   public_bind: "100.126.42.83:8765"                # この PC の Tailscale の IP（tailscale ip -4 で確認）
   local_port: 8770
   rg_path: "vendor/rg.exe"
   ```

4. `data/policy.yaml`（権限の規則）を書き換えます。例:

   ```yaml
   default: read                      # 規則に当たらない場所は「読むだけ」
   rules:
     - {path: "analysis/**", level: write}   # 書いてよい場所
     - {path: "private", level: hidden}      # 見せない場所（中身ごと隠れる。順番に関係なく優先）
   exec:
     mode: allowlist                  # 許可リストに合うコマンドは自動、それ以外は確認待ち
     allow:
       - "python scripts/**/*.py *"
     confirm_modified_scripts: false
   delete_needs_approval: true        # 削除は毎回確認待ち
   paused: false
   ```

   - `hidden` の場所は、Codex の一覧・検索・エラー文のどこにも出ません。
   - **コマンド実行は閉じ込めていません。** 実行されたスクリプトは hidden の場所や共有フォルダの外も読めます。
   - 保存するとすぐ反映されます。管理画面からも編集できます。

5. ripgrep がなければ取得します（`vendor\rg.exe` があれば不要）。
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\fetch_rg.ps1
   ```

### A-2. ファイアウォールを通す（管理者の PowerShell で 1 回）

スタートボタンを右クリック →「ターミナル (管理者)」で開き、次を実行します。

```powershell
New-NetFirewallRule -DisplayName "codex-bridge (Tailscale)" -Direction Inbound -Protocol TCP -LocalPort 8765 -RemoteAddress 100.64.0.0/10 -Action Allow
```

### A-3. Claude Code に codex-bridge を登録する（1 回）

PowerShell で次を実行します。どのプロジェクトの Claude Code からでも使えるようになります。

```powershell
claude mcp add --scope user --transport http codex-bridge http://127.0.0.1:8770/claude/mcp
```

- 登録は、**新しく始めたセッション**から有効になります。
- 毎回の許可の確認を省きたい場合は、Claude Code の設定の許可リストに `mcp__codex-bridge__*` を加えます（任意）。

### A-4. 知人に渡すものを用意する

| 渡すもの | 作り方・場所 |
|---|---|
| `codex-bridge-agent.exe` | `uv run pyinstaller codex-bridge-agent.spec` で作ると `dist\codex-bridge-agent.exe` ができます（コードを変えたら作り直す） |
| Tailscale の共有の招待リンク | https://login.tailscale.com/admin/machines → この PC の行の「…」→「Share...」→ リンクをコピー（30 日で期限切れ） |
| 招待コード | host を起動 → 管理画面 http://127.0.0.1:8770/admin/ →「招待コード」→ 名前を入れて「発行」→ その場でコピー（再表示できません） |
| 手順書 | `docs\friend-guide.md`（そのまま送るか、内容を伝える） |

- 招待コードには `public_bind` の住所が入っています。IP を変えたら発行し直してください。
- 渡すときは、OpenAI の規約上グレーな使い方であることを知人に伝え、了解をもらってください。

---

## B. 作業を頼むたびにやること

### B-1. host を起動する（PowerShell を開いたままにする）

```powershell
cd A:\MyProjects\Others\codex-bridge
uv run codex-bridge-host --config host.yaml
```

- このウィンドウを閉じると止まります。作業中は開いたままにしてください。
- この PC はスリープさせないでください（Tailscale が「Connected」のままである必要があります）。

### B-2. 管理画面で接続を確かめる

http://127.0.0.1:8770/admin/ を開き、右上が **「知人の agent に接続中」** なら準備完了です。
「未接続」なら、知人に codex-bridge（トレイの CB アイコン）が動いているか確かめてもらいます。

### B-3. 新しい Claude Code のセッションで、次の文を伝える

下の枠の中をそのままコピーして、最初のメッセージとして送ってください。`【 】` の部分だけ書き換えます。
（作業を頼むプロジェクトの `CLAUDE.md` に貼っておけば、毎回伝える必要はありません。）

```text
codex-bridge の MCP 道具（mcp__codex-bridge__*）を使って、知人の PC の Codex に作業を依頼してください。

前提:
- Codex は私の PC の共有フォルダ【D:/path/to/your/analysis-folder】を、codex-bridge の道具を通して読み書き・実行します。
- Codex に渡すパスは、すべて共有フォルダからの相対パスで書いてください（例: raw/data.csv）。
- 共有フォルダは私の PC にあるので、あなた（Claude）も結果のファイルを直接読んで確かめられます。

進め方:
1. 依頼文は具体的に書き、send_task で送る。返ってきた task_id を覚えておく。
2. 1〜2 分おきに get_status で進み具合を確かめる。
3. get_status に pending_approvals（承認待ち）が出たら、その内容を私に伝えて待つ。承認は私が管理画面で行う（あなたは承認できない）。
4. 終わったら get_result で最終報告と変更されたファイルを確かめ、必要なら共有フォルダのファイルを直接読んで検証し、私に要点を報告する。
5. 途中で方針を直したいときは send_followup、止めたいときは cancel_task を使う。
6. 状態が suspended_quota（使用量の上限で中断）になったら、resume_at の時刻と、Codex の報告（終えたこと・残り）を私に伝える。再開は、その時刻を過ぎてから send_task の continue_task_id に前のタスクの ID を渡して行う。
7. 作業前に get_usage で使用量を確かめ、上限に近ければ私に知らせる。
8. 間違った変更をしたタスクは、undo_task で元に戻せる（コマンド実行による変更は戻らない）。

今回の依頼:
【ここに頼みたいことを書く】
```

### B-4. 作業中にやること

- **確認待ち**（コマンド実行・削除）が出たら、管理画面で「許可」か「拒否」を押します。30 分そのままだと自動で拒否になります。ブラウザの通知を許可しておくと気づきやすくなります。
- **使用量**のしきい値は、管理画面の「使用量」欄か、Claude に `set_thresholds` で変えてもらえます（既定: 5 時間枠 90%、週間枠 95%）。知人も Codex を使うので、余裕を持たせてください。
- **全体を止めたい**ときは、管理画面右上の「全体を一時停止」。Codex の道具がすべて「停止中」を返します。

### B-5. 終わったら

- host のウィンドウで **Ctrl+C** を押して止めます。
- 知人との共有をやめるときは、管理画面で招待コードを「失効」し、Tailscale の管理画面で共有を解除します。

---

## C. 困ったとき

| 症状 | 対処 |
|---|---|
| 管理画面が「未接続」のまま | 知人の PC で CB アイコンが出ているか、両方の Tailscale が Connected か、ファイアウォール（A-2）、`public_bind` の IP |
| 知人の PC で「接続を断られました」 | 招待コードが失効・発行し直しされていないか。新しいコードを渡す（知人は `%LOCALAPPDATA%\codex-bridge-agent\invite.bin` を消して exe を起動し直すと、入力画面が再び出る） |
| 知人の PC でエラーの画面が出た | `%LOCALAPPDATA%\codex-bridge-agent\logs\crash.log` を送ってもらう |
| Codex が「承認待ち」で作業を終えてしまった | 管理画面で許可したあと、Claude に send_task の continue_task_id で続きを頼ませる |
| Claude が codex-bridge の道具を使えない | host が起動しているか。A-3 の登録のあと、**新しいセッション**を始めたか（`claude mcp list` で登録を確認） |
| 変更を取り消したい | Claude に `undo_task`、または管理画面のタスク詳細の「このタスクのファイル変更を取り消す」 |
