"""Build both exes and assemble the two release zips into release/.

Run from the repo root:  uv run python scripts/build_release.py
  release/codex-bridge-admin-v<ver>.zip   for the owner (unzip, run setup.bat)
  release/codex-bridge-friend-v<ver>.zip  for the friend (unzip, run the exe)
"""

import shutil
import subprocess
import sys
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
     ・Codex に触らせるフォルダを選ぶ
     ・Windows の「変更を許可しますか？」に「はい」
     ・最後に friend-kit フォルダ（知人に渡すもの）ができる
  3. Tailscale の管理画面で、この PC を知人と共有する（setup.bat の最後に案内が出ます）

■ 毎回
  start.bat … 起動（管理画面がブラウザで開きます）。黒いウィンドウは閉じないでください。
  stop.bat  … 停止（黒いウィンドウを閉じても止まります）

■ 知人に渡すもの
  ・friend-kit フォルダの中身（codex-bridge-agent.exe と 使い方.md）
  ・Tailscale の共有の招待リンク
  ・招待コード（管理画面の「招待コード」→「発行」）

詳しくは docs\\operation-guide.md を見てください（Claude Code への頼み方も書いてあります）。
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
    run(sys.executable, "-m", "PyInstaller", "--noconfirm", "codex-bridge-agent.spec")
    run(sys.executable, "-m", "PyInstaller", "--noconfirm", "codex-bridge-host.spec")

    out = ROOT / "release"
    shutil.rmtree(out, ignore_errors=True)
    admin = out / f"codex-bridge-admin-v{VERSION}"
    friend = out / f"codex-bridge-friend-v{VERSION}"
    (admin / "vendor").mkdir(parents=True)
    (admin / "docs").mkdir()
    friend.mkdir(parents=True)

    for name in ("codex-bridge-host.exe", "codex-bridge-agent.exe"):
        shutil.copy2(ROOT / "dist" / name, admin / name)
    for name in ("setup.bat", "start.bat", "stop.bat", "install-mcp.bat"):
        shutil.copy2(ROOT / name, admin / name)
    shutil.copy2(ROOT / "vendor" / "rg.exe", admin / "vendor" / "rg.exe")
    for name in ("operation-guide.md", "friend-guide.md"):
        shutil.copy2(ROOT / "docs" / name, admin / "docs" / name)
    (admin / "はじめにお読みください.txt").write_text(ADMIN_README.format(ver=VERSION), encoding="utf-8-sig")

    shutil.copy2(ROOT / "dist" / "codex-bridge-agent.exe", friend / "codex-bridge-agent.exe")
    shutil.copy2(ROOT / "docs" / "friend-guide.md", friend / "使い方.md")

    for d in (admin, friend):
        zip_dir(d, out / f"{d.name}.zip")
        print("built", out / f"{d.name}.zip", f"{(out / f'{d.name}.zip').stat().st_size >> 20} MB")


if __name__ == "__main__":
    main()
