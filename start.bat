@echo off
chcp 65001 >nul
cd /d "%~dp0"
where uv >nul 2>nul
if errorlevel 1 (
  echo uv が見つかりません。公式の配布元 https://astral.sh/uv から入れます。
  powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
  set "PATH=%USERPROFILE%\.local\bin;%PATH%"
)
uv run codex-bridge-host start --config host.yaml
echo.
echo 何かキーを押すと、このウィンドウを閉じます。
pause >nul
