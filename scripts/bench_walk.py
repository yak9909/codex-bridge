"""Measure complete host scans, using only a disposable system-temp tree.

Run with: uv run --no-sync python scripts/bench_walk.py --files 5000
Tree creation/deletion is outside the measurements. grep deliberately has no
matches, so ripgrep must search the entire tree instead of stopping at its cap.
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path
from time import perf_counter

import yaml

from codex_bridge.host.fs_read import ReadTools
from codex_bridge.host.paths import SafePaths
from codex_bridge.host.policy import PolicyStore
from codex_bridge.host.runner import CommandRunner
from codex_bridge.host.search import SearchTools


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=5000)
    args = parser.parse_args()
    if args.files < 1:
        parser.error("--files must be positive")
    rg = Path(__file__).resolve().parents[1] / "vendor" / "rg.exe"
    with tempfile.TemporaryDirectory(prefix="codex-bridge-bench-walk-") as temporary:
        base = Path(temporary)
        root, data, install = (base / name for name in ("share", "data", "install"))
        for directory in (root, data, install):
            directory.mkdir()
        for start in range(0, args.files, 250):
            directory = root / f"d{start // 250:05}"
            directory.mkdir()
            for number in range(start, min(start + 250, args.files)):
                (directory / f"f{number:06}.txt").write_bytes(b"benchmark text\n")
        policy_file = data / "policy.yaml"
        policy_file.write_text(yaml.safe_dump({
            "folders": [{"path": str(root), "level": "write"}], "main": str(root),
        }), encoding="utf-8", newline="\n")
        policy = PolicyStore(policy_file)
        paths = SafePaths(root, policy, install_dir=install)
        policy.current()
        read = ReadTools(paths, policy)
        search = SearchTools(paths, policy, rg)
        runner = CommandRunner(paths, policy, None, None, None, lambda: None)

        start = perf_counter()
        found = read.find_files("**/*.txt", limit=args.files + 1)
        seconds = perf_counter() - start
        assert len(found) == args.files
        print(f"find_files files={args.files} seconds={seconds:.6f}", flush=True)

        start = perf_counter()
        result = search.grep("absent-benchmark-needle")
        seconds = perf_counter() - start
        assert not result["matches"] and not result["truncated"]
        print(f"grep files={args.files} seconds={seconds:.6f}", flush=True)

        start = perf_counter()
        state = runner._write_area_state()
        seconds = perf_counter() - start
        assert len(state) == args.files
        print(f"_write_area_state files={args.files} seconds={seconds:.6f}", flush=True)


if __name__ == "__main__":
    main()
