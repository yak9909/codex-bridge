"""Agent first-run helpers: invite storage (DPAPI), finding or fetching codex, login."""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from ..common.invite import Invite

RELEASE_API = "https://api.github.com/repos/openai/codex/releases/latest"
PACKAGE_ASSET = "codex-package-x86_64-pc-windows-msvc.tar.gz"
UA = {"User-Agent": "codex-bridge-agent"}


def data_root() -> Path:
    base = os.environ.get("CODEX_BRIDGE_AGENT_HOME") or Path(os.environ["LOCALAPPDATA"]) / "codex-bridge-agent"
    p = Path(base)
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---- DPAPI (encrypt for the current Windows user) -------------------------
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> _Blob:
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _dpapi(data: bytes, protect: bool) -> bytes:
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    src, out = _blob(data), _Blob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(src), None, None, None, None, 0x1, ctypes.byref(out)):  # CRYPTPROTECT_UI_FORBIDDEN
        raise OSError("DPAPI failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def save_invite(directory: Path, invite: Invite) -> None:
    data = json.dumps({"url": invite.url, "token": invite.token, "name": invite.name}).encode("utf-8")
    (Path(directory) / "invite.bin").write_bytes(_dpapi(data, protect=True))


def load_invite(directory: Path) -> Invite | None:
    p = Path(directory) / "invite.bin"
    if not p.exists():
        return None
    try:
        d = json.loads(_dpapi(p.read_bytes(), protect=False))
        return Invite(d["url"], d["token"], d.get("name", ""))
    except (OSError, ValueError, KeyError):
        return None


# ---- codex -------------------------------------------------------------
def find_codex(bin_dir: Path) -> Path | None:
    found = shutil.which("codex")
    if found:
        return Path(found)
    bundled = Path(bin_dir) / "codex.exe"
    return bundled if bundled.exists() else None


def pick_asset(assets: list[dict]) -> dict:
    for a in assets:
        if a.get("name") == PACKAGE_ASSET:
            return a
    raise RuntimeError("Windows 版の Codex が見つかりませんでした")


def extract_package(archive: Path, bin_dir: Path) -> Path:
    """Extract the release package flat into bin_dir and return codex.exe."""
    bin_dir = Path(bin_dir)
    bin_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tf:
        for m in tf.getmembers():
            name = Path(m.name).name
            if not m.isfile() or ".." in Path(m.name).parts or not name:
                continue
            src = tf.extractfile(m)
            if src is None:
                continue
            with open(bin_dir / name, "wb") as dst:
                shutil.copyfileobj(src, dst)
    exe = bin_dir / "codex.exe"
    if not exe.exists():
        candidates = sorted(bin_dir.glob("codex-x86_64*.exe"))
        if not candidates:
            raise RuntimeError("パッケージに codex.exe がありません")
        candidates[0].rename(exe)
    return exe


def download_codex(bin_dir: Path, progress=lambda done, total: None) -> Path:
    with urllib.request.urlopen(urllib.request.Request(RELEASE_API, headers=UA), timeout=30) as r:
        asset = pick_asset(json.load(r)["assets"])
    with tempfile.TemporaryDirectory() as d:
        archive = Path(d) / asset["name"]
        req = urllib.request.Request(asset["browser_download_url"], headers=UA)
        with urllib.request.urlopen(req, timeout=60) as r, open(archive, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while chunk := r.read(1 << 20):
                f.write(chunk)
                done += len(chunk)
                progress(done, total)
        return extract_package(archive, bin_dir)


def run_login(codex_cmd: list[str]) -> int:
    """Open `codex login` in its own console window and wait for it."""
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    return subprocess.run([*codex_cmd, "login"], creationflags=flags).returncode


def make_guard(auth_mode: str, thresholds: dict | None = None):
    """Quota guard; usage windows only exist for ChatGPT sign-in, so it is off otherwise."""
    from .usage import QuotaGuard
    guard = QuotaGuard(thresholds or {"five_hour": 90, "weekly": 95})
    guard.enabled = auth_mode == "chatgpt"
    return guard
