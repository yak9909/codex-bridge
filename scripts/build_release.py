"""Build one exe and assemble the one release zip into release/.

Run from the repo root:  uv run python scripts/build_release.py
  release/codex-bridge-v0.2.0.zip   for both the owner and the friend
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

知人の PC の Codex に、依頼主の PC のフォルダで作業してもらうための道具です。
この zip は、依頼主と知人の両方に同じものを渡します。

■ 知人（貸す側）の方へ
  1. codex-bridge.exe をダブルクリックする
  2. 「貸す側」を選ぶ
  3. 依頼主からもらった招待コードを貼る
  ほかのファイルは使いません。
  詳しくは docs\\friend-guide.md を見てください。

■ 依頼主（使う側）の方へ
  最初に一度だけ
    1. この PC に Tailscale を入れてログインする（https://tailscale.com/download）
    2. codex-bridge.exe をダブルクリックして「使う側」を選ぶ
       （初回は初期設定のあと、そのまま起動して管理画面が開きます。
        setup.bat をダブルクリックすると、初期設定だけできます）
    3. 管理画面で、Codex に触らせる共有フォルダを選ぶ
    4. install-mcp.bat をダブルクリック（Claude Code / Codex から使えるようにする）
    5. 管理画面の「招待コード」を発行し、この zip と一緒に知人に渡す
    6. Tailscale の管理画面で、この PC を知人と共有する

  以前の版から更新するとき
    新しい zip は別のフォルダに展開されるため、そのまま始めると新しく初期設定が始まります
    （新しい招待コードが必要になり、権限の規則と共有フォルダも引き継がれません）。
    先に古いフォルダの stop.bat で止める（または古い黒いウィンドウを閉じる）。
    そのあと古いフォルダの host.yaml と data フォルダを新しいフォルダにコピーしてから、
    「使う側」を選ぶか start.bat を実行します。

  毎回
    codex-bridge.exe で「使う側」を選ぶか、start.bat をダブルクリックして起動します。
    黒いウィンドウは閉じないでください。
    停止は stop.bat か、黒いウィンドウを閉じます。

  頼み方
    start.bat などで起動してから、Claude Code か Codex で「codex-bridge で、○○の開発を依頼して」と頼むだけです。
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
    # Earlier builds made separate admin/friend zips; drop only this version's.
    for role in ("admin", "friend"):
        (out / f"codex-bridge-{role}-v{VERSION}.zip").unlink(missing_ok=True)
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
