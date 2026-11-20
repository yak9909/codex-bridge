"""Discover, verify, and install Windows releases from the public GitHub repository."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from . import __version__

REPOSITORY = "yak9909/codex-bridge"
LATEST = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
DOWNLOAD_PREFIX = f"https://github.com/{REPOSITORY}/releases/download/"
UPDATE_WAIT = 600.0
SWITCH_WAIT = "アップデートの切り替え待ち"
CHECK_INTERVAL = 6 * 60 * 60
RESTART_ARG = "--after-update"
EXE = "codex-bridge.exe"
INSTALL_STAGE = ".codex-bridge-update-"
PACKAGE_FILES = (
    EXE,
    "setup.bat",
    "start.bat",
    "stop.bat",
    "install-mcp.bat",
    "vendor/rg.exe",
    "docs/operation-guide.md",
    "docs/friend-guide.md",
    "はじめにお読みください.txt",
)


class UpdateError(Exception):
    pass


@dataclass(frozen=True)
class Release:
    version: str
    url: str
    digest: str

    @property
    def folder(self) -> str:
        return f"codex-bridge-v{self.version}"


def version_parts(value: object) -> tuple[int, ...]:
    if not isinstance(value, str) or not re.fullmatch(r"v?[0-9]{1,12}\.[0-9]{1,12}\.[0-9]{1,12}", value):
        return ()
    return tuple(int(part) for part in value.removeprefix("v").split("."))


def parse_release(raw: object, current: str = __version__) -> Release | None:
    if not isinstance(raw, dict) or raw.get("draft") or raw.get("prerelease"):
        return None
    tag = raw.get("tag_name")
    installed = version_parts(current)
    if not installed or not isinstance(tag, str) or not tag.startswith("v") or version_parts(tag) <= installed:
        return None
    version = tag[1:]
    name = f"codex-bridge-{tag}.zip"
    assets = raw.get("assets")
    asset = next((a for a in assets if isinstance(a, dict) and a.get("name") == name), None) if isinstance(assets, list) else None
    if asset is None:
        raise UpdateError("アップデート用のファイルが見つかりませんでした")
    digest = asset.get("digest")
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
        raise UpdateError("SHA-256 を確かめられないため、この版は使いません")
    url = f"{DOWNLOAD_PREFIX}{tag}/{name}"
    if asset.get("browser_download_url") != url:
        raise UpdateError("アップデート用のファイルの入手先を確かめられませんでした")
    return Release(version, url, digest[7:].lower())


class _GitHubRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith(("https://github.com/", "https://api.github.com/",
                                  "https://release-assets.githubusercontent.com/",
                                  "https://objects.githubusercontent.com/")):
            raise UpdateError("アップデート用のファイルの入手先を確かめられませんでした")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_github(url: str):
    from .agent.setup import tls_context

    request = Request(url, headers={"User-Agent": f"codex-bridge/{__version__}"})
    return build_opener(_GitHubRedirects(), HTTPSHandler(context=tls_context())).open(request, timeout=30)


def check_release() -> Release | None:
    with open_github(LATEST) as response:
        return parse_release(json.load(response))


def _extract(archive: Path, stage: Path, release: Release) -> Path:
    allowed = {f"{release.folder}/{name}" for name in PACKAGE_FILES}
    written: set[str] = set()
    with zipfile.ZipFile(archive) as package:
        entries = package.infolist()
        for entry in entries:
            name = entry.orig_filename
            parts = name.rstrip("/").split("/")
            if (any(part in ("", ".", "..") or part.rstrip(" .") != part for part in parts)
                    or any(char in name for char in ("\\", ":", "\0"))
                    or stat.S_ISLNK(entry.external_attr >> 16)):
                raise UpdateError("アップデート用の ZIP に使えないパスがあります")
            if name in allowed:
                if name in written:
                    raise UpdateError("アップデート用の ZIP に同名のファイルがあります")
                written.add(name)
        if f"{release.folder}/{EXE}" not in written:
            raise UpdateError("アップデート用の ZIP の内容を確かめられませんでした")
        for name in written:
            destination = stage / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            with package.open(name) as source, destination.open("xb") as target:
                shutil.copyfileobj(source, target)
    return stage / release.folder


def prepare_release(release: Release, data_dir: Path) -> Path:
    root = data_dir / "updates"
    if root.is_symlink() or root.is_junction():
        raise UpdateError("アップデート用の保存先を使えません")
    root.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="download-", dir=root))
    archive = stage / "release.zip"
    try:
        digest = hashlib.sha256()
        with open_github(release.url) as response, archive.open("xb") as target:
            while chunk := response.read(1024 * 1024):
                target.write(chunk)
                digest.update(chunk)
        if digest.hexdigest() != release.digest:
            raise UpdateError("SHA-256 を確かめられませんでした")
        package = _extract(archive, stage, release)
        archive.unlink()
        return package
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def _settings(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {"mode": value}


def _save_setting(path: Path, key: str, value: str | None) -> None:
    settings = _settings(path)
    settings[key] = value
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings), encoding="utf-8")
    tmp.replace(path)


def mode(path: Path) -> str:
    return "auto" if _settings(path).get("mode") == "auto" else "confirm"


def save_mode(path: Path, value: str) -> None:
    _save_setting(path, "mode", value)


def cancelled_version(path: Path) -> str | None:
    value = _settings(path).get("cancelled_version")
    return value if isinstance(value, str) else None


def save_cancelled_version(path: Path, value: str | None) -> None:
    _save_setting(path, "cancelled_version", value)


def _check_exe_rename(exe: Path) -> None:
    if os.name != "nt":
        return
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    # Request the access needed for a rename without changing the live executable.
    handle = kernel.CreateFileW(str(exe), 0x10000, 7, None, 3, 0, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    kernel.CloseHandle(handle)


def check_install_folder(exe: Path) -> None:
    if exe.name.lower() != EXE:
        raise UpdateError(f"{EXE} の名前で起動してから更新してください")
    try:
        with tempfile.TemporaryDirectory(prefix=".codex-bridge-probe-", dir=exe.parent) as directory:
            probe = Path(directory) / "write"
            probe.write_bytes(b"")
            probe.replace(probe.with_name("rename"))
            _check_exe_rename(exe)
    except OSError as e:
        raise UpdateError(f"exe のフォルダに書き込めないため、アップデートできません: {exe.parent}") from e


def _alternate_old_path(old: Path) -> Path:
    return old.with_name(f"{old.name}-{time.time_ns()}")


def _free_old_exe(old: Path, *, preserve: bool = False) -> None:
    if not preserve:
        try:
            old.unlink(missing_ok=True)
            return
        except OSError:
            pass
    if old.exists() or old.is_symlink():
        old.rename(_alternate_old_path(old))


def acknowledge_restart(exe: Path, stage_name: str) -> bool:
    if not re.fullmatch(re.escape(INSTALL_STAGE) + r"[a-z0-9_]{8}", stage_name):
        return False
    stage = exe.parent / stage_name
    if stage.is_symlink() or stage.is_junction():
        return False
    try:
        (stage / "ready").touch(exist_ok=False)
    except OSError:
        return False
    return True


def _launch(exe: Path, stage: Path) -> None:
    child = subprocess.Popen([str(exe), RESTART_ARG, stage.name, str(os.getpid())], cwd=exe.parent,
                             env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"},
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    deadline = time.monotonic() + 45
    while child.poll() is None:
        if (stage / "ready").is_file():
            return
        if time.monotonic() >= deadline:
            child.terminate()
            child.wait(timeout=10)
            raise UpdateError("新しい exe の起動を確認できませんでした")
        time.sleep(0.1)
    raise UpdateError("新しい exe が起動直後に終了しました")


def install_release(exe: Path, package: Path, launch=None, *, before_launch=None) -> None:
    """Stage replacements on the install volume and undo any moves on failure."""
    if exe.name.lower() != EXE:
        raise UpdateError(f"{EXE} の名前で起動してから更新してください")
    base = exe.parent
    old_exe = exe.with_suffix(".exe.old")
    preserve_old = any(base.glob(INSTALL_STAGE + "*"))
    staging = Path(tempfile.mkdtemp(prefix=INSTALL_STAGE, dir=base))
    keep_backups = False
    try:
        try:
            _free_old_exe(old_exe, preserve=preserve_old)
        except OSError:
            # A file locked against both operations does not need to be reused as this rollback target.
            old_exe = _alternate_old_path(old_exe)
        files = []
        for name in PACKAGE_FILES:
            source = package / name
            if not source.is_file():
                continue
            dest = exe if name == EXE else base / name
            if (dest.is_symlink() or dest.parent.is_symlink() or dest.parent.is_junction()
                    or (dest.exists() and not dest.is_file())):
                raise UpdateError(f"入れ替え先を使えません: {name}")
            prepared = staging / "new" / name
            prepared.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, prepared)
            backup = old_exe if name == EXE else staging / "old" / name
            backup.parent.mkdir(parents=True, exist_ok=True)
            files.append((dest, prepared, backup))
        if not files or files[0][0] != exe:
            raise UpdateError("新しい exe が見つかりませんでした")
        changes: dict[Path, Path | None] = {}
        created_dirs = []
        try:
            for dest, prepared, backup in files:
                if not dest.parent.exists():
                    dest.parent.mkdir()
                    created_dirs.append(dest.parent)
                if dest.exists():
                    dest.replace(backup)
                    changes[dest] = backup
                prepared.replace(dest)
                changes.setdefault(dest, None)
            if before_launch is not None:
                before_launch()
            if launch is None:
                _launch(exe, staging)
            else:
                launch(exe)
        except Exception as install_error:
            restoration_errors = []
            for dest, backup in changes.items():
                try:
                    if backup is None:
                        dest.unlink()
                    else:
                        backup.replace(dest)
                except OSError as e:
                    restoration_errors.append(str(e))
            for directory in reversed(created_dirs):
                try:
                    directory.rmdir()
                except OSError as e:
                    restoration_errors.append(str(e))
            if restoration_errors:
                keep_backups = True
                raise UpdateError(f"入れ替えに失敗し、一部を元に戻せませんでした。保存先 {staging}。"
                                  f"{install_error}。{'、'.join(restoration_errors)}") from install_error
            raise
    finally:
        if not keep_backups:
            shutil.rmtree(staging, ignore_errors=True)


def clean_up(exe: Path, data_dir: Path) -> None:
    try:
        _free_old_exe(exe.with_suffix(".exe.old"), preserve=any(exe.parent.glob(INSTALL_STAGE + "*")))
    except OSError:
        pass  # The onefile bootloader may still be exiting.
    root = data_dir / "updates"
    if root.is_symlink() or root.is_junction():
        return
    try:
        for stage in root.glob("download-*"):
            if re.fullmatch(r"download-[a-z0-9_]{8}", stage.name):
                shutil.rmtree(stage, ignore_errors=True)
        root.rmdir()
    except OSError:
        pass
