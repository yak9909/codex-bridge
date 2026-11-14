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
