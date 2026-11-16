"""Helpers behind setup.bat / start.bat / stop.bat (kept here so they can be tested)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable

FIREWALL_RULE = "codex-bridge (Tailscale)"
MCP_NAME = "codex-bridge"
PUBLIC_PORT = 8765
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---- Tailscale -------------------------------------------------------------
def tailscale_exe() -> str | None:
    found = shutil.which("tailscale")
    if found:
        return found
    default = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tailscale" / "tailscale.exe"
    return str(default) if default.exists() else None


def parse_tailscale_ip(output: str) -> str | None:
    for line in (output or "").splitlines():
        line = line.strip()
        if re.fullmatch(r"100\.\d{1,3}\.\d{1,3}\.\d{1,3}", line):
            return line
    return None


def tailscale_ip(run=subprocess.run) -> str | None:
    exe = tailscale_exe()
    if not exe:
        return None
    try:
        r = run([exe, "ip", "-4"], capture_output=True, text=True, timeout=15, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_tailscale_ip(r.stdout)


# ---- host.yaml ---------------------------------------------------------------
def write_host_config(path: Path, share_root: Path | None, data_dir: Path, ip: str,
                      port: int = PUBLIC_PORT, local_port: int = 8770, rg_path: str = "vendor/rg.exe") -> None:
    share_line = "null" if share_root is None else json.dumps(Path(share_root).as_posix(), ensure_ascii=False)
    text = (
        "# codex-bridge host settings (made by setup.bat)\n"
        "# share_root: the folder the friend's Codex may work in (choose it on the admin page)\n"
        f"share_root: {share_line}\n"
        "# data_dir: logs, snapshots, tokens (must be OUTSIDE share_root)\n"
        f"data_dir: {json.dumps(Path(data_dir).as_posix(), ensure_ascii=False)}\n"
        "# public_bind: this PC's Tailscale IP (start.bat updates it if the IP changes)\n"
        f'public_bind: "{ip}:{port}"\n'
        f"local_port: {local_port}\n"
        f'rg_path: "{rg_path}"\n'
    )
    Path(path).write_text(text, encoding="utf-8")


def update_public_bind(path: Path, ip: str, port: int | None = None) -> bool:
    """Point public_bind at ip (keeping its port and the rest of the file). Returns True if it changed."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    m = re.search(r"^public_bind:.*$", text, flags=re.MULTILINE)
    if port is None:
        old = re.search(r":(\d+)\D*$", m.group(0)) if m else None
        port = int(old.group(1)) if old else PUBLIC_PORT
    new_line = f'public_bind: "{ip}:{port}"'
    if m and m.group(0).strip() == new_line:
        return False
    text = text[:m.start()] + new_line + text[m.end():] if m else text.rstrip("\n") + "\n" + new_line + "\n"
    path.write_text(text, encoding="utf-8")
    return True


def set_yaml_value(path: Path, key: str, value: str) -> None:
    """Replace (or append) a top-level 'key: "value"' line, keeping comments and other lines."""
    path = Path(path)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    line = f"{key}: {json.dumps(value, ensure_ascii=False)}"
    m = re.search(rf"^{re.escape(key)}:.*$", text, flags=re.MULTILINE)
    text = text[:m.start()] + line + text[m.end():] if m else text.rstrip("\n") + "\n" + line + "\n"
    path.write_text(text, encoding="utf-8")


def inside(child: Path, parent: Path) -> bool:
    try:
        Path(child).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


# ---- Windows firewall --------------------------------------------------------
def firewall_command(port: int = PUBLIC_PORT) -> str:
    return (f'New-NetFirewallRule -DisplayName "{FIREWALL_RULE}" -Direction Inbound -Protocol TCP '
            f"-LocalPort {port} -RemoteAddress 100.64.0.0/10 -Action Allow")


def firewall_rule_exists(run=subprocess.run) -> bool:
    r = run(["powershell", "-NoProfile", "-Command",
             f'if (Get-NetFirewallRule -DisplayName "{FIREWALL_RULE}" -ErrorAction SilentlyContinue) {{ exit 0 }} else {{ exit 1 }}'],
            capture_output=True, text=True, creationflags=NO_WINDOW)
    return r.returncode == 0


def add_firewall_rule_elevated(port: int = PUBLIC_PORT) -> bool:
    """Ask Windows for administrator rights (UAC) and add the rule. Waits for it to finish."""
    import ctypes
    import ctypes.wintypes as wt

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wt.HWND), ("lpVerb", wt.LPCWSTR),
                    ("lpFile", wt.LPCWSTR), ("lpParameters", wt.LPCWSTR), ("lpDirectory", wt.LPCWSTR),
                    ("nShow", ctypes.c_int), ("hInstApp", wt.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                    ("lpClass", wt.LPCWSTR), ("hkeyClass", wt.HKEY), ("dwHotKey", wt.DWORD),
                    ("hIconOrMonitor", wt.HANDLE), ("hProcess", wt.HANDLE)]

    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x40  # SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = "powershell.exe"
    info.lpParameters = f"-NoProfile -Command {firewall_command(port)}"
    info.nShow = 0
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
        return False  # the user said no to the UAC prompt
    ctypes.windll.kernel32.WaitForSingleObject(info.hProcess, 60_000)
    ctypes.windll.kernel32.CloseHandle(info.hProcess)
    return firewall_rule_exists()


# ---- Claude Code -----------------------------------------------------------
def register_claude(run=subprocess.run, claude: str | None = "auto", local_port: int = 8770) -> str:
    """Register the Claude-facing MCP endpoint at user scope. Returns missing / already / added / failed."""
    if claude == "auto":
        claude = shutil.which("claude")
    if not claude:
        return "missing"
    if run([claude, "mcp", "get", MCP_NAME], capture_output=True, text=True, encoding="utf-8", errors="replace").returncode == 0:
        return "already"
    r = run([claude, "mcp", "add", "--scope", "user", "--transport", "http", MCP_NAME,
             f"http://127.0.0.1:{local_port}/claude/mcp"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    return "added" if r.returncode == 0 else "failed"


def cli_command(path: str) -> list[str]:
    """npm installs CLIs as .cmd shims; run those through cmd.exe."""
    return ["cmd.exe", "/d", "/c", path] if str(path).lower().endswith((".cmd", ".bat")) else [path]


def register_codex(run=subprocess.run, codex: str | None = "auto", local_port: int = 8770) -> str:
    """Register the same control endpoint in the owner's own Codex CLI. Returns missing / already / added / failed."""
    if codex == "auto":
        codex = shutil.which("codex")
    if not codex:
        return "missing"
    base = cli_command(codex)
    if run([*base, "mcp", "get", MCP_NAME], capture_output=True, text=True, encoding="utf-8", errors="replace").returncode == 0:
        return "already"
    r = run([*base, "mcp", "add", MCP_NAME, "--url", f"http://127.0.0.1:{local_port}/claude/mcp"],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
    return "added" if r.returncode == 0 else "failed"


def codex_config_path() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "config.toml"


def codex_auto_approve(config: Path | None = None) -> bool:
    """Let the owner's Codex call codex-bridge tools without asking each time (they poll get_status a lot).

    Inserts default_tools_approval_mode = "approve" into [mcp_servers.codex-bridge]. Returns True if changed."""
    path = Path(config or codex_config_path())
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    header = f"[mcp_servers.{MCP_NAME}]"
    m = re.search(rf"^{re.escape(header)}[ \t]*$", text, flags=re.MULTILINE)
    if not m:
        return False
    nxt = re.search(r"^\[", text[m.end():], flags=re.MULTILINE)
    table = text[m.end(): m.end() + nxt.start()] if nxt else text[m.end():]
    if re.search(r"^\s*default_tools_approval_mode\s*=", table, flags=re.MULTILINE):
        return False
    text = text[:m.end()] + '\ndefault_tools_approval_mode = "approve"' + text[m.end():]
    path.write_text(text, encoding="utf-8")
    return True


# ---- stop ------------------------------------------------------------------
def _http_post(url: str, headers: dict, timeout: float) -> int:
    import urllib.request
    req = urllib.request.Request(url, data=b"{}", headers={**headers, "Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status


def request_shutdown(local_port: int, post: Callable = _http_post) -> bool:
    try:
        status = post(f"http://127.0.0.1:{local_port}/admin/api/shutdown", headers={"X-Codex-Bridge": "1"},
                      timeout=5)
    except OSError:
        return False
    return status == 200
