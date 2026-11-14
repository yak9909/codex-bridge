import io
import tarfile

import pytest

from codex_bridge.agent import setup
from codex_bridge.common.invite import make_invite, parse_invite

CODE = make_invite("http://100.64.0.1:8765", "tok-secret-123", "friend")


def test_invite_stored_encrypted(tmp_path):
    setup.save_invite(tmp_path, parse_invite(CODE))
    raw = (tmp_path / "invite.bin").read_bytes()
    assert b"tok-secret-123" not in raw and b"100.64" not in raw
    assert setup.load_invite(tmp_path).token == "tok-secret-123"


def test_load_invite_missing_or_corrupt(tmp_path):
    assert setup.load_invite(tmp_path) is None
    (tmp_path / "invite.bin").write_bytes(b"garbage")
    assert setup.load_invite(tmp_path) is None


def test_find_codex_prefers_path_then_bundled(tmp_path, monkeypatch):
    bundled = tmp_path / "bin" / "codex.exe"
    monkeypatch.setattr(setup.shutil, "which", lambda name: None)
    assert setup.find_codex(tmp_path / "bin") is None
    bundled.parent.mkdir()
    bundled.write_bytes(b"x")
    assert setup.find_codex(tmp_path / "bin") == bundled
    monkeypatch.setattr(setup.shutil, "which", lambda name: r"C:\tools\codex.cmd")
    assert str(setup.find_codex(tmp_path / "bin")) == r"C:\tools\codex.cmd"


def test_apikey_login_disables_quota_guard():
    assert setup.make_guard("chatgpt").enabled is True
    assert setup.make_guard("apikey").enabled is False


def test_pick_asset():
    assets = [{"name": "codex-aarch64-pc-windows-msvc.exe", "browser_download_url": "a"},
              {"name": "codex-package-x86_64-pc-windows-msvc.tar.gz", "browser_download_url": "b"},
              {"name": "codex-x86_64-pc-windows-msvc.exe", "browser_download_url": "c"}]
    assert setup.pick_asset(assets)["browser_download_url"] == "b"
    with pytest.raises(RuntimeError):
        setup.pick_asset([{"name": "x", "browser_download_url": "x"}])


def test_extract_package_finds_codex_exe(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in (("pkg/bin/codex.exe", b"exe"), ("pkg/bin/helper.exe", b"h"), ("../evil.txt", b"x")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    archive = tmp_path / "p.tar.gz"
    archive.write_bytes(buf.getvalue())
    exe = setup.extract_package(archive, tmp_path / "bin")
    assert exe.read_bytes() == b"exe" and (exe.parent / "helper.exe").exists()
    assert not (tmp_path / "evil.txt").exists()


def test_user_mcp_names_from_codex_config(tmp_path):
    (tmp_path / "config.toml").write_text(
        'model = "x"\n[mcp_servers.blender]\ncommand = "b"\n[mcp_servers.node_repl]\ncommand = "n"\n'
        '[mcp_servers.bridge]\nurl = "u"\n', encoding="utf-8")
    assert setup.user_mcp_names(tmp_path) == ["blender", "node_repl"]
    assert setup.user_mcp_names(tmp_path / "missing") == []
    (tmp_path / "config.toml").write_text("not = [valid", encoding="utf-8")
    assert setup.user_mcp_names(tmp_path) == []


def test_parse_and_compare_codex_version():
    assert setup.parse_version("codex-cli 0.167.0\n") == (0, 167, 0)
    assert setup.parse_version("garbage") is None
    assert setup.version_ok((0, 167, 0)) and setup.version_ok((0, 170, 1)) and setup.version_ok((1, 0, 0))
    assert not setup.version_ok((0, 120, 9)) and not setup.version_ok(None)


def test_choose_codex_prefers_new_enough(tmp_path):
    path_codex, bundled = tmp_path / "codex.cmd", tmp_path / "bin" / "codex.exe"
    versions = {path_codex: (0, 100, 0), bundled: (0, 169, 0)}
    get = lambda p: versions.get(p)
    assert setup.choose_codex(path_codex, bundled, get) == bundled          # PATH one too old
    versions[path_codex] = (0, 168, 0)
    assert setup.choose_codex(path_codex, bundled, get) == path_codex
    assert setup.choose_codex(None, None, get) is None
    versions[bundled] = (0, 1, 0)
    versions[path_codex] = (0, 1, 0)
    assert setup.choose_codex(path_codex, bundled, get) is None             # nothing usable: download
