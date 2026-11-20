"""Exercise release updates with in-memory downloads and temporary installations."""

import hashlib
import io
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_bridge import updater


@pytest.mark.parametrize(("tag", "current", "newer"), [
    ("v0.10.0", "0.9.9", True),
    ("v10.0.0", "v2.99.3", True),
    ("v0.3.0", "0.3.0", False),
    ("v0.2.9", "0.3.0", False),
    ("0.3.1", "0.3.0", False),  # a GitHub tag must start with v
    ("v0.3.1-rc.1", "0.3.0", False),
    ("v.3.1", "0.3.0", False),
])
def test_release_version(tag, current, newer):
    name = f"codex-bridge-{tag}.zip"
    url = f"{updater.DOWNLOAD_PREFIX}{tag}/{name}"
    doc = {"tag_name": tag, "assets": [{"name": name, "browser_download_url": url, "digest": "sha256:" + "A" * 64}]}
    assert bool(updater.parse_release(doc, current)) is newer
    assert updater.version_parts("v0.3.1") == updater.version_parts("0.3.1") == (0, 3, 1)


@pytest.mark.parametrize("flag", ["draft", "prerelease"])
def test_unpublished_release_is_ignored(flag):
    assert updater.parse_release({"tag_name": "v9.0.0", flag: True}) is None


def test_check_release_uses_the_public_endpoint(monkeypatch):
    seen = []

    def open_fake(url):
        seen.append(url)
        return io.BytesIO(b'{"tag_name": "v0.0.0"}')

    monkeypatch.setattr(updater, "open_github", open_fake)
    assert updater.check_release() is None
    assert seen == ["https://api.github.com/repos/yak9909/codex-bridge/releases/latest"]


def test_release_requires_the_named_asset_and_digest():
    doc = {"tag_name": "v9.0.0", "assets": []}
    with pytest.raises(updater.UpdateError, match="見つかりません"):
        updater.parse_release(doc)
    doc["assets"] = [{"name": "codex-bridge-v9.0.0.zip"}]
    with pytest.raises(updater.UpdateError, match="確かめられない"):
        updater.parse_release(doc)
    doc["assets"][0].update(digest="sha256:" + "a" * 64, browser_download_url="https://example.invalid/release.zip")
    with pytest.raises(updater.UpdateError, match="入手先"):
        updater.parse_release(doc)


def fake_download(monkeypatch, extra=None, *, correct_digest=True, exe=True):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        for name in updater.PACKAGE_FILES:
            if name != updater.EXE or exe:
                package.writestr("codex-bridge-v9.0.0/" + name, ("new " + name).encode())
        if extra:
            package.writestr(extra, b"blocked")
        package.writestr("codex-bridge-v9.0.0/host.yaml", b"not part of the distribution")
    contents = buffer.getvalue()
    digest = hashlib.sha256(contents).hexdigest() if correct_digest else "a" * 64
    release = updater.Release("9.0.0", "fake", digest)
    monkeypatch.setattr(updater, "open_github", lambda url: io.BytesIO(contents))
    return release


@pytest.mark.parametrize("name", ["../escape", "/absolute", "C:/absolute", "x/../../escape", "x\\..\\escape", "x/.. /escape"])
def test_zip_rejects_escaping_paths(monkeypatch, tmp_path, name):
    release = fake_download(monkeypatch, name)
    with pytest.raises(updater.UpdateError, match="使えないパス"):
        updater.prepare_release(release, tmp_path / "data")
    assert not list((tmp_path / "data" / "updates").iterdir())
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize(("digest", "exe", "message"), [(False, True, "SHA-256"), (True, False, "内容")])
def test_discard_unusable_download(monkeypatch, tmp_path, digest, exe, message):
    release = fake_download(monkeypatch, correct_digest=digest, exe=exe)
    with pytest.raises(updater.UpdateError, match=message):
        updater.prepare_release(release, tmp_path)
    assert not list((tmp_path / "updates").iterdir())


@pytest.mark.parametrize("launch_fails", [False, True])
def test_replace_and_rollback(monkeypatch, tmp_path, launch_fails):
    exe = tmp_path / updater.EXE
    exe.write_bytes(b"old exe")
    bat = tmp_path / "start.bat"
    bat.write_bytes(b"old bat")
    config = tmp_path / "host.yaml"
    config.write_bytes(b"config")
    extra_file = tmp_path / "custom.txt"
    extra_file.write_bytes(b"custom")
    data = tmp_path / "data"
    data.mkdir()
    (data / "task.json").write_bytes(b"task")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "notes.md").write_bytes(b"notes")
    package = updater.prepare_release(fake_download(monkeypatch), data)
    old = tmp_path / "codex-bridge.exe.old"
    assert not (package / "host.yaml").exists()
    calls = []

    def launch(path):
        calls.append(path)
        assert path.read_bytes() == b"new codex-bridge.exe"
        assert old.read_bytes() == b"old exe"
        assert bat.read_bytes() == b"new start.bat"
        assert (tmp_path / "vendor" / "rg.exe").read_bytes() == b"new vendor/rg.exe"
        if launch_fails:
            raise OSError("could not start")

    if launch_fails:
        with pytest.raises(OSError, match="could not start"):
            updater.install_release(exe, package, launch=launch)
        assert exe.read_bytes() == b"old exe"
        assert bat.read_bytes() == b"old bat"
        assert not (tmp_path / "vendor").exists()
        assert not old.exists()
    else:
        updater.install_release(exe, package, launch=launch)
        assert old.is_file()
        updater.clean_up(exe, data)
        assert exe.read_bytes() == b"new codex-bridge.exe"
        assert not (old.exists() or (data / "updates").exists())
    assert calls == [exe]
    assert config.read_bytes() == b"config"
    assert extra_file.read_bytes() == b"custom"
    assert (data / "task.json").read_bytes() == b"task"
    assert (tmp_path / "docs" / "notes.md").read_bytes() == b"notes"


def test_no_install_changes_if_cannot_create_staging(monkeypatch, tmp_path):
    exe = tmp_path / updater.EXE
    exe.write_bytes(b"old exe")
    package = updater.prepare_release(fake_download(monkeypatch), tmp_path / "data")

    def no_access(*args, **kwargs):
        raise PermissionError("read-only installation")

    monkeypatch.setattr(updater.tempfile, "mkdtemp", no_access)
    with pytest.raises(PermissionError, match="read-only"):
        updater.install_release(exe, package, launch=lambda path: pytest.fail("launched"))
    assert exe.read_bytes() == b"old exe"
    assert not exe.with_suffix(".exe.old").exists()


def test_update_mode_defaults_and_persists(tmp_path: Path):
    path = tmp_path / "update-settings.json"
    assert updater.mode(path) == "confirm"
    updater.save_mode(path, "auto")
    assert updater.mode(path) == "auto"
    updater.save_mode(path, "confirm")
    assert updater.mode(path) == "confirm"
    path.write_text("broken", encoding="utf-8")
    assert updater.mode(path) == "confirm"


@pytest.mark.parametrize("ready", [True, False])
def test_restart_requires_child_acknowledgement(monkeypatch, tmp_path, ready):
    exe = tmp_path / updater.EXE
    exe.write_bytes(b"old exe")
    package = updater.prepare_release(fake_download(monkeypatch), tmp_path / "data")

    def spawn(argv, **kwargs):
        assert argv[:2] == [str(exe), updater.RESTART_ARG]
        assert kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
        assert not updater.acknowledge_restart(exe, "../outside")
        if ready:
            assert updater.acknowledge_restart(exe, argv[2])
        return SimpleNamespace(poll=lambda: None if ready else 1)

    monkeypatch.setattr(updater.subprocess, "Popen", spawn)
    if ready:
        updater.install_release(exe, package)
        assert exe.read_bytes() == b"new codex-bridge.exe"
    else:
        with pytest.raises(updater.UpdateError, match="起動直後"):
            updater.install_release(exe, package)
        assert exe.read_bytes() == b"old exe"
    assert not list(tmp_path.glob(updater.INSTALL_STAGE + "*"))


def test_unreadable_installed_version_disables_update():
    assert updater.parse_release({"tag_name": "v9.0.0"}, "0.3.1.dev0") is None


@pytest.mark.parametrize("at_startup", [True, False])
def test_locked_old_exe_is_renamed(monkeypatch, tmp_path, at_startup):
    exe = tmp_path / updater.EXE
    exe.write_bytes(b"current")
    old = exe.with_suffix(".exe.old")
    old.write_bytes(b"previous")
    original_unlink = Path.unlink

    def unlink(path, *args, **kwargs):
        if path == old:
            raise PermissionError("mapped by old bootloader")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    if at_startup:
        updater.clean_up(exe, tmp_path / "data")
        assert not old.exists()
    else:
        package = updater.prepare_release(fake_download(monkeypatch), tmp_path / "data")
        updater.install_release(exe, package, launch=lambda path: None)
        assert old.read_bytes() == b"current"
        assert exe.read_bytes() == b"new codex-bridge.exe"
    assert [file.read_bytes() for file in tmp_path.glob("codex-bridge.exe.old-*")] == [b"previous"]


def test_restart_timeout_restores_install_and_retains_backups_until_failure(monkeypatch, tmp_path):
    exe = tmp_path / updater.EXE
    exe.write_bytes(b"current")
    package = updater.prepare_release(fake_download(monkeypatch), tmp_path / "data")
    update_clock = iter([0, 46])
    monkeypatch.setattr(updater, "time", SimpleNamespace(monotonic=lambda: next(update_clock)))
    calls = []

    def spawn(argv, **kwargs):
        stage = tmp_path / argv[2]
        assert stage.is_dir()
        assert exe.with_suffix(".exe.old").read_bytes() == b"current"
        return SimpleNamespace(poll=lambda: None, terminate=lambda: calls.append("terminate"),
                               wait=lambda timeout: calls.append("wait"))

    monkeypatch.setattr(updater.subprocess, "Popen", spawn)
    with pytest.raises(updater.UpdateError, match="起動を確認できません"):
        updater.install_release(exe, package)
    assert calls == ["terminate", "wait"]
    assert exe.read_bytes() == b"current"
    assert not list(tmp_path.glob(updater.INSTALL_STAGE + "*"))


def test_install_preflight_checks_rename_without_moving_exe(tmp_path, monkeypatch):
    exe = tmp_path / updater.EXE
    exe.write_bytes(b"current")
    updater.check_install_folder(exe)

    def deny(path):
        assert path == exe
        raise PermissionError("rename denied")

    monkeypatch.setattr(updater, "_check_exe_rename", deny)
    with pytest.raises(updater.UpdateError, match="exe のフォルダに書き込めない"):
        updater.check_install_folder(exe)
    assert list(tmp_path.iterdir()) == [exe]
    assert exe.read_bytes() == b"current"


def test_install_can_use_unique_backup_if_previous_exe_cannot_be_renamed(tmp_path, monkeypatch):
    exe = tmp_path / updater.EXE
    exe.write_bytes(b"current")
    old = exe.with_suffix(".exe.old")
    old.write_bytes(b"previous")
    package = updater.prepare_release(fake_download(monkeypatch), tmp_path / "data")

    def locked(path, *, preserve):
        assert path == old
        raise PermissionError("previous exe is locked")

    monkeypatch.setattr(updater, "_free_old_exe", locked)
    updater.install_release(exe, package, launch=lambda path: None)
    assert exe.read_bytes() == b"new codex-bridge.exe"
    assert old.read_bytes() == b"previous"
    assert [file.read_bytes() for file in tmp_path.glob("codex-bridge.exe.old-*")] == [b"current"]

