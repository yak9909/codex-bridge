"""Host configuration (host.yaml)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class HostConfig:
    share_root: Path
    data_dir: Path
    public_bind: str  # "<tailscale ip>:<port>"
    local_port: int
    rg_path: Path
    install_dir: Path | None = None  # directory containing host.yaml and the host executables/scripts


def load_host_config(path: Path) -> HostConfig:
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    base = path.parent
    def p(v: str) -> Path:
        q = Path(v)
        return q if q.is_absolute() else (base / q).resolve()
    return HostConfig(
        share_root=p(raw["share_root"]),
        data_dir=p(raw.get("data_dir", "data")),
        public_bind=str(raw.get("public_bind", "127.0.0.1:8765")),
        local_port=int(raw.get("local_port", 8770)),
        rg_path=p(raw.get("rg_path", "vendor/rg.exe")),
        install_dir=base.resolve(),
    )
