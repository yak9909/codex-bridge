import subprocess

import yaml

from codex_bridge.host import setup_tools as st


def done(rc=0, out=""):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=out, stderr="")


def test_parse_tailscale_ip():
    assert st.parse_tailscale_ip("100.126.42.83\n") == "100.126.42.83"
    assert st.parse_tailscale_ip("fd7a:115c::1\n100.1.2.3\n") == "100.1.2.3"
    assert st.parse_tailscale_ip("") is None
    assert st.parse_tailscale_ip("Tailscale is stopped.") is None


def test_write_host_config(tmp_path):
    cfg = tmp_path / "host.yaml"
    st.write_host_config(cfg, share_root=tmp_path / "share", data_dir=tmp_path / "data", ip="100.1.2.3")
    d = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert d["public_bind"] == "100.1.2.3:8765" and d["local_port"] == 8770
    assert d["share_root"] == (tmp_path / "share").as_posix()


def test_update_public_bind_keeps_other_lines(tmp_path):
    cfg = tmp_path / "host.yaml"
    cfg.write_text('# my comment\nshare_root: "D:/x"\npublic_bind: "100.1.1.1:8765"\nlocal_port: 8770\n',
                   encoding="utf-8")
    assert st.update_public_bind(cfg, "100.9.9.9") is True
    text = cfg.read_text(encoding="utf-8")
    assert "# my comment" in text and 'public_bind: "100.9.9.9:8765"' in text and "D:/x" in text
    assert st.update_public_bind(cfg, "100.9.9.9") is False


def test_data_dir_must_be_outside_share(tmp_path):
    assert st.inside(tmp_path / "share" / "data", tmp_path / "share")
    assert not st.inside(tmp_path / "data", tmp_path / "share")


def test_register_claude_skips_when_already_registered():
    calls = []

    def run(args, **kw):
        calls.append(args)
        return done(0)
    assert st.register_claude(run, claude="claude", local_port=8770) == "already"
    assert calls == [["claude", "mcp", "get", "codex-bridge"]]


def test_register_claude_adds_at_user_scope():
    calls = []

    def run(args, **kw):
        calls.append(args)
        return done(1) if args[2] == "get" else done(0)
    assert st.register_claude(run, claude="claude", local_port=8770) == "added"
    assert calls[-1] == ["claude", "mcp", "add", "--scope", "user", "--transport", "http", "codex-bridge",
                         "http://127.0.0.1:8770/claude/mcp"]


def test_register_claude_without_claude():
    assert st.register_claude(lambda *a, **k: done(0), claude=None, local_port=8770) == "missing"


def test_firewall_command_limits_to_tailscale_range():
    cmd = st.firewall_command(8765)
    assert "-LocalPort 8765" in cmd and "-RemoteAddress 100.64.0.0/10" in cmd and st.FIREWALL_RULE in cmd


def test_request_shutdown_uses_header():
    seen = {}

    def post(url, headers, timeout):
        seen.update(url=url, headers=headers)
        return 200
    assert st.request_shutdown(8770, post=post) is True
    assert seen["url"] == "http://127.0.0.1:8770/admin/api/shutdown" and seen["headers"]["X-Codex-Bridge"] == "1"
    assert st.request_shutdown(8770, post=lambda *a, **k: (_ for _ in ()).throw(OSError("refused"))) is False


def test_friend_kit_copies_bundled_exe_without_building(tmp_path, monkeypatch):
    # In the released admin zip the agent exe ships next to setup.bat: just copy it.
    from codex_bridge.host import setup_cli
    (tmp_path / "codex-bridge-agent.exe").write_bytes(b"exe")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "friend-guide.md").write_text("guide", encoding="utf-8")
    monkeypatch.setattr(setup_cli.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("built")))
    kit = setup_cli.build_friend_kit(tmp_path, open_folder=False)
    assert (kit / "codex-bridge-agent.exe").read_bytes() == b"exe"
    assert (kit / "使い方.md").read_text(encoding="utf-8") == "guide"


def test_update_public_bind_keeps_port(tmp_path):
    cfg = tmp_path / "host.yaml"
    cfg.write_text('public_bind: "100.1.1.1:9000"\n', encoding="utf-8")
    assert st.update_public_bind(cfg, "100.2.2.2") is True
    assert 'public_bind: "100.2.2.2:9000"' in cfg.read_text(encoding="utf-8")


def test_register_codex_adds_url_server():
    calls = []

    def run(args, **kw):
        calls.append(args)
        return done(1) if args[2] == "get" else done(0)
    assert st.register_codex(run, codex="codex", local_port=8770) == "added"
    assert calls[-1] == ["codex", "mcp", "add", "codex-bridge", "--url", "http://127.0.0.1:8770/claude/mcp"]


def test_register_codex_skips_when_present_or_missing():
    assert st.register_codex(lambda *a, **k: done(0), codex="codex", local_port=8770) == "already"
    assert st.register_codex(lambda *a, **k: done(0), codex=None, local_port=8770) == "missing"


def test_codex_command_wraps_cmd_shims():
    assert st.cli_command("C:/x/codex.cmd") == ["cmd.exe", "/d", "/c", "C:/x/codex.cmd"]
    assert st.cli_command("C:/x/codex.exe") == ["C:/x/codex.exe"]
