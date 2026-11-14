"""codex-bridge-host entry point."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import uvicorn

from .app import build_host, create_local_app, create_public_app
from .config import load_host_config
from .policy_form import yaml_from_form

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

DEFAULT_POLICY_FORM = {
    "default": "read",
    "folders": [],
    "exec": {"mode": "allowlist", "allow": ["python scripts/**/*.py *"], "confirm_modified_scripts": False},
    "delete_needs_approval": True,
    "paused": False,
}
SAMPLE_POLICY = yaml_from_form(DEFAULT_POLICY_FORM)


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
    print("codex-bridge を起動しました。")
    print(f"  管理画面:           http://127.0.0.1:{cfg.local_port}/admin/")
    print(f"  共有フォルダ:       {cfg.share_root}")
    print(f"  知人の agent の接続先: http://{cfg.public_bind}")
    print("  止めるときは stop.bat を実行するか、このウィンドウを閉じてください。")

    async def watch_shutdown() -> None:
        await host.shutdown.wait()
        print("停止の依頼を受けたので止めます…")
        public.should_exit = local.should_exit = True

    watcher = asyncio.create_task(watch_shutdown())
    await asyncio.gather(public.serve(), local.serve())
    watcher.cancel()
    print("codex-bridge を止めました。")


def main() -> None:
    ap = argparse.ArgumentParser(prog="codex-bridge-host")
    ap.add_argument("command", nargs="?", default="serve", choices=["serve", "setup", "start", "stop"])
    ap.add_argument("--config", default="host.yaml", type=Path)
    args = ap.parse_args()
    if args.command == "setup":
        from .setup_cli import run_setup
        run_setup(args.config)
        return
    if args.command == "start":
        from .setup_cli import run_start
        run_start(args.config)
        return
    if args.command == "stop":
        from .setup_cli import run_stop
        run_stop(args.config)
        return
    if ensure_config(args.config):
        print(f"{args.config} の見本を作りました。share_root と public_bind を書き換えてから、もう一度起動してください。")
        return
    asyncio.run(serve(args.config))


if __name__ == "__main__":
    main()
