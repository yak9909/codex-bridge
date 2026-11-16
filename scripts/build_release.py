"""Build one exe and assemble the two release zips into release/.

Run from the repo root:  uv run python scripts/build_release.py
  release/codex-bridge-admin-v0.2.0.zip   for the owner (unzip, run setup.bat)
  release/codex-bridge-friend-v0.2.0.zip  for the friend (unzip, run the exe)
The release version comes from pyproject.toml.
Only same-version zip files are replaced; other releases are kept.
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

ADMIN_README = """codex-bridge（依頼主＝管理者用） v{ver}

知人の PC の Codex に、この PC のフォルダで作業してもらうための道具です。

■ 最初に一度だけ
  1. この PC に Tailscale を入れてログインする（https://tailscale.com/download）
  2. setup.bat をダブルクリックして、画面の質問に答える
     （codex-bridge.exe をダブルクリックして「使う側」を選んでも始められます）
     ・Codex に触らせるフォルダを選ぶ
     ・Windows の「変更を許可しますか？」に「はい」
     ・最後に friend-kit フォルダ（知人に渡すもの）ができる
  3. Tailscale の管理画面で、この PC を知人と共有する（setup.bat の最後に案内が出ます）
  4. install-mcp.bat をダブルクリック（Claude Code / Codex から使えるようにする）

■ 毎回
  start.bat … 起動（管理画面がブラウザで開きます）。黒いウィンドウは閉じないでください。
  codex-bridge.exe をダブルクリックして「使う側」を選んでも起動できます。
  stop.bat  … 停止（黒いウィンドウを閉じても止まります）

■ 知人に渡すもの
  ・friend-kit フォルダの中身（codex-bridge.exe と 使い方.md）
    知人は codex-bridge.exe をダブルクリックして「貸す側」を選びます。
  ・Tailscale の共有の招待リンク
  ・招待コード（管理画面の「招待コード」→「発行」）

■ 頼み方
  start.bat で起動してから、Claude Code か Codex で「codex-bridge で、○○の開発を依頼して」と頼むだけです。
  管理画面の「Codex に依頼する」欄から直接頼むこともできます。

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
    # Stage outside the repository so old release folders and files stay intact.
    with tempfile.TemporaryDirectory(prefix="codex-bridge-release-") as staging:
        admin = Path(staging) / f"codex-bridge-admin-v{VERSION}"
        friend = Path(staging) / f"codex-bridge-friend-v{VERSION}"
        (admin / "vendor").mkdir(parents=True)
        (admin / "docs").mkdir()
        friend.mkdir()

        shutil.copy2(ROOT / "dist" / "codex-bridge.exe", admin / "codex-bridge.exe")
        for name in ("setup.bat", "start.bat", "stop.bat", "install-mcp.bat"):
            shutil.copy2(ROOT / name, admin / name)
        shutil.copy2(ROOT / "vendor" / "rg.exe", admin / "vendor" / "rg.exe")
        for name in ("operation-guide.md", "friend-guide.md"):
            shutil.copy2(ROOT / "docs" / name, admin / "docs" / name)
        (admin / "はじめにお読みください.txt").write_text(ADMIN_README.format(ver=VERSION),
                                                           encoding="utf-8-sig", newline="\n")

        shutil.copy2(ROOT / "dist" / "codex-bridge.exe", friend / "codex-bridge.exe")
        shutil.copy2(ROOT / "docs" / "friend-guide.md", friend / "使い方.md")

        for directory in (admin, friend):
            archive = out / f"{directory.name}.zip"
            zip_dir(directory, archive)
            print("built", archive, f"{archive.stat().st_size >> 20} MB")


if __name__ == "__main__":
    main()
