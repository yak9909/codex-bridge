"""Host configuration (host.yaml)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class HostConfig:
    share_root: Path | None
    data_dir: Path
    public_bind: str  # "<tailscale ip>:<port>"
    local_port: int
    rg_path: Path
    install_dir: Path | None = None  # directory containing host.yaml and the host executables/scripts


def configured_folder(path: Path | None) -> Path | None:
    """An old sample or a folder that no longer exists needs owner setup."""
    if path is None or path.as_posix().rstrip("/").lower() == "c:/path/to/your/folder":
        return None
    return path if path.is_dir() else None


def load_host_config(path: Path) -> HostConfig:
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    base = path.parent
    def p(v: str) -> Path:
        q = Path(v)
        return q if q.is_absolute() else (base / q).resolve()
    share = str(raw.get("share_root") or "").strip()
    return HostConfig(
        share_root=configured_folder(p(share)) if share else None,
        data_dir=p(raw.get("data_dir", "data")),
        public_bind=str(raw.get("public_bind", "127.0.0.1:8765")),
        local_port=int(raw.get("local_port", 8770)),
        rg_path=p(raw.get("rg_path", "vendor/rg.exe")),
        install_dir=base.resolve(),
    )
