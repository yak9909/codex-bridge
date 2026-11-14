"""Interactive steps behind setup.bat / start.bat / stop.bat."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from . import setup_tools as st
from .config import load_host_config

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


def _choose_folder(title: str) -> Path | None:
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        chosen = filedialog.askdirectory(title=title, mustexist=True)
    finally:
        root.destroy()
    return Path(chosen) if chosen else None


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
def run_setup(config_path: Path) -> None:
    root = Path.cwd()
    config_path = Path(config_path).resolve()
    total = 7
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

    _step(3, total, "共有フォルダ（Codex に触らせるフォルダ）")
    choose = True
    if config_path.exists():
        cfg = load_host_config(config_path)
        _say(f"今の共有フォルダ: {cfg.share_root}")
        choose = _yes("共有フォルダを選び直しますか？", default=False)
    if choose:
        share = None
        while share is None:
            _say("フォルダを選ぶ画面を開きます（ほかのウィンドウの後ろに隠れていたら、タスクバーを確認してください）。")
            share = _choose_folder("Codex に触らせるフォルダを選んでください")
            if share is None and not _yes("フォルダが選ばれませんでした。もう一度選びますか？"):
                _say("初期設定を中止しました。")
                return
        data_dir = root / "data"
        if st.inside(data_dir, share):
            data_dir = Path(os.environ["LOCALAPPDATA"]) / "codex-bridge-host" / "data"
        st.write_host_config(config_path, share_root=share, data_dir=data_dir, ip=ip)
        _say(f"共有フォルダを {share} にしました。")
    elif st.update_public_bind(config_path, ip):
        _say("Tailscale の IP が変わっていたので、設定を更新しました。招待コードは発行し直してください。")

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

    _step(5, total, "Claude Code への登録")
    result = st.register_claude(local_port=cfg.local_port)
    _say({"added": "登録しました。新しく始めた Claude Code のセッションから使えます。",
          "already": "登録済みです。",
          "missing": "Claude Code（claude コマンド）が見つかりませんでした。入れたあとで setup.bat をもう一度実行してください。",
          "failed": "登録に失敗しました。あとで setup.bat をもう一度実行してください。"}[result])

    _step(6, total, "知人に渡す一式（friend-kit フォルダ）を作る")
    if _yes("今、作りますか？（1〜2 分かかります）"):
        build_friend_kit(root)

    _step(7, total, "完了")
    _say("初期設定が終わりました。次にやること:")
    _say("  1. start.bat をダブルクリックして codex-bridge を起動する（管理画面が開きます）")
    _say("  2. 管理画面の「招待コード」で発行し、friend-kit フォルダの中身と一緒に知人に渡す")
    _say("  3. Tailscale の管理画面でこの PC を知人と共有する（この画面を開きます）")
    _say("     → この PC の行の「…」→「Share...」→ リンクをコピーして知人に送る")
    if _yes("Tailscale の管理画面を開きますか？"):
        webbrowser.open("https://login.tailscale.com/admin/machines")


def build_friend_kit(root: Path) -> None:
    spec = root / "codex-bridge-agent.spec"
    r = subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", str(spec)], cwd=root,
                       capture_output=True, text=True)
    exe = root / "dist" / "codex-bridge-agent.exe"
    if r.returncode != 0 or not exe.exists():
        _say("作れませんでした。詳しくは次の内容を確認してください:")
        _say((r.stderr or r.stdout)[-2000:])
        return
    kit = root / "friend-kit"
    kit.mkdir(exist_ok=True)
    shutil.copy2(exe, kit / exe.name)
    shutil.copy2(root / "docs" / "friend-guide.md", kit / "使い方.md")
    _say(f"できました: {kit}")
    os.startfile(kit)


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
