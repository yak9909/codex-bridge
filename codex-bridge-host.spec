# PyInstaller spec: uv run pyinstaller codex-bridge-host.spec
# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

datas = [("src/codex_bridge/host/web/static", "codex_bridge/host/web/static")]
for pkg in ("mcp", "mcp_types", "fastapi", "starlette", "pydantic", "uvicorn", "websockets"):
    try:
        datas += copy_metadata(pkg)
    except Exception:
        pass
datas += collect_data_files("mcp")

hidden = collect_submodules("mcp", filter=lambda n: not n.startswith(("mcp.cli", "mcp.os"))) + \
    collect_submodules("mcp_types") + collect_submodules("uvicorn") + [
    "codex_bridge.host.setup_cli",
]

a = Analysis(
    ["scripts/host_entry.py"],
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
    name="codex-bridge-host",
    console=True,
    upx=False,
)
