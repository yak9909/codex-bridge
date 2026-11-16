"""Encrypted invites and lender-owned settings, one directory per connection."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from ..common.invite import Invite, parse_invite
from ..common.protocol import LOG_VISIBILITY
from .settings import CONNECTION_KEYS, SettingsStore, _merge, defaults
from .setup import load_invite, save_invite

SAFE_ID = re.compile(r"[a-f0-9]{12}")


def connection_settings(raw=None, *, strict=False) -> dict:
    if strict and (not isinstance(raw, dict) or set(raw) - CONNECTION_KEYS):
        raise ValueError("接続先の設定は features、pre_approval、disposable、log_visibility だけです")
    raw = raw if isinstance(raw, dict) else {}
    settings = _merge(defaults(), {k: v for k, v in raw.items() if k != "log_visibility"}, strict)
    visibility = raw.get("log_visibility", "summary")
    if visibility not in LOG_VISIBILITY:
        if strict:
            raise ValueError("log_visibility は summary、files、all のどれかです")
        visibility = "summary"
    return {**{k: settings[k] for k in ("features", "pre_approval", "disposable")}, "log_visibility": visibility}


def _write_settings(path: Path, settings: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
    os.replace(tmp, path)


@dataclass
class Connection:
    id: str
    invite: Invite
    settings: dict


class ConnectionStore:
    def __init__(self, home: Path) -> None:
        self.home = Path(home)
        self.root = self.home / "connections"
        self._lock = threading.RLock()
        self._migrate()

    def _migrate(self) -> None:
        old = self.home / "invite.bin"
        if self.root.exists() or not old.exists():
            return
        invite = load_invite(self.home)
        if invite is None:
            raise ValueError("保存された招待コードを読めませんでした。元のファイルは残しています")
        stage = self.home / ("connections-migrate-" + secrets.token_hex(6))
        try:
            first = stage / secrets.token_hex(6)
            first.mkdir(parents=True)
            save_invite(first, invite)
            legacy = SettingsStore(self.home / "settings.json").get()
            try:
                legacy["log_visibility"] = (self.home / "logs" / "visibility.txt").read_text(encoding="utf-8").strip()
            except (OSError, ValueError):
                pass
            _write_settings(first / "settings.json", connection_settings(legacy))
            for name in ("sessions.json", "logs"):
                source = self.home / name
                if source.is_dir():
                    shutil.copytree(source, first / name)
                elif source.is_file():
                    shutil.copy2(source, first / name)
            for path in (first / "logs" / "tasks").glob("*.jsonl"):
                lines = []
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                    try:
                        record = json.loads(line)
                    except ValueError:
                        record = None
                    if isinstance(record, dict):
                        record.update(connection_id=first.name, host_name=urlsplit(invite.url).netloc)
                        line = json.dumps(record, ensure_ascii=False)
                    lines.append(line)
                path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
            # All copies must succeed before the old invite is removed. On failure the old data still works.
            os.replace(stage, self.root)
            try:
                old.unlink()
            except BaseException:
                shutil.rmtree(self.root)
                raise
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise

    def _path(self, id: str) -> Path:
        if not isinstance(id, str) or not SAFE_ID.fullmatch(id):
            raise KeyError(id)
        return self.root / id

    def _read(self, id: str) -> Connection:
        path = self._path(id)
        invite = load_invite(path)
        if invite is None:
            raise KeyError(id)
        try:
            raw = json.loads((path / "settings.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        return Connection(id, invite, connection_settings(raw))

    def list(self) -> list[Connection]:
        with self._lock:
            rows = []
            for path in sorted(self.root.iterdir()) if self.root.exists() else ():
                if path.is_dir() and SAFE_ID.fullmatch(path.name):
                    rows.append(self._read(path.name))
            return rows

    def _unique(self, invite: Invite, except_id=None) -> None:
        if any(c.id != except_id and (c.invite.url, c.invite.name) == (invite.url, invite.name) for c in self.list()):
            raise ValueError("この接続先はすでに登録されています")

    def add(self, code: str) -> Connection:
        invite = parse_invite(code)
        with self._lock:
            self._unique(invite)
            self.root.mkdir(parents=True, exist_ok=True)
            id = secrets.token_hex(6)
            path = self._path(id)
            path.mkdir()
            try:
                save_invite(path, invite)
                _write_settings(path / "settings.json", connection_settings())
            except BaseException:
                shutil.rmtree(path, ignore_errors=True)
                raise
            return self._read(id)

    def replace_invite(self, id, code) -> Connection:
        invite = parse_invite(code)
        with self._lock:
            old = self._read(id)
            self._unique(invite, id)
            path = self._path(id)
            sessions, archive = path / "sessions.json", None
            if (old.invite.url, old.invite.name) != (invite.url, invite.name) and sessions.exists():
                archive = path / ("sessions-before-" + secrets.token_hex(6) + ".json")
                os.replace(sessions, archive)
            try:
                save_invite(path, invite)
            except BaseException:
                if archive is not None:
                    os.replace(archive, sessions)
                raise
            return self._read(id)

    def remove(self, id) -> None:
        with self._lock:
            self._read(id)
            shutil.rmtree(self._path(id))

    def update_settings(self, id, patch) -> dict:
        with self._lock:
            current = self._read(id).settings
            connection_settings(patch, strict=True)
            merged = {**current, **patch}
            for key in ("features", "pre_approval"):
                if key in patch:
                    merged[key] = {**current[key], **patch[key]}
            saved = connection_settings(merged, strict=True)
            _write_settings(self._path(id) / "settings.json", saved)
            return saved
