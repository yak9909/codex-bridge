# PyInstaller spec: uv run pyinstaller codex-bridge-agent.spec
# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ["scripts/agent_entry.py"],
    pathex=["src"],
    datas=[("src/codex_bridge/common/web", "codex_bridge/common/web")],  # UI parts shared with the host's page
    hiddenimports=["pystray._win32", "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
                   "uvicorn.lifespan.on"],
    excludes=["mcp", "pytest"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="codex-bridge-agent",
    console=False,
    upx=False,
)
