"""codex-bridge-host entry point."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import uvicorn

from .app import build_host, create_local_app, create_public_app
from .config import configured_folder, effective_share_root, load_host_config
from .policy_form import yaml_from_form

SAMPLE_HOST = """\
# codex-bridge host settings
# 共有フォルダは起動後に管理画面で選べます。
share_root: null
# data_dir: logs, snapshots, tokens (keep it OUTSIDE share_root)
data_dir: "data"
# public_bind: this PC's Tailscale IP and a port (see `tailscale ip -4`)
public_bind: "100.64.0.1:8765"
# local_port: Claude Code and the admin page use http://127.0.0.1:<local_port>
local_port: 8770
rg_path: "vendor/rg.exe"
"""

def sample_policy(share_root: Path | None) -> str:
    """First policy: the chosen folder, read-only, as the main folder."""
    folder = configured_folder(Path(share_root)) if share_root is not None else None
    share = folder.as_posix() if folder is not None else None
    return yaml_from_form({
        "main": share,
        "folders": [{"path": share, "level": "read"}] if share else [],
        "exec": {"mode": "allowlist", "allow": ["python scripts/**/*.py *"], "confirm_modified_scripts": False},
        "delete_needs_approval": True,
        "paused": False,
    })


def ensure_config(path: Path) -> bool:
    """Create sample host.yaml / policy.yaml when missing. Returns True if host.yaml was created."""
    path = Path(path)
    created = False
    if not path.exists():
        path.write_text(SAMPLE_HOST, encoding="utf-8", newline="\n")
        created = True
    cfg = load_host_config(path)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    policy = cfg.data_dir / "policy.yaml"
    if not policy.exists():
        policy.write_text(sample_policy(cfg.share_root), encoding="utf-8", newline="\n")
    return created


async def serve(config_path: Path) -> None:
    cfg = load_host_config(config_path)
    for root in (cfg.share_root, effective_share_root(cfg)):
        if root is not None and cfg.data_dir.resolve().is_relative_to(root.resolve()):
            raise SystemExit("data_dir は share_root の外に置いてください")
    host = build_host(cfg, config_path=config_path)
    pub_host, pub_port = cfg.public_bind.rsplit(":", 1)
    public = uvicorn.Server(uvicorn.Config(create_public_app(host), host=pub_host, port=int(pub_port),
                                           log_level="warning", ws_max_size=64 * 1024 * 1024))
    local = uvicorn.Server(uvicorn.Config(create_local_app(host), host="127.0.0.1", port=cfg.local_port,
                                          log_level="warning"))
    print("codex-bridge を起動しました。")
    print(f"  管理画面:           http://127.0.0.1:{cfg.local_port}/admin/")
    print(f"  共有フォルダ: {host.paths.root or '未設定（管理画面で選んでください）'}")
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
    ap = argparse.ArgumentParser(prog="codex-bridge host")
    ap.add_argument("command", nargs="?", default="serve", choices=["serve", "setup", "start", "stop", "install-mcp"])
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
    if args.command == "install-mcp":
        from .setup_cli import run_install_mcp
        run_install_mcp(args.config)
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
