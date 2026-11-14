"""Convert a v0.1 policy (one share root + relative rules) into the v0.2 form (absolute folders)."""

from __future__ import annotations

from pathlib import Path


def _is_glob(path: str) -> bool:
    return any(c in path for c in "*?[")


def migrate_v1(raw: dict | None, share_root: Path) -> dict:
    raw = dict(raw or {})
    share = Path(share_root)
    default = raw.pop("default", "read")
    rules = raw.pop("rules", None) or []
    folders: list[dict] = []
    if default in ("read", "write"):
        folders.append({"path": share.as_posix(), "level": default})
    advanced: list[dict] = list(raw.pop("advanced_rules", None) or [])
    for r in rules:
        path = str(r.get("path", ""))
        if _is_glob(path):
            advanced.append({"path": path, "level": r.get("level")})
            continue
        rel = path.replace("\\", "/").strip("/")
        folders.append({"path": (share / rel).as_posix() if rel else share.as_posix(), "level": r.get("level")})
    out = {"main": share.as_posix(), "folders": folders}
    if advanced:
        out["advanced_rules"] = advanced
    out.update(raw)  # exec, delete_needs_approval, paused, limits
    return out
