"""Agent tokens: only SHA-256 hashes are stored."""

from __future__ import annotations

import hmac
import json
import secrets
import threading
import time
from pathlib import Path

from .mcp_util import sha256_hex


class TokenStore:
    def __init__(self, data_dir: Path) -> None:
        self.path = Path(data_dir) / "tokens.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._items: dict[str, dict] = {}
        if self.path.exists():
            self._items = json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self) -> None:
        self.path.write_text(json.dumps(self._items, ensure_ascii=False, indent=1), encoding="utf-8")

    def issue(self, name: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._items[name] = {"sha256": sha256_hex(token), "created": time.time()}
            self._save()
        return token

    def revoke(self, name: str) -> None:
        with self._lock:
            self._items.pop(name, None)
            self._save()

    def hashes(self) -> set[str]:
        return {v["sha256"] for v in self._items.values()}

    def match(self, token: str) -> str | None:
        """The invite name this token was issued under, or None."""
        if not token:
            return None
        digest = sha256_hex(token)
        found = None
        for name, v in list(self._items.items()):
            if hmac.compare_digest(digest, v["sha256"]):
                found = name
        return found

    def list(self) -> list[dict]:
        return [{"name": k, "created": v["created"]} for k, v in self._items.items()]
