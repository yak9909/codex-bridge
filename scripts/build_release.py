"""Build one exe and assemble the one release zip into release/.

Run from the repo root:  uv run python scripts/build_release.py
  release/codex-bridge-v0.3.0.zip   for both the owner and the friend
The release version comes from pyproject.toml.
Only the same-version zip (and same-version admin/friend zips of earlier
builds) are replaced; other releases are kept.
"""

import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]

README = """codex-bridge v{ver}

貸す人の PC の Codex に、依頼主の PC のフォルダで作業してもらうための道具です。
この zip は、依頼主と貸す人の両方に同じものを渡します。

■ 起動と画面
  codex-bridge.exe をダブルクリックすると、タスクトレイに常駐します。
  CB アイコンを左クリックするか、右クリック →「画面を開く」で、
  http://127.0.0.1:8766/ を開きます。「使う」「貸す」「ログ」のタブがあります。
  すでに動いているときにもう一度 exe を開くと、そのアプリの画面が開きます。
  停止するときは、トレイの「終了」を押します。

■ 貸す人の方へ
  1. codex-bridge.exe をダブルクリックして画面を開く
  2. 「貸す」タブに、依頼主からもらった招待コードを貼る
  3. 「つなぐ」を押す
  使えるモデル・推論の強さ・始める前の許可などは、依頼主ごとのカードで決めます。
  使用量の上限と超えたときの止め方は、貸す人が決めます。
  詳しくは docs\\friend-guide.md を見てください。

■ 依頼主の方へ
  最初に一度だけ
    1. この PC に Tailscale を入れてログインする（https://tailscale.com/download）
    2. codex-bridge.exe をダブルクリックして画面を開く
    3. 「使う」タブの「準備を始める」を押す
       Tailscale とファイアウォールを設定するコンソールが開きます。
       終わると「使う」が自動で動き始めます。
    4. 「使う」→「共有フォルダ」で、Codex に触らせるフォルダを追加する
    5. install-mcp.bat をダブルクリックする
       Claude Code / Codex の接続先は、引き続き http://127.0.0.1:8770 です。
       承認待ちと終了の知らせも入ります。Codex では /hooks でフックを信頼してください。
    6. 「使う」→「招待」→ 名前を入力 →「招待コードを発行」でコードを作る
       貸す人ごとに別の名前で発行し、この zip と一緒に渡します。
    7. Tailscale の画面で、この PC を貸す人と共有する

  設定
    貸す人のカードを開き、コマンド実行・削除の承認・相手に見せる記録を決めます。
    新しい招待のコマンド実行は「毎回承認」です。
    設定は変更すると反映され、複数行の入力は欄からフォーカスを外すと反映されます。
    使用量の上限は貸す人が決めます。依頼主自身の上限設定はありません。

■ アップデート（使う方・貸す方 共通）
  v0.3.1 からは「ログ」タブで「確かめる」「更新する」を使えます。
  既定の「毎回確認する」は同じ欄で「自動で更新する」に変えられます。
  更新は、使う側と貸す側の作業がすべて終わったあと、再起動して適用します。

  v0.3.0 から v0.3.1 への移行は手動です。
    古いアプリをトレイの「終了」で止めます。
    コンソールで起動している場合は、古いフォルダの stop.bat で止めます。
    新しい zip を展開し、古いフォルダの host.yaml と data フォルダをコピーしてから、
    新しい exe をダブルクリックしてください。
    新しいフォルダで install-mcp.bat も実行し直し、Codex の /hooks でフックを信頼します。

  コンソールで使う場合
    setup.bat は初期設定、start.bat は起動、stop.bat は停止です。
    codex-bridge.exe host setup|start|stop|install-mcp も使えます。
    start.bat で開く黒いウィンドウは、作業中は閉じないでください。
    この場合は、同じ画面の「使う」を http://127.0.0.1:8770/admin/ で開けます。

  頼み方
    「使う」が動いたら、Claude Code か Codex で「codex-bridge で、○○の開発を依頼して」と頼みます。
    「使う」→「依頼」→「新しい依頼」から直接頼むこともできます。
    承認待ちか終了になると、依頼したエージェントに自動で知らせが届きます。

  詳しくは docs\\operation-guide.md を見てください。
"""


def run(*args: str) -> None:
    print(">", " ".join(args), flush=True)
    subprocess.run(args, cwd=ROOT, check=True)


def zip_dir(src: Path, dest: Path) -> None:
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(src.rglob("*")):
            if p.is_file():
                z.write(p, Path(src.name) / p.relative_to(src))


def main() -> None:
    if not (ROOT / "vendor" / "rg.exe").exists():
        run("powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "scripts/fetch_rg.ps1")
    run(sys.executable, "scripts/make_bats.py")
    run(sys.executable, "-m", "PyInstaller", "--noconfirm", "codex-bridge.spec")

    out = ROOT / "release"
    out.mkdir(exist_ok=True)
    # Earlier builds made separate admin/friend zips; drop only this version's.
    stale = [out / f"codex-bridge-{role}-v{VERSION}.zip" for role in ("admin", "friend")]
    stale.append(out / f"codex-bridge-v{VERSION}.zip")
    for old in stale:
        try:
            old.unlink(missing_ok=True)
        except PermissionError:
            raise SystemExit(f"release\\{old.name} が開かれています。7-Zip などを閉じてからもう一度実行してください。")
    # Stage outside the repository so old release folders and files stay intact.
    with tempfile.TemporaryDirectory(prefix="codex-bridge-release-") as staging:
        pkg = Path(staging) / f"codex-bridge-v{VERSION}"
        (pkg / "vendor").mkdir(parents=True)
        (pkg / "docs").mkdir()

        shutil.copy2(ROOT / "dist" / "codex-bridge.exe", pkg / "codex-bridge.exe")
        for name in ("setup.bat", "start.bat", "stop.bat", "install-mcp.bat"):
            shutil.copy2(ROOT / name, pkg / name)
        shutil.copy2(ROOT / "vendor" / "rg.exe", pkg / "vendor" / "rg.exe")
        for name in ("operation-guide.md", "friend-guide.md"):
            shutil.copy2(ROOT / "docs" / name, pkg / "docs" / name)
        (pkg / "はじめにお読みください.txt").write_text(README.format(ver=VERSION),
                                                       encoding="utf-8-sig", newline="\n")

        archive = out / f"{pkg.name}.zip"
        zip_dir(pkg, archive)
        print("built", archive, f"{archive.stat().st_size >> 20} MB")


if __name__ == "__main__":
    main()
