# PyInstaller spec: uv run pyinstaller codex-bridge.spec
# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

datas = [
    ("src/codex_bridge/host/web/static", "codex_bridge/host/web/static"),
    ("src/codex_bridge/agent/static", "codex_bridge/agent/static"),
    ("src/codex_bridge/common/web", "codex_bridge/common/web"),
    ("src/codex_bridge/assets/codex-bridge.ico", "codex_bridge/assets"),
]
for pkg in ("mcp", "mcp_types", "fastapi", "starlette", "pydantic", "uvicorn", "websockets"):
    try:
        datas += copy_metadata(pkg)
    except Exception:
        pass
datas += collect_data_files("mcp")

hidden = collect_submodules("mcp", filter=lambda n: not n.startswith(("mcp.cli", "mcp.os"))) + \
    collect_submodules("mcp_types") + collect_submodules("uvicorn") + [
    "codex_bridge.host.setup_cli", "pystray._win32", "uvicorn.logging", "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto", "uvicorn.lifespan.on",
]

a = Analysis(
    ["scripts/codex_bridge_entry.py"],
    pathex=["src"],
    datas=datas,
    hiddenimports=hidden,
    excludes=["pytest", "PyInstaller"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="codex-bridge",
    icon="src/codex_bridge/assets/codex-bridge.ico",
    console=True,
    hide_console="hide-early",
    upx=False,
)
