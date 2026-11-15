"""Walk performance contracts and the C1 boundaries they must preserve."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

from codex_bridge.host.fs_read import ReadTools
from codex_bridge.host.paths import SafePaths, ToolError
from codex_bridge.host.policy import PolicyStore, glob_match
from codex_bridge.host.runner import CommandRunner
from codex_bridge.host.search import SearchTools

RG = Path(__file__).resolve().parents[2] / "vendor" / "rg.exe"


def make_paths(tmp_path, *, broad=False, legacy=False):
    root = tmp_path / "share"
    data = tmp_path / "data"
    root.mkdir()
    data.mkdir()
    raw = {"default": "write"} if legacy else {
        "folders": [{"path": str(tmp_path if broad else root), "level": "write"}],
        "main": str(root),
    }
    policy = PolicyStore(data / "policy.yaml")
    # Model an existing manually edited policy, including an unsafe broad share.
    policy.path.write_text(yaml.safe_dump(raw), encoding="utf-8", newline="\n")
    paths = SafePaths(root, policy, install_dir=tmp_path / "install")
    policy.current()
    return paths, policy


def link_to(link, target, kind):
    if kind == "junction":
        if os.name != "nt":
            pytest.skip("Windows junctions only")
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                       check=True, capture_output=True)
    else:
        try:
            link.symlink_to(target, target_is_directory=target.is_dir())
        except OSError as exc:
            pytest.skip(f"symlinks unavailable: {exc}")


@pytest.mark.parametrize("with_link", [False, True])
def test_walk_resolves_only_links(tmp_path, monkeypatch, with_link):
    paths, _ = make_paths(tmp_path)
    for i in range(20):
        directory = paths.root / f"d{i:02}"
        directory.mkdir()
        for j in range(100):
            (directory / f"f{j:03}.txt").write_bytes(b"needle\n")
    if with_link:
        link_to(paths.root / "alias.txt", paths.root / "d00/f000.txt", "symlink")
    counts = {"resolve": 0, "realpath": 0}
    original_resolve = Path.resolve
    original_realpath = os.path.realpath

    def resolve(self, *args, **kwargs):
        counts["resolve"] += 1
        return original_resolve(self, *args, **kwargs)

    def realpath(*args, **kwargs):
        counts["realpath"] += 1
        return original_realpath(*args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setattr(os.path, "realpath", realpath)
    files = list(paths.walk(paths.root, "read"))
    assert len(files) == 2000 + int(with_link)
    # resolve delegates to realpath: bound each counter, rather than double-counting.
    assert all(n <= 1 + int(with_link) for n in counts.values()), counts
    assert all(isinstance(p, Path) and isinstance(e, os.DirEntry) for p, e in files)


@pytest.mark.parametrize("kind", ["junction", "symlink"])
def test_walk_hides_link_into_data_dir(tmp_path, kind):
    paths, policy = make_paths(tmp_path, broad=True)
    (policy.path.parent / "secret.txt").write_text("private needle", encoding="utf-8")
    (paths.root / "public.txt").write_text("public needle", encoding="utf-8")
    link_to(paths.root / "peek", policy.path.parent, kind)
    assert [p.name for p, _ in paths.walk(paths.root, "read")] == ["public.txt"]
    assert ReadTools(paths, policy).find_files("**/*", ".") == ["public.txt"]
    hits = SearchTools(paths, policy, RG).grep("needle", ".")["matches"]
    assert [m["path"] for m in hits] == ["public.txt"]
    assert not paths.visible(paths.root / "peek/secret.txt")


def old_find(paths, pattern="**/*"):
    """The pre-change find_files walk, kept as an independent compatibility oracle."""
    found = []
    for base in paths.root_paths():
        if not paths.discoverable(base):
            continue
        for directory, dirs, files in os.walk(base):
            parent = Path(directory)
            dirs[:] = sorted(n for n in dirs if paths.discoverable(parent / n))
            for name in sorted(files):
                path = parent / name
                rel = Path(os.path.relpath(path, base)).as_posix()
                if glob_match(pattern, rel) and paths.discoverable(path):
                    found.append(paths.display(path))
    return found


@pytest.mark.parametrize("legacy", [False, True])
def test_walk_same_results_as_before(tmp_path, legacy):
    paths, policy = make_paths(tmp_path, legacy=legacy)
    files = ["z.txt", "a.txt", "nested/deep/a.txt", "readonly/r.txt", "readonly/build/w.txt",
             "hidden/secret.txt", "hidden/override/secret.txt", ".dot/visible.txt",
             ".CoDeX-BrIdGe/outputs/private.txt", "nested/.codex-bridge/private.txt"]
    for name in files:
        p = paths.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("needle\n", encoding="utf-8")
    rules = [{"path": "hidden", "level": "hidden"},
             {"path": "hidden/override", "level": "write"},
             {"path": "readonly", "level": "read"},
             {"path": "readonly/build", "level": "write"}]
    raw = policy.as_dict()
    raw["rules" if legacy else "advanced_rules"] = rules
    if not legacy:
        other = tmp_path / "other"
        (other / "build").mkdir(parents=True)
        (other / "r.txt").write_text("needle\n", encoding="utf-8")
        (other / "build/w.txt").write_text("needle\n", encoding="utf-8")
        raw["folders"] += [{"path": str(other), "level": "read"},
                           {"path": str(other / "build"), "level": "write"},
                           {"path": str(paths.root / "nested"), "level": "write"}]
    policy.save(raw)
    expected = old_find(paths)
    walked = [paths.display(p) for root in paths.root_paths() for p, _ in paths.walk(root, "read")]
    assert walked == expected
    assert ReadTools(paths, policy).find_files("**/*") == expected
    assert ".dot/visible.txt" in expected
    assert not any("secret" in p or ".codex-bridge" in p.lower() for p in expected)
    writable = {paths.display(p): (e.stat().st_mtime_ns, e.stat().st_size)
                for root in paths.root_paths() for p, e in paths.walk(root, "write")}
    assert set(writable) == {p for p in expected if "/r.txt" not in p}
    runner = CommandRunner(paths, policy, None, None, None, lambda: None)
    assert runner._write_area_state() == writable


@pytest.mark.parametrize("kind", ["junction", "symlink"])
def test_walk_and_grep_protect_installation_and_case_variants(tmp_path, kind):
    paths, policy = make_paths(tmp_path, broad=True)
    install = paths.install_dir
    for name in ("host.yaml", "vendor/rg.exe", "src/main.py", ".venv/python.exe", "scripts/start.py"):
        p = install / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("private needle\n", encoding="utf-8")
        assert paths.level_of(p) is None
        if os.name == "nt":
            assert paths.level_of(Path(str(p).upper())) is None
    (install / "allowed").mkdir()
    (install / "allowed/ok.txt").write_text("public needle\n", encoding="utf-8")
    link_to(paths.root / "install-alias", install, kind)
    result = [paths.display(p) for p, _ in paths.walk(tmp_path, "read")]
    assert all("private" not in p and not p.endswith(("host.yaml", "rg.exe", "main.py", "python.exe", "start.py"))
               for p in result)
    assert any(p.endswith("allowed/ok.txt") for p in result)
    hits = SearchTools(paths, policy, RG).grep("private")["matches"]
    assert hits == []


def test_grep_uses_folder_exclusions_without_python_walk(tmp_path, monkeypatch):
    paths, policy = make_paths(tmp_path, broad=True)
    hidden = paths.root / "hidden"
    hidden.mkdir()
    (hidden / "secret.txt").write_text("needle\n", encoding="utf-8")
    (paths.root / "ok.txt").write_text("needle\n", encoding="utf-8")
    raw = policy.as_dict()
    raw["folders"].append({"path": str(hidden), "level": "hidden"})
    policy.path.write_text(yaml.safe_dump(raw), encoding="utf-8", newline="\n")

    def no_walk(*args, **kwargs):
        pytest.fail("rg must enumerate the folders itself")

    monkeypatch.setattr(paths, "walk", no_walk)
    commands = []
    popen = subprocess.Popen

    def capture(cmd, **kwargs):
        commands.append(cmd)
        return popen(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", capture)
    hits = SearchTools(paths, policy, RG).grep("needle")["matches"]
    assert [m["path"] for m in hits] == ["ok.txt"]
    assert commands and all(cmd[-2:] == ["--", "."] for cmd in commands)
    assert any("hidden" in arg for cmd in commands for arg in cmd if arg.startswith("!"))
    assert any("data" in arg for cmd in commands for arg in cmd if arg.startswith("!"))


@pytest.mark.parametrize("file_glob", ["*.txt", "**/*.txt"])
def test_grep_falls_back_to_walk_when_rg_is_missing(tmp_path, file_glob):
    paths, policy = make_paths(tmp_path)
    (paths.root / "ok.txt").write_text("before\nNeedle\nafter\n", encoding="utf-8", newline="\n")
    (paths.root / "sjis.txt").write_bytes("日本語 needle\n".encode("cp932"))
    search = SearchTools(paths, policy, tmp_path / "missing-rg.exe")
    result = search.grep("needle", glob=file_glob, ignore_case=True, context=1)
    assert {m["path"] for m in result["matches"]} == {"ok.txt", "sjis.txt"}
    assert sum("context" in m for m in result["matches"]) == 2
    assert not result["truncated"]
    assert search.grep("日本語")["matches"][0]["path"] == "sjis.txt"
    with pytest.raises(ToolError, match="正規表現"):
        search.grep("(")


def test_subtree_write_check_resolves_only_links(tmp_path, monkeypatch):
    paths, _ = make_paths(tmp_path)
    for i in range(20):
        (paths.root / f"f{i}.txt").write_bytes(b"x")
    calls = []
    original = Path.resolve

    def resolve(self, *args, **kwargs):
        calls.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    paths.check_subtree_writable(paths.root, "削除")
    assert len(calls) <= 1, calls


def test_subtree_write_check_rejects_empty_readonly_directory(tmp_path):
    paths, policy = make_paths(tmp_path)
    (paths.root / "empty").mkdir()
    raw = policy.as_dict()
    raw["folders"].append({"path": str(paths.root / "empty"), "level": "read"})
    policy.save(raw)
    with pytest.raises(ToolError, match="書き込みが許可されていない場所"):
        paths.check_subtree_writable(paths.root, "削除")


@pytest.mark.parametrize("levels", [("hidden", "write"), ("read", "write"), ("write", "read")])
def test_walk_preserves_duplicate_folder_precedence(tmp_path, levels):
    paths, policy = make_paths(tmp_path)
    directory = paths.root / "nested"
    directory.mkdir()
    file = directory / "file.txt"
    file.write_bytes(b"needle\n")
    raw = policy.as_dict()
    raw["folders"] += [{"path": str(directory), "level": level} for level in levels]
    policy.path.write_text(yaml.safe_dump(raw), encoding="utf-8", newline="\n")
    for need in ("read", "write"):
        expected = [file] if paths.discoverable(file) and (need == "read" or paths.level_of(file) == "write") else []
        assert [p for p, _ in paths.walk(paths.root, need)] == expected


def test_walk_applies_v2_rule_on_the_main_folder_itself(tmp_path):
    paths, policy = make_paths(tmp_path)
    (paths.root / "file.txt").write_bytes(b"needle\n")
    raw = policy.as_dict()
    raw["advanced_rules"] = [{"path": "", "level": "hidden"}]
    policy.save(raw)
    assert not paths.discoverable(paths.root)
    assert list(paths.walk(paths.root, "read")) == []
