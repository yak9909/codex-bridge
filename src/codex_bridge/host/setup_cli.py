"""Interactive steps behind setup.bat / start.bat / stop.bat."""

from __future__ import annotations

import asyncio
import subprocess
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from . import setup_tools as st
from .config import effective_share_root, load_host_config

LINE = "─" * 60


def _say(text: str = "") -> None:
    print(text, flush=True)


def _step(n: int, total: int, title: str) -> None:
    _say()
    _say(LINE)
    _say(f" [{n}/{total}] {title}")
    _say(LINE)


def _yes(question: str, default: bool = True) -> bool:
    mark = "[Y/n]" if default else "[y/N]"
    ans = input(f"{question} {mark}: ").strip().lower()
    return default if not ans else ans in ("y", "yes", "はい")


def _wait_tailscale_ip() -> str:
    while True:
        ip = st.tailscale_ip()
        if ip:
            return ip
        if not st.tailscale_exe():
            _say("Tailscale が見つかりません。開いたページから Windows 版を入れて、ログインしてください。")
            webbrowser.open("https://tailscale.com/download")
        else:
            _say("Tailscale に接続されていません。タスクトレイの Tailscale のアイコンを右クリックし、")
            _say("「Log in...」または「Connect」で接続してください。")
        input("終わったら Enter キーを押してください…")


# ---- setup.bat ---------------------------------------------------------------
def run_setup(config_path: Path, *, then_start: bool = False) -> None:
    root = Path.cwd()
    config_path = Path(config_path).resolve()
    total = 5
    _say("codex-bridge の初期設定を始めます。質問には Enter だけで答えれば、おすすめの設定になります。")

    _step(1, total, "Tailscale の確認")
    ip = _wait_tailscale_ip()
    _say(f"この PC の Tailscale の IP: {ip}")

    _step(2, total, "検索用の部品（ripgrep）の用意")
    if (root / "vendor" / "rg.exe").exists():
        _say("用意済みです。")
    else:
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                        str(root / "scripts" / "fetch_rg.ps1")], check=False)

    _step(3, total, "設定ファイルと権限の規則")
    if not config_path.exists():
        st.write_host_config(config_path, share_root=None, data_dir=root / "data", ip=ip)
    elif st.update_public_bind(config_path, ip):
        _say("Tailscale の IP が変わっていたので、設定を更新しました。招待コードは発行し直してください。")
    _say("共有フォルダ（Codex に触らせるフォルダ）は、起動したあとの管理画面で選びます。")

    from .main import ensure_config
    ensure_config(config_path)
    cfg = load_host_config(config_path)
    policy = cfg.data_dir / "policy.yaml"
    _say(f"権限の規則: {policy}")
    _say("  最初は「全部読むだけ・コマンドは許可リストのみ・削除は確認待ち」です。")
    _say("  書かせてよいフォルダや、見せないフォルダは、管理画面の「権限の規則」でも変えられます。")
    if _yes("今、権限の規則をメモ帳で開きますか？", default=False):
        subprocess.Popen(["notepad.exe", str(policy)])

    _step(4, total, "ファイアウォールの許可（知人の PC からの接続を通す）")
    if st.firewall_rule_exists():
        _say("設定済みです。")
    else:
        _say("Windows が「このアプリがデバイスに変更を加えることを許可しますか？」と聞いてきたら「はい」を押してください。")
        _say("（Tailscale 経由の接続だけを、ポート 8765 に通す設定です）")
        _say("設定できました。" if st.add_firewall_rule_elevated() else
             "設定できませんでした。あとで setup.bat をもう一度実行してください。")

    _step(5, total, "完了")
    _say("初期設定が終わりました。次にやること:")
    n = 0
    if not then_start:
        n += 1
        _say(f"  {n}. start.bat（または exe をダブルクリックして「使う側」）で codex-bridge を起動する（管理画面が開きます）")
    n += 1
    _say(f"  {n}. 管理画面で共有フォルダを選ぶ")
    n += 1
    _say(f"  {n}. install-mcp.bat を実行して、Claude Code / Codex から codex-bridge を使えるようにする")
    n += 1
    _say(f"  {n}. 管理画面の「招待コード」を発行し、同じ zip と一緒に知人に渡す（知人は exe をダブルクリックして「貸す側」）")
    n += 1
    _say(f"  {n}. Tailscale の管理画面でこの PC を知人と共有する（この画面を開きます）")
    _say("     → この PC の行の「…」→「Share...」→ リンクをコピーして知人に送る")
    if then_start:
        _say("次回からは、start.bat か、exe をダブルクリックして「使う側」を選ぶと起動できます。")
    if _yes("Tailscale の管理画面を開きますか？"):
        webbrowser.open("https://login.tailscale.com/admin/machines")
    if then_start:
        _say()
        _say("このまま codex-bridge を起動します。"
             "この黒いウィンドウは閉じないでください（閉じると止まります）。")
        run_start(config_path)


# ---- install-mcp.bat ---------------------------------------------------------
def run_install_mcp(config_path: Path) -> None:
    port = load_host_config(config_path).local_port if Path(config_path).exists() else 8770
    _say("Claude Code と Codex から codex-bridge を使えるように登録します。")
    msgs = {"added": "登録しました。", "already": "登録済みです。", "failed": "登録に失敗しました。",
            "missing": "見つかりませんでした（入れていなければ問題ありません。入れたあとでもう一度実行してください）。"}
    _say(f"  Claude Code: {msgs[st.register_claude(local_port=port)]}")
    codex_result = st.register_codex(local_port=port)
    _say(f"  Codex      : {msgs[codex_result]}")
    if codex_result in ("added", "already") and st.codex_auto_approve():
        _say("               （codex-bridge の道具を毎回確認なしで呼べるように設定しました）")
    _say("")
    _say("使い方: start.bat で codex-bridge を起動してから、Claude Code か Codex で新しい会話を始め、")
    _say("  「codex-bridge で、○○の開発を依頼して」のように頼んでください。")
    _say("  エージェントが共有フォルダを確かめ、知人の Codex に依頼し、結果を確認して報告します。")


# ---- start.bat ---------------------------------------------------------------
def _admin_alive(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/admin/api/overview", timeout=2) as r:
            return r.status == 200
    except OSError:
        return False


def run_start(config_path: Path) -> None:
    config_path = Path(config_path)
    if not config_path.exists():
        _say("まだ初期設定がされていません。先に setup.bat を実行してください。")
        raise SystemExit(1)
    cfg = load_host_config(config_path)
    admin = f"http://127.0.0.1:{cfg.local_port}/admin/"
    if effective_share_root(cfg) is None:
        from .paths import UNSET_SHARE_MSG
        _say(UNSET_SHARE_MSG)
    if not st.firewall_rule_exists():
        _say("ファイアウォールの許可がまだです。知人の PC から接続できないので、setup.bat をもう一度実行してください。")
    if _admin_alive(cfg.local_port):
        _say("codex-bridge はすでに動いています。管理画面を開きます。")
        webbrowser.open(admin)
        return
    ip = _wait_tailscale_ip()
    if st.update_public_bind(config_path, ip):
        _say(f"Tailscale の IP が {ip} に変わっていたので、設定を更新しました。")
        _say("※ 知人には、管理画面で発行し直した新しい招待コードを渡してください。")

    def open_when_ready() -> None:
        for _ in range(60):
            if _admin_alive(cfg.local_port):
                webbrowser.open(admin)
                return
            time.sleep(0.5)

    threading.Thread(target=open_when_ready, daemon=True).start()
    from .main import serve
    try:
        asyncio.run(serve(config_path))
    except KeyboardInterrupt:
        pass
    except OSError as e:
        _say(f"起動できませんでした: {e}")
        _say("ほかのプログラムが同じポートを使っていないか、Tailscale が接続されているかを確かめてください。")
        raise SystemExit(1)


# ---- stop.bat ----------------------------------------------------------------
def run_stop(config_path: Path) -> None:
    port = load_host_config(config_path).local_port if Path(config_path).exists() else 8770
    if st.request_shutdown(port):
        _say("codex-bridge に停止を依頼しました。数秒で止まります。")
    else:
        _say("codex-bridge は動いていないようです。")
