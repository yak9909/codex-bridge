"""codex-bridge-host entry point."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import uvicorn

from .app import build_host, create_local_app, create_public_app
from .config import load_host_config

SAMPLE_HOST = """\
# codex-bridge host settings
# share_root: the folder the friend's Codex may work in
share_root: "C:/path/to/your/folder"
# data_dir: logs, snapshots, tokens (keep it OUTSIDE share_root)
data_dir: "data"
# public_bind: this PC's Tailscale IP and a port (see `tailscale ip -4`)
public_bind: "100.64.0.1:8765"
# local_port: Claude Code and the admin page use http://127.0.0.1:<local_port>
local_port: 8770
rg_path: "vendor/rg.exe"
"""

SAMPLE_POLICY = """\
# Permission rules. Saved changes apply immediately.
# level: hidden (invisible) / read / write. Later and more specific rules win;
# a hidden folder hides everything inside it.
default: read
rules:
  # - {path: "analysis/**", level: write}
  # - {path: "private", level: hidden}
exec:
  # off / allowlist (matching commands run, others wait for approval) / ask / all
  mode: allowlist
  allow:
    - "python scripts/**/*.py *"
  confirm_modified_scripts: false
delete_needs_approval: true
paused: false
"""


def ensure_config(path: Path) -> bool:
    """Create sample host.yaml / policy.yaml when missing. Returns True if host.yaml was created."""
    path = Path(path)
    created = False
    if not path.exists():
        path.write_text(SAMPLE_HOST, encoding="utf-8")
        created = True
    cfg = load_host_config(path)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    policy = cfg.data_dir / "policy.yaml"
    if not policy.exists():
        policy.write_text(SAMPLE_POLICY, encoding="utf-8")
    return created


async def serve(config_path: Path) -> None:
    cfg = load_host_config(config_path)
    if not cfg.share_root.is_dir():
        raise SystemExit(f"share_root がありません: {cfg.share_root}")
    if cfg.data_dir.resolve().is_relative_to(cfg.share_root.resolve()):
        raise SystemExit("data_dir は share_root の外に置いてください")
    host = build_host(cfg)
    pub_host, pub_port = cfg.public_bind.rsplit(":", 1)
    public = uvicorn.Server(uvicorn.Config(create_public_app(host), host=pub_host, port=int(pub_port),
                                           log_level="warning", ws_max_size=64 * 1024 * 1024))
    local = uvicorn.Server(uvicorn.Config(create_local_app(host), host="127.0.0.1", port=cfg.local_port,
                                          log_level="warning"))
    print(f"codex-bridge host: 管理画面 http://127.0.0.1:{cfg.local_port}/admin/")
    print(f"  Claude Code: claude mcp add --transport http codex-bridge http://127.0.0.1:{cfg.local_port}/claude/mcp")
    print(f"  知人の agent の接続先: http://{cfg.public_bind}")
    await asyncio.gather(public.serve(), local.serve())


def main() -> None:
    ap = argparse.ArgumentParser(prog="codex-bridge-host")
    ap.add_argument("--config", default="host.yaml", type=Path)
    args = ap.parse_args()
    if ensure_config(args.config):
        print(f"{args.config} の見本を作りました。share_root と public_bind を書き換えてから、もう一度起動してください。")
        return
    asyncio.run(serve(args.config))


if __name__ == "__main__":
    main()
