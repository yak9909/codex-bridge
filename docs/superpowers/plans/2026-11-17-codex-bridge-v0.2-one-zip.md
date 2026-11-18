# codex-bridge v0.2 — one zip, setup without folder, exe goes on to start — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One release zip for both people (friend: run the exe → 貸す側; owner: run the exe → 使う側 = setup then start in the same console), setup no longer asks for the shared folder or builds a friend kit.

**Architecture:** `host setup` loses the folder step and the friend-kit step and writes `share_root: null` in a new host.yaml (the folder is chosen on the admin page, setup-ui plan). A new `host setup --then-start` flag makes setup call `run_start` at the end; the exe's 使う側 path uses it when host.yaml is missing. `scripts/build_release.py` builds one zip.

**Tech Stack:** Python 3.12, PyInstaller onefile, pytest (`uv run --no-sync pytest tests -q`).

**Spec:** the user's request of 2026-11-17 (quoted below) + `docs/2026-11-14-codex-bridge-v2-design.md` + `docs/superpowers/plans/2026-11-16-codex-bridge-v0.2-setup-ui.md` (R-b: sample/empty/missing share_root = unset, host still starts).

> setupで共有フォルダを指定させていますが、これは画面で設定できるためやめてください。知人の一式を作るのもやめてください。adminとfriendで分けるのもやめます。一つのzipをadminとfriendに渡す設計にします。friendは何も考えることなくexeで貸す側を実行するだけ、adminは使う側を選びsetupを実行するといった感じです。また、exeから使う側を選んだ時に実行されるsetupが終了したあと、そのまま終わってしまいます。使う側を選んだのにsetupだけするといった状況になり混乱を招きます。コンソールはそのままにそのままcodex-bridgeを起動するようにしてください。

## Global Constraints

- User-facing text is Japanese, plain words (same tone as the existing setup_cli messages).
- Release zip name: `codex-bridge-v{VERSION}.zip` (VERSION from pyproject.toml, now 0.2.0), top folder `codex-bridge-v{VERSION}/`. Older zips/folders in `release/` (v0.1.0 admin/friend) are never touched; same-version `codex-bridge-admin-v{VERSION}.zip` / `codex-bridge-friend-v{VERSION}.zip` left by earlier builds are removed by the build so release/ holds one v0.2.0 zip.
- Zip contents: `codex-bridge.exe`, `setup.bat`, `start.bat`, `stop.bat`, `install-mcp.bat`, `vendor/rg.exe`, `docs/operation-guide.md`, `docs/friend-guide.md`, `はじめにお読みください.txt` (UTF-8 with BOM, LF, as now).
- The friend never needs a bat or a setting: double-click exe → 貸す側 → paste invite code (unchanged agent behaviour).
- `setup.bat` keeps doing setup only (it does not start the host); only the exe's 使う側 path chains setup → start.
- Do not change the admin page, the policy, or the agent.
- host.yaml keeps its comments; data_dir stays `<exe folder>/data` (the install-dir guard in `share.py:_guard` already refuses a shared folder that contains the install folder).

## Review Focus

1. Owner picks 使う側 on a fresh unzip → setup runs → at the end the host starts in the same console and the admin page opens showing the "unset" banner; nothing closes in between. (Task 2 test: `test_dialog_host_without_config_runs_setup_then_start`.)
2. Setup interrupted or failing (Ctrl+C, SystemExit(1) from a step) → start is NOT run, the dialog-started console still waits for Enter on error. (Task 2 test: `test_setup_failure_does_not_start`.)
3. Re-running setup with an existing host.yaml that already has a real share_root → setup leaves share_root and policy untouched (only public_bind may change). (Task 1 test: `test_setup_keeps_existing_share_root`.)
4. New host.yaml from setup → host starts unset (effective_share_root is None), not with a sample folder. (Task 1 test: `test_setup_writes_unset_share_root`.)
5. A release/ that still holds same-version admin/friend zips from an earlier build → the build removes those two and leaves v0.1.0 untouched. (Task 3 test: `test_build_removes_same_version_split_zips_only`.)

---

### Task 1: setup without the folder step and without the friend kit

**Files:**
- Modify: `src/codex_bridge/host/setup_cli.py` (remove `_choose_folder`, `_apply_choice_to_policy`, step 3 folder block, step 5 friend kit, `build_friend_kit`; renumber steps; final messages)
- Modify: `src/codex_bridge/host/setup_tools.py:48-61` (`write_host_config`)
- Test: `tests/host/test_setup_tools.py`, `tests/host/test_unset_share.py` (setup-path tests that used `_choose_folder` / `_apply_choice_to_policy`), `tests/test_entry.py:415-450` (friend-kit tests)

**Interfaces:**
- Produces: `write_host_config(path: Path, share_root: Path | None, data_dir: Path, ip: str, port=PUBLIC_PORT, local_port=8770, rg_path="vendor/rg.exe") -> None` — `share_root=None` writes the line `share_root: null` with the comment `# share_root: the folder the friend's Codex may work in (choose it on the admin page)`.
- Produces: `run_setup(config_path: Path, *, then_start: bool = False) -> None` (the flag is used in Task 2; in this task it only exists and defaults to False).

Behaviour of `run_setup` after this task (steps, total 4):
1. Tailscale の確認 (unchanged)
2. 検索用の部品（ripgrep）の用意 (unchanged)
3. 設定ファイルと権限の規則: if host.yaml is missing → `write_host_config(config_path, share_root=None, data_dir=root / "data", ip=ip)`; if it exists → `st.update_public_bind(config_path, ip)` with the existing "IP が変わっていた" message. Never touch share_root of an existing host.yaml. Then `ensure_config`, the existing policy lines and the notepad question. Add one line: 「共有フォルダ（Codex に触らせるフォルダ）は、起動したあとの管理画面で選びます。」
4. ファイアウォールの許可 (unchanged)
5→ becomes 完了: owner steps without friend-kit: 1. install-mcp.bat, 2. start.bat（または exe で「使う側」）, 3. 管理画面で共有フォルダを選ぶ, 4. 招待コードを発行し、**同じ zip** と一緒に知人に渡す（知人は exe をダブルクリックして「貸す側」）, 5. Tailscale の共有 (keep the existing question that opens the Tailscale admin page).

- [ ] **Step 1: Write failing tests**
  - `test_write_host_config_unset`: `write_host_config(cfg, share_root=None, ...)` → file contains `share_root: null`; `load_host_config(cfg).share_root is None`.
  - `test_setup_writes_unset_share_root`: run `run_setup` in tmp cwd with Tailscale IP, firewall, ensure-rg, `input` monkeypatched (answer defaults) → host.yaml exists, `effective_share_root(load_host_config(...)) is None`, no folder dialog was opened (monkeypatch any tkinter use to fail).
  - `test_setup_keeps_existing_share_root`: existing host.yaml with a real share_root (tmp folder) + policy.yaml with that main → after `run_setup`, the share_root line and policy.yaml bytes are unchanged.
  - `test_setup_does_not_build_friend_kit`: after `run_setup`, no `friend-kit` folder, and output contains no 「friend-kit」.
  - Delete tests for removed functions (`test_friend_kit_*` in tests/test_entry.py and tests/host/test_setup_tools.py; setup-path tests in tests/host/test_unset_share.py that drive `_choose_folder`) — replace, don't just drop, any assertion still meaningful (e.g. "setup with an existing sample policy leaves the host unset").
- [ ] **Step 2: Run them, expect FAIL** — `uv run --no-sync pytest tests/host/test_setup_tools.py tests/host/test_unset_share.py tests/test_entry.py -q`
- [ ] **Step 3: Implement** the behaviour above.
- [ ] **Step 4: Run the same command, expect PASS; then the full suite** `uv run --no-sync pytest tests -q`.
- [ ] **Step 5: Commit** `feat(setup): no shared-folder step and no friend kit; new host.yaml starts unset`

### Task 2: exe 使う側 runs setup and then starts in the same console

**Files:**
- Modify: `src/codex_bridge/host/main.py:86-91` (add `--then-start` to the argparse; pass to `run_setup`)
- Modify: `src/codex_bridge/host/setup_cli.py` (`run_setup(..., then_start=True)` ends by calling `run_start(config_path)`)
- Modify: `src/codex_bridge/entry.py:150-154`
- Test: `tests/test_entry.py`, `tests/host/test_setup_tools.py` (or the existing setup test file)

**Interfaces:**
- Consumes: `run_setup(config_path, *, then_start: bool = False)` from Task 1; existing `run_start(config_path)`.
- Produces: CLI `codex-bridge host setup --then-start --config host.yaml`.

Behaviour:
- `run_setup(..., then_start=True)`: the 完了 step prints 「初期設定が終わりました。このまま codex-bridge を起動します。この黒いウィンドウは閉じないでください（閉じると止まります）。」, keeps the Tailscale-admin question, then calls `run_start(config_path)` in the same process. The step list for the 完了 message drops "2. start.bat …" in this mode (the host is starting now) but still says start.bat / exe for next time.
- `then_start=False` (setup.bat) is unchanged from Task 1.
- entry.py dialog path: host.yaml missing → `_run_host(["setup", "--then-start", "--config", "host.yaml"], directory, from_dialog=True)`; present → `start` as now.
- If setup exits early (SystemExit with non-zero code, KeyboardInterrupt, exception) start is not called; the existing `pause_on_error` handling in `_run_host` applies.

- [ ] **Step 1: Write failing tests**
  - `test_dialog_host_without_config_runs_setup_then_start` (tests/test_entry.py): `choose_role` → "host", no host.yaml in the directory, `_run_host` captured → called once with `["setup", "--then-start", "--config", "host.yaml"]`, `from_dialog=True`.
  - `test_setup_then_start_calls_run_start`: `run_setup(cfg, then_start=True)` with steps monkeypatched → `run_start` called once with the config path, after the 完了 text; output contains 「このまま codex-bridge を起動します」.
  - `test_setup_without_then_start_does_not_start`: `then_start=False` → `run_start` not called.
  - `test_setup_failure_does_not_start`: a step raising `SystemExit(1)` (e.g. firewall helper) with `then_start=True` → SystemExit propagates, `run_start` not called.
  - `test_host_main_accepts_then_start`: `host.main` with argv `["setup", "--then-start", "--config", str(cfg)]` → `run_setup` called with `then_start=True`.
- [ ] **Step 2: Run, expect FAIL** — `uv run --no-sync pytest tests/test_entry.py tests/host -q -k "then_start or setup"`
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, expect PASS; full suite.**
- [ ] **Step 5: Commit** `feat(entry): 使う側 runs setup then starts the host in the same console`

### Task 3: one release zip and docs for one zip

**Files:**
- Modify: `scripts/build_release.py` (one staging folder `codex-bridge-v{VERSION}`, one zip; remove same-version `codex-bridge-admin-v{VERSION}.zip` / `codex-bridge-friend-v{VERSION}.zip` in release/ if present; new README text)
- Modify: `README.md` (release table → one row), `docs/operation-guide.md` (no friend-kit; folder chosen on the admin page; 使う側 = setup → start), `docs/friend-guide.md` (only if it mentions friend-kit / a separate zip)
- Test: the existing build_release test in `tests/test_entry.py` (~line 455-470) or a new `tests/test_build_release.py`

**Interfaces:**
- Produces: `release/codex-bridge-v{VERSION}.zip`.

`はじめにお読みください.txt` content (format `{ver}`), plain Japanese, two sections:
- 「知人（貸す側）の方へ」: codex-bridge.exe をダブルクリック →「貸す側」→ 依頼主からもらった招待コードを貼る。ほかのファイルは使いません。詳しくは docs\friend-guide.md。
- 「依頼主（使う側）の方へ」: 最初に Tailscale を入れてログイン → codex-bridge.exe をダブルクリックして「使う側」（初回は初期設定のあと、そのまま起動して管理画面が開く。setup.bat でも初期設定だけできる）→ 管理画面で共有フォルダを選ぶ → install-mcp.bat → 招待コードを発行し、この zip と一緒に知人に渡す → Tailscale でこの PC を知人と共有。毎回: exe で「使う側」か start.bat、停止は stop.bat か黒いウィンドウを閉じる。頼み方（今の文のまま）。詳しくは docs\operation-guide.md。

- [ ] **Step 1: Write failing tests** — `test_build_makes_one_zip` (monkeypatch `run` so PyInstaller/make_bats don't run; fake `dist/codex-bridge.exe`, `vendor/rg.exe`, bats, docs under a tmp ROOT) → release/ gets exactly `codex-bridge-v0.2.0.zip` whose names are the list in Global Constraints under `codex-bridge-v0.2.0/`; `test_build_removes_same_version_split_zips_only` → pre-existing `codex-bridge-admin-v0.2.0.zip`, `codex-bridge-friend-v0.2.0.zip`, `codex-bridge-admin-v0.1.0.zip`, `old-build/keep.txt` → the first two are gone, the last two unchanged. Adapt the existing test that keeps v0.1.0 files.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement; update README.md and docs.** `grep -rn "friend-kit\|admin-v\|friend-v" README.md docs/*.md scripts src` must return nothing (except historical docs under docs/superpowers and docs/2026-11-14-*, which stay as records).
- [ ] **Step 4: Run, expect PASS; full suite.**
- [ ] **Step 5: Commit** `build: one release zip for owner and friend`

### Task 4: real build check (verifier, no code changes)

- [ ] **Step 1:** Back up release/ (scratchpad), `uv run --no-sync python scripts/build_release.py`; release/ holds v0.1.0 (hash-identical) + one `codex-bridge-v0.2.0.zip`; list the zip.
- [ ] **Step 2:** Unzip into a scratch folder; run `codex-bridge.exe host setup --help` and `host setup --then-start --help`-equivalent check (argparse accepts the flag) with CREATE_NO_WINDOW; do NOT run the real setup (it opens a UAC prompt for the firewall on the user's screen).
- [ ] **Step 3:** Mark NOT VERIFIED and leave to the user: double-click → 使う側 on a fresh unzip → setup → start in the same console → admin page with the unset banner.
- [ ] **Step 4:** Stop everything, `git status` clean, release/ v0.1.0 unchanged.
