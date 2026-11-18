"""Write setup.bat / start.bat / stop.bat (UTF-8, CRLF). Run: python scripts/make_bats.py

Each bat uses codex-bridge.exe when it sits next to the bat (released admin zip),
otherwise runs from source with uv (installing uv first if needed)."""

from pathlib import Path

TEMPLATE = r"""@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "%~dp0codex-bridge.exe" (
  "%~dp0codex-bridge.exe" host {cmd} --config host.yaml
  goto done
)
where uv >nul 2>nul
if errorlevel 1 (
  echo uv が見つかりません。公式の配布元 https://astral.sh/uv から入れます。
  powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
  set "PATH=%USERPROFILE%\.local\bin;%PATH%"
)
uv run codex-bridge host {cmd} --config host.yaml
:done
echo.
echo 何かキーを押すと、このウィンドウを閉じます。
pause >nul
"""

root = Path(__file__).resolve().parents[1]
for cmd in ("setup", "start", "stop", "install-mcp"):
    text = TEMPLATE.replace("{cmd}", cmd).replace("\n", "\r\n")
    (root / f"{cmd}.bat").write_bytes(text.encode("utf-8"))
    print("wrote", cmd + ".bat")
