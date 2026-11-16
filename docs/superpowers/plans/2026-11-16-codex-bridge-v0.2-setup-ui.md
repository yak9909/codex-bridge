# codex-bridge v0.2 — 共有フォルダを画面で設定・起動ダイアログの見た目（計画）

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development。実装は Codex gpt-6.1-sol、検査は opus。

**Goal:** 共有フォルダが未設定・見本のまま・存在しない場所でも host を止めず、管理画面で選べるようにする。起動ダイアログの選択肢を 2 行（大きい 1 行目＋小さい説明）にする。

**Spec:** 利用者の指摘（下の Requirements）と、v0.2 の設計 `docs/2026-11-14-codex-bridge-v2-design.md`、前の計画 `docs/superpowers/plans/2026-11-16-codex-bridge-v0.2-feedback.md`（Global Constraints はそのまま有効）。

## Requirements（利用者の指摘 2026-11-16、原文）

- S1: ダイアログで「使う側」を選ぶと
  ```
  share_root がありません: C:\path\to\your\folder
  Enter キーを押すと閉じます。
  ```
  と言われます。そもそもこれは画面で設定できるべきです。
- S2: ダイアログも UI デザインが良くありません。
  「使う側」二行目に小さく「他の人の Codex を使う」
  「貸す側」二行目に小さく「自分の Codex を他の人に使わせる」

## 今の作り（調べた結果）

- `host/main.py:18` の `SAMPLE_HOST` が `share_root: "C:/path/to/your/folder"` を書き、`ensure_config` は `sample_policy(cfg.share_root)` で存在しない場所を policy.yaml の main に書く。`serve()`（`host/main.py:57-58`）は share_root が無いと `SystemExit`。
- `config.py` の `load_host_config` は `raw["share_root"]` が必須。`build_host`（`host/app.py:69-71`）は `SafePaths(config.share_root, policy, ...)`。
- 管理画面には、PC 側のフォルダ選択（`POST /admin/api/pick-folder` → `share.pick_folder_dialog`、`asyncio.to_thread` 済み）と、共有フォルダの変更（`PUT /admin/api/share-root` → `share.change_share_root`、書けるフォルダとして追加し main にする）が既にある。
- `setup_cli.run_setup` の手順 3 は、host.yaml があると「選び直しますか？」の既定が No で、見本の場所のまま進む。
- ダイアログは `launcher.py:choose_role`（Tk の Button 2 つ、1 行の文）。

## Global Constraints

- 前の計画（2026-11-16-codex-bridge-v0.2-feedback）の Global Constraints をすべて守る。特に: データフォルダ・インストール先の保護（C1、Task 2 の判定）、利用者に見せる文は平易な日本語。
- テストは `uv run --no-sync pytest tests -q`。release/ の v0.1.0 は変えない。
- 画面で選んだ共有フォルダにも、データフォルダの保護が効くこと（`_guard` を通る）。

## Rulings（計画を書いた時点）

- R-a: 直すのは共有フォルダの設定だけ。Tailscale・ファイアウォール・ripgrep の手順は setup.bat（コンソール）のまま — 利用者の指摘は共有フォルダとダイアログ — 外れたときの費用: 次の指摘で同じ形の作業がもう 1 回。
- R-b: 見本の場所・空・存在しない share_root は「未設定」と同じに扱う。未設定でも host は起動し、管理画面が開く。依頼と道具は平易な言葉で断る（落ちない）— 外れたときの費用: 小さい。
- R-c: 画面での選び方は、既存の「PC 側のフォルダ選択」と「共有フォルダの変更」を使い回す。選んだら再起動なしで使える（`change_share_root` は実行中に変えられる）。

## Review Focus

1. 見本の場所が入った host.yaml / policy.yaml（利用者の今の状態）から、ダイアログ→使う側で、エラーでなく管理画面が開き、そこで共有フォルダを選ぶと使えるようになる。
2. 未設定の間、Codex の道具・依頼・MCP（Claude 側の道具）がどれも落ちず、平易な言葉で断る。
3. 画面で選んだフォルダにもデータフォルダの保護が効く。
4. ダイアログの見た目（2 行、指定の文言、中央、キー操作）。

### Task 1: 共有フォルダが未設定でも起動し、管理画面で選べるようにする（S1）

- [ ] **Step 1:** `load_host_config` で share_root を省略可にし、見本の場所（`C:/path/to/your/folder`）・空・存在しないフォルダは「未設定」として扱う（例: `share_root: Path | None` か、未設定を表す判定関数）。`serve()` は未設定でも終了しない（data_dir が share_root の中にある判定は、設定済みのときだけ）。起動時の表示は「共有フォルダ: 未設定（管理画面で選んでください）」。
- [ ] **Step 2:** `SAMPLE_HOST` に偽の場所を書かない（share_root の行を出さないか空にする）。`ensure_config` / `sample_policy` は、未設定なら policy.yaml に main もフォルダも書かない。既に見本の場所が入った policy.yaml（利用者の今の状態）も、存在しないフォルダとして未設定扱いになり、落ちない。
- [ ] **Step 3:** `build_host` / `SafePaths` / policy の main が未設定でも動く。未設定の間、Codex の道具（list_dir・read_file・find_files・grep・書き込み・run_command）と Claude 側の MCP の道具、依頼の開始は、「共有フォルダがまだ設定されていません。管理画面で選んでください。」の趣旨で断る（例外で落ちない、HTTP 500 にしない）。
- [ ] **Step 4:** 管理画面: 未設定のとき、ページの最初に目立つ案内「共有フォルダがまだ設定されていません」と「フォルダを選ぶ」ボタンを出す。ボタンは既存の `pick-folder` → `share-root` を使う。選んだら案内が消え、フォルダ一覧に出て、再起動なしで使える。選んだ場所がデータフォルダ・インストール先を含むなど `_guard` に断られたら、その理由を案内のそばに出す。共有フォルダを選んだら host.yaml の share_root も書き換える（次の起動でも未設定にならない）。
- [ ] **Step 5:** `setup_cli.run_setup` の手順 3: host.yaml の share_root が未設定（見本・空・存在しない）なら、「選び直しますか？」を聞かずに選ぶ画面を出す。`run_start` は未設定でも起動する（管理画面で選べることを 1 行で言う）。
- [ ] **Step 6:** テスト: 見本の場所の host.yaml＋policy.yaml から `serve` 相当が終了しない／未設定で各道具が平易なエラーを返す／`share-root` で設定すると道具が使えるようになり host.yaml が更新される／データフォルダを含む場所は断られる／`ensure_config` が偽の場所を書かない。文書（`docs/operation-guide.md` の初期設定の節）を 1〜2 文で更新。

### Task 2: 起動ダイアログを 2 行の選択肢にする（S2）

- [ ] **Step 1:** `launcher.py:choose_role` の選択肢を 2 つのカード（Frame＋Label 2 つ）にする。1 行目は大きめの太字「使う側」/「貸す側」、2 行目は小さく灰色で、文言は利用者の指定どおり:
  - 使う側: 「他の人の Codex を使う」
  - 貸す側: 「自分の Codex を他の人に使わせる」
  カード全体（Label の上も含む）のクリックで選べる。マウスを乗せると背景が変わる。
- [ ] **Step 2:** キー操作: Tab / 上下矢印で選択の移動、Enter / Space で決定、Esc / 閉じるボタンで何もせず終了（戻り値 None）。選んでいるカードは枠で分かる。
- [ ] **Step 3:** 画面の中央に出す。窓のアイコンを Tk の羽根から codex-bridge のアイコン（あれば exe と同じ .ico、無ければ既定の Windows のアイコン）にする。見出し「この PC での使い方を選んでください。」は残す。
- [ ] **Step 4:** テスト: 選択肢の文言、クリック・Enter・Esc の戻り値（Tk を表示せずに検証できる形、または `update()` で実際に作って破棄）。

### Task 3: 本物での確認と撮影

このタスクは確認。コードは直さず、失敗は記録する。

- [ ] **Step 1:** release/ を退避してから `uv run --no-sync python scripts/build_release.py`。一時フォルダに admin の zip を展開し、利用者の状態を再現した host.yaml（`share_root: "C:/path/to/your/folder"`、試験用のポート）と policy.yaml を置く。
- [ ] **Step 2:** exe を引数なしで起動 → ダイアログを撮影（2 行の文言、中央）→ 使う側（クリックできなければ Enter）→ エラーで止まらず管理画面が開く → 未設定の案内を撮影 → 試験用のフォルダを設定（フォルダ選択が押せなければ `PUT /admin/api/share-root`）→ 案内が消え、フォルダ一覧に出る → host.yaml が更新されたことを確かめる → 再起動しても設定が残る。
- [ ] **Step 3:** 未設定の間に Claude 側の MCP の道具（例: 状態の確認）と Codex の道具を 1 回ずつ呼び、平易な言葉で断ることを確かめる。
- [ ] **Step 4:** 起動したものをすべて止め、`git status` が空で、release/ の v0.1.0 が元のままであることを確かめる。撮影画像は作業フォルダの `shots/task-3/`。
