"""Invite codes: 'cbx1.' + base64url(JSON {url, token, name})."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass

PREFIX = "cbx1."


@dataclass(frozen=True)
class Invite:
    url: str
    token: str
    name: str


def make_invite(url: str, token: str, name: str) -> str:
    body = json.dumps({"url": url, "token": token, "name": name}, ensure_ascii=False).encode("utf-8")
    return PREFIX + base64.urlsafe_b64encode(body).decode("ascii").rstrip("=")


def parse_invite(code: str) -> Invite:
    code = code.strip()
    err = ValueError("招待コードの形式が正しくありません。もう一度コピーして貼り付けてください。")
    if not code.startswith(PREFIX) or len(code) <= len(PREFIX):
        raise err
    b = code[len(PREFIX):]
    try:
        data = json.loads(base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)))
        return Invite(str(data["url"]), str(data["token"]), str(data.get("name", "")))
    except (binascii.Error, ValueError, KeyError, TypeError, UnicodeDecodeError):
        raise err from None
