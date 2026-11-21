import json
import subprocess

import pytest

import yaml

from codex_bridge import hook
from codex_bridge.host import setup_tools as st


@pytest.fixture(autouse=True)
def isolated_client_configs(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cc"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "cx"))


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


def test_write_host_config_unset(tmp_path):
    cfg = tmp_path / "host.yaml"
    st.write_host_config(cfg, share_root=None, data_dir=tmp_path / "data", ip="100.1.2.3")
    text = cfg.read_text(encoding="utf-8")
    assert "\nshare_root: null\n" in text
    assert "# share_root: the folder the friend's Codex may work in (choose it on the admin page)" in text
    from codex_bridge.host.config import load_host_config
    assert load_host_config(cfg).share_root is None


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
    assert calls[-1] == ["codex", "mcp", "add", "codex-bridge", "--url",
                         "http://127.0.0.1:8770/claude/mcp?client=codex"]


def test_register_codex_skips_when_present_or_missing():
    assert st.register_codex(lambda *a, **k: done(0), codex="codex", local_port=8770) == "already"
    assert st.register_codex(lambda *a, **k: done(0), codex=None, local_port=8770) == "missing"


def test_codex_command_wraps_cmd_shims():
    assert st.cli_command("C:/x/codex.cmd") == ["cmd.exe", "/d", "/c", "C:/x/codex.cmd"]
    assert st.cli_command("C:/x/codex.exe") == ["C:/x/codex.exe"]


def test_codex_auto_approve_is_added_once(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('model = "x"\n\n[mcp_servers.codex-bridge]\nurl = "http://127.0.0.1:8770/claude/mcp"\n\n'
                   '[desktop]\nfoo = 1\n', encoding="utf-8")
    assert st.codex_auto_approve(cfg) is True
    text = cfg.read_text(encoding="utf-8")
    assert '[mcp_servers.codex-bridge]\ndefault_tools_approval_mode = "approve"\nurl' in text
    assert "[desktop]\nfoo = 1" in text
    assert st.codex_auto_approve(cfg) is False          # already there
    import tomllib
    assert tomllib.loads(text)["mcp_servers"]["codex-bridge"]["default_tools_approval_mode"] == "approve"


def test_codex_auto_approve_without_entry(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('model = "x"\n', encoding="utf-8")
    assert st.codex_auto_approve(cfg) is False
    assert st.codex_auto_approve(tmp_path / "missing.toml") is False


def test_hook_command_frozen_and_source(monkeypatch):
    executable = r"C:\a b\デスク\codex-bridge.exe"
    assert st.hook_command("claude", 8770, frozen=True, executable=executable) == (
        '"C:/a b/デスク/codex-bridge.exe" hook claude --port 8770')
    assert st.hook_command("codex", 8770, frozen=True, executable=executable) == (
        '& "C:/a b/デスク/codex-bridge.exe" hook codex --port 8770')
    python = r"C:\a b\デスク\python.exe"
    assert st.hook_command("claude", 8770, frozen=False, executable=python) == (
        '"C:/a b/デスク/python.exe" -m codex_bridge hook claude --port 8770')
    source = st.hook_command("codex", 8770, frozen=False, executable=python)
    assert source.startswith('& "') and "-m codex_bridge hook codex" in source
    monkeypatch.setattr(st.sys, "frozen", True, raising=False)
    monkeypatch.setattr(st.sys, "executable", executable)
    assert st.hook_command("codex", 9001) == (
        '& "C:/a b/デスク/codex-bridge.exe" hook codex --port 9001')


def test_install_claude_hook_creates_file(tmp_path):
    command = '"C:/デスク/codex-bridge.exe" hook claude --port 8770'
    assert st.claude_settings_path() == tmp_path / "cc" / "settings.json"
    assert st.install_claude_hook(command) == "added"
    text = st.claude_settings_path().read_text(encoding="utf-8")
    assert json.loads(text) == {"hooks": {"PostToolUse": [{
        "matcher": "mcp__codex-bridge__.*", "hooks": [{
            "type": "command", "command": command, "asyncRewake": True, "timeout": hook.HOOK_TIMEOUT,
        }],
    }]}}
    assert "デスク" in text and '\n  "hooks": {' in text


@pytest.mark.parametrize("kind,event", [("claude", "PostToolUse"), ("codex", "Stop")])
def test_install_keeps_other_hooks_and_keys(tmp_path, kind, event):
    path = tmp_path / "custom.json"
    old = {"type": "command", "command": f'"D:/old/codex-bridge.exe" hook {kind} --port 8770'}
    other = {"type": "command", "command": f"other-tool hook {kind}", "timeout": 2}
    unrelated = {"matcher": "other", "hooks": [other], "extra": True}
    mixed = {"matcher": "old", "hooks": [old, other], "extra": "keep"}
    empty = {"hooks": []}
    data = {"theme": "日本語", "hooks": {event: [mixed, {"hooks": [old]}, unrelated, empty],
                                         "OtherEvent": [{"hooks": [other]}]}}
    path.write_text(json.dumps(data), encoding="utf-8")
    command = st.hook_command(kind, 9001, frozen=False, executable="C:/new/python.exe")
    install = getattr(st, f"install_{kind}_hook")
    assert install(command, path) == "updated"
    result = json.loads(path.read_text(encoding="utf-8"))
    assert result["theme"] == data["theme"]
    assert result["hooks"]["OtherEvent"] == data["hooks"]["OtherEvent"]
    assert result["hooks"][event][:-1] == [dict(mixed, hooks=[other]), unrelated, empty]
    assert result["hooks"][event][-1]["hooks"][0]["command"] == command


@pytest.mark.parametrize("kind,event", [("claude", "PostToolUse"), ("codex", "Stop")])
def test_old_hook_from_other_folder_replaced(tmp_path, kind, event):
    path = tmp_path / "custom.json"
    old = f'"D:/old/codex-bridge.exe" hook {kind} --port 8770'
    path.write_text(json.dumps({"hooks": {event: [{"matcher": "old", "hooks": [
        {"type": "command", "command": old, "timeout": 10},
    ]}]}}), encoding="utf-8")
    command = st.hook_command(kind, 8770, frozen=True, executable="C:/new/codex-bridge.exe")
    assert getattr(st, f"install_{kind}_hook")(command, path) == "updated"
    groups = json.loads(path.read_text(encoding="utf-8"))["hooks"][event]
    handlers = [handler for group in groups for handler in group["hooks"]]
    assert len(handlers) == 1
    assert handlers[0]["command"] == command and handlers[0]["timeout"] == hook.HOOK_TIMEOUT


@pytest.mark.parametrize("kind", ["claude", "codex"])
def test_install_same_twice_is_already_and_untouched(tmp_path, kind):
    path = tmp_path / "custom.json"
    command = st.hook_command(kind, 8770, frozen=True, executable="C:/new/codex-bridge.exe")
    install = getattr(st, f"install_{kind}_hook")
    assert install(command, path) == "added"
    before, mtime = path.read_bytes(), path.stat().st_mtime_ns
    assert install(command, path) == "already"
    assert path.read_bytes() == before and path.stat().st_mtime_ns == mtime
    assert not path.with_name(path.name + ".bak").exists()


@pytest.mark.parametrize("kind", ["claude", "codex"])
@pytest.mark.parametrize("content", ['{"broken":', '[]', 'null', '{"hooks": []}',
                                     '{"hooks": {"PostToolUse": {}, "Stop": {}}}'])
def test_invalid_json_failed_untouched(tmp_path, kind, content):
    path = tmp_path / "custom.json"
    path.write_text(content, encoding="utf-8")
    before, mtime = path.read_bytes(), path.stat().st_mtime_ns
    command = f'"C:/codex-bridge.exe" hook {kind} --port 8770'
    assert getattr(st, f"install_{kind}_hook")(command, path) == "failed"
    assert path.read_bytes() == before and path.stat().st_mtime_ns == mtime
    assert not path.with_name(path.name + ".bak").exists()


@pytest.mark.parametrize("kind", ["claude", "codex"])
def test_bak_written_before_change(tmp_path, monkeypatch, kind):
    path = tmp_path / "custom.json"
    before = b'{"keep": "original"}\n'
    path.write_bytes(before)
    backup = path.with_name(path.name + ".bak")
    backup.write_text("older backup", encoding="utf-8")
    write_text = type(path).write_text

    def write_after_backup(self, *args, **kwargs):
        if self == path:
            assert backup.read_bytes() == before
        return write_text(self, *args, **kwargs)

    monkeypatch.setattr(type(path), "write_text", write_after_backup)
    command = f'"C:/codex-bridge.exe" hook {kind} --port 8770'
    assert getattr(st, f"install_{kind}_hook")(command, path) == "added"
    assert backup.read_bytes() == before
    assert json.loads(path.read_text(encoding="utf-8"))["keep"] == "original"


def test_codex_hook_in_codex_home(tmp_path):
    command = '& "C:/codex-bridge.exe" hook codex --port 8770'
    assert st.codex_hooks_path() == tmp_path / "cx" / "hooks.json"
    assert st.install_codex_hook(command) == "added"
    assert json.loads(st.codex_hooks_path().read_text(encoding="utf-8")) == {"hooks": {"Stop": [{
        "hooks": [{"type": "command", "command": command, "timeout": hook.HOOK_TIMEOUT}],
    }]}}


@pytest.mark.parametrize("kind,event,filename", [("claude", "PostToolUse", "settings.json"),
                                               ("codex", "Stop", "hooks.json")])
def test_install_hook_reads_bom_and_writes_without_bom(tmp_path, kind, event, filename):
    path = tmp_path / filename
    path.write_text('{"keep": "元の設定"}', encoding="utf-8-sig")
    before = path.read_bytes()
    command = st.hook_command(kind, 8770)
    assert getattr(st, f"install_{kind}_hook")(command, path) == "added"
    assert not path.read_bytes().startswith(b"\xef\xbb\xbf")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["keep"] == "元の設定" and data["hooks"][event][0]["hooks"][0]["command"] == command
    assert path.with_name(path.name + ".bak").read_bytes() == before


@pytest.mark.parametrize("current", [None, "missing_table", "wrong", "correct"])
def test_install_mcp_warns_only_when_codex_url_still_wrong(tmp_path, monkeypatch, capsys, current):
    from codex_bridge.host import setup_cli

    monkeypatch.setattr(st, "register_claude", lambda **kwargs: "missing")
    monkeypatch.setattr(st, "register_codex", lambda **kwargs: "already")
    monkeypatch.setattr(st, "codex_set_url", lambda url: False)
    monkeypatch.setattr(st, "codex_auto_approve", lambda: False)
    url = st.codex_mcp_url(8770)
    config = st.codex_config_path()
    if current is not None:
        config.parent.mkdir()
        text = (f'[mcp_servers.codex-bridge] # bridge\nurl = "{url if current == "correct" else "wrong"}"\n'
                if current != "missing_table" else f'[other]\nurl = "{url}"\n')
        config.write_text(text, encoding="utf-8")
    setup_cli.run_install_mcp(tmp_path / "missing.yaml")
    output = capsys.readouterr().out
    warning = ("  ※ Codex の登録の URL を書き換えられませんでした。config.toml の "
               f"[mcp_servers.codex-bridge] の url を {url} にしてください。")
    assert (warning in output) is (current != "correct")


def test_codex_url_rewritten_on_rerun(tmp_path):
    config = tmp_path / "cx" / "config.toml"
    config.parent.mkdir()
    config.write_text('[mcp_servers.codex-bridge]\nurl = "http://127.0.0.1:8770/claude/mcp"\n'
                      '[other]\nurl = "x"', encoding="utf-8")
    url = st.codex_mcp_url(8770)
    assert url == "http://127.0.0.1:8770/claude/mcp?client=codex"
    assert st.codex_set_url(url) is True
    assert config.read_text(encoding="utf-8") == (
        f'[mcp_servers.codex-bridge]\nurl = "{url}"\n[other]\nurl = "x"')
    before, mtime = config.read_bytes(), config.stat().st_mtime_ns
    assert st.codex_set_url(url) is False
    assert config.read_bytes() == before and config.stat().st_mtime_ns == mtime
    config.write_text('[mcp_servers.codex-bridge]\nenabled = true\n[other]\nurl = "x"', encoding="utf-8")
    assert st.codex_set_url(url, config) is True
    import tomllib
    data = tomllib.loads(config.read_text(encoding="utf-8"))
    assert data["mcp_servers"]["codex-bridge"] == {"url": url, "enabled": True}
    assert data["other"] == {"url": "x"}
    assert st.codex_set_url(url, tmp_path / "missing.toml") is False
    config.write_text('[other]\nurl = "x"', encoding="utf-8")
    assert st.codex_set_url(url, config) is False
    assert config.read_text(encoding="utf-8") == '[other]\nurl = "x"'


@pytest.mark.parametrize("result", ["added", "already"])
def test_install_mcp_output(tmp_path, monkeypatch, capsys, result):
    from codex_bridge.host import setup_cli
    monkeypatch.setattr(st, "register_claude", lambda **kwargs: result)
    monkeypatch.setattr(st, "register_codex", lambda **kwargs: result)
    config = tmp_path / "cx" / "config.toml"
    config.parent.mkdir()
    config.write_text('[mcp_servers.codex-bridge]\nurl = "http://127.0.0.1:8770/claude/mcp"\n',
                      encoding="utf-8")
    setup_cli.run_install_mcp(tmp_path / "missing.yaml")
    output = capsys.readouterr().out
    assert "  Claude Code の知らせ: 入れました。" in output
    assert "  Codex の知らせ      : 入れました。" in output
    assert ("  ※ Codex を開いて /hooks で codex-bridge のフックを信頼してください"
            "（最初の 1 回と、install-mcp を実行し直してフックが変わったとき）。") in output
    assert "  依頼したタスクが承認待ちか終了になると、依頼したエージェントに自動で知らせが届きます。" in output
    assert "  （Codex では、知らせを待つ間は会話が続いている扱いです。話しかけるときは Esc で止めてください。）" in output
    import tomllib
    table = tomllib.loads(config.read_text(encoding="utf-8"))["mcp_servers"]["codex-bridge"]
    assert table["url"] == "http://127.0.0.1:8770/claude/mcp?client=codex"
    assert table["default_tools_approval_mode"] == "approve"
    claude_path, codex_path = st.claude_settings_path(), st.codex_hooks_path()
    claude_handler = json.loads(claude_path.read_text(encoding="utf-8"))["hooks"]["PostToolUse"][0]["hooks"][0]
    codex_handler = json.loads(codex_path.read_text(encoding="utf-8"))["hooks"]["Stop"][0]["hooks"][0]
    assert claude_handler["command"] == st.hook_command("claude", 8770)
    assert codex_handler["command"] == st.hook_command("codex", 8770)
    setup_cli.run_install_mcp(tmp_path / "missing.yaml")
    output = capsys.readouterr().out
    assert "  Claude Code の知らせ: 入れてあります。" in output
    assert "  Codex の知らせ      : 入れてあります。" in output
    assert "/hooks" not in output

    for kind, event, path in [("claude", "PostToolUse", claude_path), ("codex", "Stop", codex_path)]:
        old = f'"D:/old/codex-bridge.exe" hook {kind} --port 8770'
        path.write_text(json.dumps({"hooks": {event: [{"hooks": [{"command": old}]}]}}), encoding="utf-8")
    setup_cli.run_install_mcp(tmp_path / "missing.yaml")
    output = capsys.readouterr().out
    assert "  Claude Code の知らせ: 入れ直しました（古い場所のものは外しました）。" in output
    assert "  Codex の知らせ      : 入れ直しました（古い場所のものは外しました）。" in output
    assert "/hooks" in output

    claude_path.write_text("invalid JSON", encoding="utf-8")
    codex_path.write_text("invalid JSON", encoding="utf-8")
    setup_cli.run_install_mcp(tmp_path / "missing.yaml")
    output = capsys.readouterr().out
    assert f"  Claude Code の知らせ: 入れられませんでした（設定ファイルを読めません: {claude_path}）。" in output
    assert f"  Codex の知らせ      : 入れられませんでした（設定ファイルを読めません: {codex_path}）。" in output
    assert "/hooks" not in output

    def unexpected(*args, **kwargs):
        pytest.fail("Hook/config installation requires successful MCP registration")

    for name in ("install_claude_hook", "install_codex_hook", "codex_set_url", "codex_auto_approve"):
        monkeypatch.setattr(st, name, unexpected)
    for skipped in ("missing", "failed"):
        monkeypatch.setattr(st, "register_claude", lambda **kwargs: skipped)
        monkeypatch.setattr(st, "register_codex", lambda **kwargs: skipped)
        setup_cli.run_install_mcp(tmp_path / "missing.yaml")
        output = capsys.readouterr().out
        assert "の知らせ" not in output and "/hooks" not in output
