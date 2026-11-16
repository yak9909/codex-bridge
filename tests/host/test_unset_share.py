"""First-run setup uses only pytest's temporary folders, including legacy configs."""

import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import yaml

from codex_bridge.host import main, setup_cli
from codex_bridge.host.app import build_host, create_local_app, create_public_app
from codex_bridge.host.config import effective_share_root, load_host_config
from codex_bridge.host.paths import ToolError
from conftest import RG_PATH
from mcp_helpers import running

MESSAGE = "共有フォルダがまだ設定されていません。管理画面で選んでください。"
H = {"X-Codex-Bridge": "1"}
MCP_H = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def config_file(tmp_path, share="C:/path/to/your/folder"):
    cfg = tmp_path / "host.yaml"
    cfg.write_text(yaml.safe_dump({"share_root": share, "data_dir": str(tmp_path / "data"),
                                   "rg_path": str(RG_PATH)}), encoding="utf-8", newline="\n")
    return cfg


def legacy_host(tmp_path, version=2):
    cfg = config_file(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    raw = {"main": "C:/path/to/your/folder", "folders": [
        {"path": "C:/path/to/your/folder", "level": "write"}]} if version == 2 else {"default": "write"}
    (data / "policy.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8", newline="\n")
    return build_host(load_host_config(cfg), config_path=cfg)


@pytest.mark.parametrize("value", [None, "", "  ", "C:/path/to/your/folder", "missing", "file.txt"])
def test_config_treats_missing_empty_sample_and_nonfolder_as_unset(tmp_path, value):
    (tmp_path / "file.txt").write_text("file", encoding="utf-8")
    cfg = config_file(tmp_path, value)
    assert load_host_config(cfg).share_root is None
    cfg.write_text("data_dir: data\n", encoding="utf-8")
    assert load_host_config(cfg).share_root is None


def test_ensure_config_does_not_invent_a_shared_folder(tmp_path):
    cfg = tmp_path / "host.yaml"
    assert main.ensure_config(cfg)
    raw = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert not raw.get("share_root")
    policy = yaml.safe_load((tmp_path / "data/policy.yaml").read_text(encoding="utf-8"))
    assert not policy.get("main") and policy["folders"] == []
    assert "C:/path/to/your/folder" not in cfg.read_text(encoding="utf-8")


@pytest.mark.parametrize("share", [None, "C:/path/to/your/folder", "missing"])
def test_unset_start_never_adds_the_sample_folder(tmp_path, share):
    candidate = Path(share) if share is not None else None
    if share == "missing":
        candidate = tmp_path / share
    raw = yaml.safe_load(main.sample_policy(candidate))
    assert raw["main"] is None and raw["folders"] == []

    cfg = config_file(tmp_path, share)
    main.ensure_config(cfg)
    host = build_host(load_host_config(cfg), config_path=cfg)
    assert host.paths.root is None
    assert host.policy.as_dict()["folders"] == []


@pytest.mark.parametrize("main_kind", ["sample", "missing", "absent", "existing"])
async def test_choose_folder_drops_only_sample_when_main_is_unset(tmp_path, main_kind):
    host = legacy_host(tmp_path)
    chosen = tmp_path / "chosen"
    existing = tmp_path / "existing"
    chosen.mkdir()
    existing.mkdir()
    sample = "C:/path/to/your/folder"
    missing = (tmp_path / "unplugged-drive").as_posix()
    retained = [
        {"path": missing, "level": "read"},
        {"path": sample + "-backup", "level": "read"},
        {"path": sample + "/private", "level": "hidden"},
        {"path": existing.as_posix(), "level": "read"},
    ]
    raw = {"main": {"sample": sample, "missing": missing, "absent": None,
                    "existing": existing.as_posix()}[main_kind],
           "folders": [{"path": sample, "level": "write"}, *retained],
           "exec": {"mode": "ask"}, "paused": True}
    host.policy.path.write_text(yaml.safe_dump(raw), encoding="utf-8", newline="\n")
    assert (host.paths.root is None) == (main_kind != "existing")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_local_app(host)),
                                 base_url="http://127.0.0.1") as client:
        response = await client.put("/admin/api/share-root", json={"path": str(chosen)}, headers=H)
        assert response.status_code == 200, response.text
        rows = (await client.get("/admin/api/folders")).json()["folders"]
    saved = yaml.safe_load(host.policy.path.read_text(encoding="utf-8"))
    expected = ([{"path": sample, "level": "write"}] if main_kind == "existing" else [])
    expected += [*retained, {"path": chosen.as_posix(), "level": "write"}]
    assert saved["folders"] == expected
    assert saved["main"] == chosen.as_posix()
    assert saved["paused"] is True and saved["exec"]["mode"] == "ask"
    assert any(f["path"] == sample for f in rows) == (main_kind == "existing")


@pytest.mark.parametrize("ending", ["\n", "\r\n"])
@pytest.mark.parametrize("share_line", [
    'share_root: "C:/path/to/your/folder"  # 選択する場所',
    "share_root:  # 選択する場所",
    None,
])
@pytest.mark.parametrize("final_newline", [False, True])
async def test_share_change_preserves_host_yaml_bytes(tmp_path, ending, share_line, final_newline):
    host = legacy_host(tmp_path)
    chosen = tmp_path / "選んだフォルダ #1"
    chosen.mkdir()
    lines = ["# codex-bridge host settings", "# 共有フォルダの説明"]
    if share_line is not None:
        lines.append(share_line)
    lines += ["", "data_dir: 'data' # 記録", 'public_bind: "127.0.0.1:8765"',
              "local_port: 8770", "# 最後のコメント", ""]
    original = ending.join(lines) + (ending if final_newline else "")
    # Exercise the no-final-newline case without discarding existing blank lines.
    if not final_newline:
        original = original.rstrip("\r\n")
    host.config_path.write_bytes(original.encode("utf-8"))
    value = json.dumps(chosen.as_posix(), ensure_ascii=False)
    if share_line is not None:
        expected = original.replace(share_line, f"share_root: {value}  # 選択する場所")
    else:
        expected = original + ("" if original.endswith(ending) else ending) + f"share_root: {value}" + ending
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_local_app(host)),
                                 base_url="http://127.0.0.1") as client:
        response = await client.put("/admin/api/share-root", json={"path": str(chosen)}, headers=H)
        assert response.status_code == 200, response.text
    assert host.config_path.read_bytes() == expected.encode("utf-8")
    assert load_host_config(host.config_path).share_root == chosen.resolve()


@pytest.mark.parametrize("version", [1, 2])
async def test_legacy_sample_can_serve_without_shared_folder(tmp_path, monkeypatch, capsys, version):
    host = legacy_host(tmp_path, version)
    assert host.paths.root is None
    assert host.paths.roots() == []
    seen = []

    class Server:
        def __init__(self, config):
            self.config = config

        async def serve(self):
            seen.append(self.config)

    monkeypatch.setattr(main.uvicorn, "Server", Server)
    await main.serve(host.config_path)
    assert len(seen) == 2
    assert "共有フォルダ: 未設定（管理画面で選んでください）" in capsys.readouterr().out


@pytest.mark.parametrize("linked", [False, True])
async def test_serve_still_rejects_data_inside_configured_share(tmp_path, linked):
    share = tmp_path / "share"
    share.mkdir()
    chosen = share
    if linked:
        chosen = tmp_path / "alias"
        try:
            chosen.symlink_to(share, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"symlinks unavailable: {exc}")
    cfg = config_file(tmp_path, str(chosen))
    raw = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    raw["data_dir"] = str(share / "data")
    cfg.write_text(yaml.safe_dump(raw), encoding="utf-8", newline="\n")
    with pytest.raises(SystemExit, match="data_dir は share_root の外に置いてください"):
        await main.serve(cfg)


async def rpc(client, prefix, name, args, headers=None):
    response = await client.post(prefix + "/mcp", headers={**MCP_H, **(headers or {})}, json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": args}})
    assert response.status_code == 200, response.text
    return response.json()["result"]


CODEX_CALLS = [
    ("list_dir", {}), ("list_dir", {"path": "."}), ("read_file", {"path": "hello.txt"}),
    ("stat", {"path": "."}), ("find_files", {"pattern": "**/*"}), ("grep", {"pattern": "hello"}),
    ("write_file", {"path": "new.txt", "content": "hello"}),
    ("edit_file", {"path": "hello.txt", "old": "hello", "new": "world"}),
    ("apply_patch", {"patch": "*** Begin Patch\n*** Add File: new.txt\n+hello\n*** End Patch"}),
    ("move_path", {"src": "hello.txt", "dst": "new.txt"}), ("delete_path", {"path": "hello.txt"}),
    ("run_command", {"command": "echo hello"}), ("get_command_result", {"job_id": "missing"}),
]


@pytest.mark.parametrize("version", [1, 2])
async def test_unset_codex_tools_and_task_start_return_japanese(tmp_path, version):
    host = legacy_host(tmp_path, version)
    token = host.tokens.issue("friend")
    async with running(host.codex_ep):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_public_app(host)),
                                     base_url="http://127.0.0.1:8770") as client:
            for name, args in CODEX_CALLS:
                result = await rpc(client, "/codex", name, args, {"Authorization": "Bearer " + token})
                assert result.get("isError") and MESSAGE in result["content"][0]["text"], (name, result)
    with pytest.raises(ToolError, match=MESSAGE):
        await host.mgr.start("work")
    assert not host.mgr.tasks


async def test_claude_status_guide_and_admin_handle_unset(tmp_path):
    host = legacy_host(tmp_path)
    async with running(host.claude_ep):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_local_app(host)),
                                     base_url="http://127.0.0.1:8770") as client:
            overview = (await client.get("/admin/api/overview")).json()
            assert overview["share_root"] is None and overview["share_root_message"] == MESSAGE
            assert (await client.get("/admin/api/folders")).status_code == 200
            response = await client.post("/admin/api/tasks", json={"prompt": "work"}, headers=H)
            assert response.status_code == 409 and MESSAGE in response.json()["detail"]
            for name in ("get_overview", "get_guide", "get_status"):
                result = await rpc(client, "/claude", name, {})
                assert not result.get("isError"), result
                assert MESSAGE in json.dumps(result, ensure_ascii=False)
                assert '"None"' not in result["content"][0]["text"]
            result = await rpc(client, "/claude", "send_task", {"prompt": "work"})
            assert result.get("isError") and MESSAGE in result["content"][0]["text"]


@pytest.mark.parametrize("version", [1, 2])
async def test_pick_then_share_works_immediately_and_survives_restart(tmp_path, monkeypatch, version):
    host = legacy_host(tmp_path, version)
    chosen = tmp_path / "chosen"
    chosen.mkdir()
    (chosen / "hello.txt").write_text("hello\n", encoding="utf-8", newline="\n")
    monkeypatch.setattr("codex_bridge.host.web.app.pick_folder_dialog", lambda initial: str(chosen))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_local_app(host)),
                                 base_url="http://127.0.0.1") as client:
        picked = (await client.post("/admin/api/pick-folder", headers=H)).json()["path"]
        response = await client.put("/admin/api/share-root", json={"path": picked}, headers=H)
        assert response.status_code == 200, response.text
        assert load_host_config(host.config_path).share_root == chosen.resolve()
        assert host.config.share_root == chosen.resolve()
        assert host.services.read.read_file("hello.txt")["text"] == "hello\n"
        host.services.write.write_file("new.txt", "new")
        assert (chosen / "new.txt").read_text(encoding="utf-8") == "new"
        assert host.services.read.find_files("*.txt") == ["hello.txt", "new.txt"]
        assert host.services.search.grep("hello")["matches"]
        overview = (await client.get("/admin/api/overview")).json()
        assert overview["share_root"] == str(chosen.resolve()) and not overview["share_root_message"]
        assert any(f["path"] == chosen.as_posix() for f in overview["folders"])
    restarted = build_host(load_host_config(host.config_path), config_path=host.config_path)
    assert restarted.paths.root == chosen.resolve()
    assert restarted.services.read.read_file("hello.txt")["text"] == "hello\n"


async def test_selection_guards_data_and_install_files_even_for_existing_rule(tmp_path):
    host = legacy_host(tmp_path)
    original = host.config_path.read_bytes()
    host.policy.path.write_text(yaml.safe_dump({"main": str(tmp_path), "folders": [
        {"path": str(tmp_path), "level": "write"}]}), encoding="utf-8")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_local_app(host)),
                                 base_url="http://127.0.0.1") as client:
        for candidate in (tmp_path, host.config.data_dir):
            response = await client.put("/admin/api/share-root", json={"path": str(candidate)}, headers=H)
            assert response.status_code == 400 and "データフォルダ" in response.json()["detail"]
    assert host.config_path.read_bytes() == original
    assert not host.paths.visible(host.config_path)
    assert not host.paths.visible(host.config.data_dir / "policy.yaml")


async def test_empty_policy_has_no_fallback_and_can_be_configured_again(tmp_path):
    cfg = config_file(tmp_path, None)
    main.ensure_config(cfg)
    host = build_host(load_host_config(cfg), config_path=cfg)
    chosen = tmp_path / "chosen"
    chosen.mkdir()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_local_app(host)),
                                 base_url="http://127.0.0.1:8770") as client:
        for _ in range(2):
            assert host.paths.root is None and host.paths.roots() == []
            assert (await client.put("/admin/api/share-root", json={"path": str(chosen)}, headers=H)).status_code == 200
            assert (await client.post("/admin/api/folders/remove", json={"path": str(chosen)}, headers=H)).status_code == 200
        assert (await client.get("/admin/api/overview")).json()["share_root"] is None
        with pytest.raises(ToolError, match=MESSAGE):
            host.services.read.list_dir()


async def test_picker_cancel_keeps_setup_and_runs_outside_event_loop(tmp_path, monkeypatch):
    import threading
    host = legacy_host(tmp_path)
    loop_thread = threading.get_ident()
    called = []
    def pick(initial):
        called.append((initial, threading.get_ident()))
        return None
    monkeypatch.setattr("codex_bridge.host.web.app.pick_folder_dialog", pick)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_local_app(host)),
                                 base_url="http://127.0.0.1:8770") as client:
        response = await client.post("/admin/api/pick-folder", headers=H)
        assert response.status_code == 200 and response.json()["path"] is None
        assert host.paths.root is None
    assert called[0][0] is None and called[0][1] != loop_thread


async def test_selection_rejects_separate_install_and_its_private_folders(tmp_path):
    host = legacy_host(tmp_path)
    install = tmp_path / "install"
    (install / "vendor").mkdir(parents=True)
    host.config = replace(host.config, install_dir=install)
    from codex_bridge.host.paths import SafePaths
    host.paths = SafePaths(None, host.policy, install_dir=install)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_local_app(host)),
                                 base_url="http://127.0.0.1:8770") as client:
        for path in (install, install / "vendor"):
            response = await client.put("/admin/api/share-root", json={"path": str(path)}, headers=H)
            assert response.status_code == 400 and "インストール先" in response.json()["detail"]
        assert host.paths.root is None


def setup_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "vendor").mkdir(exist_ok=True)
    (tmp_path / "vendor/rg.exe").touch()
    monkeypatch.setattr(setup_cli, "_wait_tailscale_ip", lambda: "100.64.0.1")
    monkeypatch.setattr(setup_cli.st, "firewall_rule_exists", lambda: True)
    monkeypatch.setattr(setup_cli.st, "add_firewall_rule_elevated", lambda: pytest.fail("unexpected firewall change"))
    monkeypatch.setattr(setup_cli.subprocess, "run", lambda *a, **kw: pytest.fail("unexpected subprocess"))
    monkeypatch.setattr(setup_cli.subprocess, "Popen", lambda *a, **kw: pytest.fail("unexpected process"))
    monkeypatch.setattr(setup_cli.webbrowser, "open", lambda *a: pytest.fail("unexpected browser"))
    import tkinter
    monkeypatch.setattr(tkinter, "Tk", lambda *a, **kw: pytest.fail("no folder dialog in setup"))
    questions = []
    monkeypatch.setattr(setup_cli, "_yes", lambda q, **kw: questions.append(q) or False)
    return questions


def test_setup_writes_unset_share_root(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "host.yaml"
    questions = setup_env(tmp_path, monkeypatch)
    setup_cli.run_setup(cfg)
    assert "share_root: null" in cfg.read_text(encoding="utf-8")
    assert effective_share_root(load_host_config(cfg)) is None
    assert "管理画面で選びます" in capsys.readouterr().out
    assert not any("フォルダ" in q for q in questions)


def test_setup_with_existing_sample_install_leaves_the_host_unset(tmp_path, monkeypatch):
    cfg = sample_install(tmp_path)
    policy = tmp_path / "data/policy.yaml"
    before = policy.read_bytes()
    setup_env(tmp_path, monkeypatch)
    setup_cli.run_setup(cfg)
    assert effective_share_root(load_host_config(cfg)) is None
    assert policy.read_bytes() == before
    assert 'public_bind: "100.64.0.1:8765"' in cfg.read_text(encoding="utf-8")


def test_setup_keeps_existing_share_root(tmp_path, monkeypatch):
    share = tmp_path / "share"
    share.mkdir()
    cfg = config_file(tmp_path, share.as_posix())
    cfg.write_text(cfg.read_text(encoding="utf-8") + 'public_bind: "100.1.1.1:8765"\n',
                   encoding="utf-8", newline="\n")
    main.ensure_config(cfg)
    policy = tmp_path / "data/policy.yaml"
    policy_before = policy.read_bytes()
    line = next(l for l in cfg.read_text(encoding="utf-8").splitlines() if l.startswith("share_root:"))
    setup_env(tmp_path, monkeypatch)
    setup_cli.run_setup(cfg)
    text = cfg.read_text(encoding="utf-8")
    assert line in text.splitlines()
    assert 'public_bind: "100.64.0.1:8765"' in text
    assert effective_share_root(load_host_config(cfg)) == share.resolve()
    assert policy.read_bytes() == policy_before


def test_setup_does_not_build_friend_kit(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "host.yaml"
    questions = setup_env(tmp_path, monkeypatch)
    setup_cli.run_setup(cfg)
    assert not (tmp_path / "friend-kit").exists()
    assert "friend-kit" not in capsys.readouterr().out + "".join(questions)
    assert not hasattr(setup_cli, "build_friend_kit")


def test_start_explains_admin_setup_and_still_serves(tmp_path, monkeypatch, capsys):
    cfg = config_file(tmp_path)
    monkeypatch.setattr(setup_cli, "_admin_alive", lambda port: False)
    monkeypatch.setattr(setup_cli, "_wait_tailscale_ip", lambda: "127.0.0.1")
    monkeypatch.setattr(setup_cli.threading.Thread, "start", lambda self: None)
    called = []
    async def serve(path):
        called.append(path)
    monkeypatch.setattr(main, "serve", serve)
    setup_cli.run_start(cfg)
    assert called == [cfg]
    assert MESSAGE in capsys.readouterr().out


# ---- final-review fixes (I-1, M-1..M-4, 11-a) ---------------------------------
SAMPLE = "C:/path/to/your/folder"


def shared_host(tmp_path, share=None):
    """A host whose share root is a real folder (host.yaml and policy.yaml agree)."""
    share = share or tmp_path / "share"
    share.mkdir(exist_ok=True)
    cfg = config_file(tmp_path, share.as_posix())
    main.ensure_config(cfg)
    return build_host(load_host_config(cfg), config_path=cfg)


def sample_install(tmp_path, policy_main=SAMPLE):
    """host.yaml and policy.yaml exactly like the shipped samples (both still unset)."""
    cfg = config_file(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    (data / "policy.yaml").write_text(yaml.safe_dump({
        "main": policy_main, "folders": [{"path": SAMPLE, "level": "read"}],
        "exec": {"mode": "allowlist", "allow": ["python scripts/**/*.py *"]}}), encoding="utf-8", newline="\n")
    return cfg


def test_effective_share_root_follows_the_policy_main(tmp_path):
    from codex_bridge.host.config import effective_share_root
    real = tmp_path / "real"
    real.mkdir()
    cfg = sample_install(tmp_path, policy_main=real.as_posix())
    assert load_host_config(cfg).share_root is None            # host.yaml alone says "unset"
    assert effective_share_root(load_host_config(cfg)) == real.resolve()
    policy = tmp_path / "data/policy.yaml"
    before = policy.read_bytes()
    policy.write_text(yaml.safe_dump({"main": None, "folders": []}), encoding="utf-8")
    assert effective_share_root(load_host_config(cfg)) is None
    policy.write_bytes(before)


def test_effective_share_root_leaves_a_v1_policy_alone(tmp_path):
    from codex_bridge.host.config import effective_share_root
    real = tmp_path / "real"
    real.mkdir()
    cfg = config_file(tmp_path, real.as_posix())
    (tmp_path / "data").mkdir()
    v1 = tmp_path / "data/policy.yaml"
    v1.write_text("default: read\n", encoding="utf-8")
    assert effective_share_root(load_host_config(cfg)) == real.resolve()
    assert v1.read_text(encoding="utf-8") == "default: read\n"
    assert not (tmp_path / "data/policy.v1.yaml").exists()


def test_start_and_setup_trust_the_policy_main_not_a_sample_host_yaml(tmp_path, monkeypatch, capsys):
    real = tmp_path / "real"
    real.mkdir()
    cfg = sample_install(tmp_path, policy_main=real.as_posix())
    monkeypatch.setattr(setup_cli, "_admin_alive", lambda port: False)
    monkeypatch.setattr(setup_cli, "_wait_tailscale_ip", lambda: "127.0.0.1")
    monkeypatch.setattr(setup_cli.threading.Thread, "start", lambda self: None)

    async def serve(path):
        pass
    monkeypatch.setattr(main, "serve", serve)
    setup_cli.run_start(cfg)
    assert MESSAGE not in capsys.readouterr().out
    policy = tmp_path / "data/policy.yaml"
    before = policy.read_bytes()
    setup_env(tmp_path, monkeypatch)
    setup_cli.run_setup(cfg)
    assert policy.read_bytes() == before
    assert effective_share_root(load_host_config(cfg)) == real.resolve()


async def test_removing_the_main_folder_keeps_host_yaml_in_step(tmp_path):
    host = shared_host(tmp_path)
    first = tmp_path / "share"
    second = tmp_path / "second"
    second.mkdir()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_local_app(host)),
                                 base_url="http://127.0.0.1") as client:
        assert (await client.post("/admin/api/folders/add", json={"path": str(second), "level": "read"},
                                  headers=H)).status_code == 200
        assert (await client.post("/admin/api/folders/remove", json={"path": str(first)}, headers=H)).status_code == 200
        assert host.paths.root == second.resolve()
        assert load_host_config(host.config_path).share_root == second.resolve()
        assert host.config.share_root == second.resolve()
        assert (await client.post("/admin/api/folders/remove", json={"path": str(second)}, headers=H)).status_code == 200
    assert host.paths.root is None
    assert yaml.safe_load(host.config_path.read_text(encoding="utf-8"))["share_root"] is None
    from codex_bridge.host.config import effective_share_root
    assert effective_share_root(load_host_config(host.config_path)) is None


def test_start_warns_when_host_yaml_keeps_a_folder_the_policy_no_longer_has(tmp_path, monkeypatch, capsys):
    stale = tmp_path / "stale"
    stale.mkdir()
    cfg = config_file(tmp_path, stale.as_posix())
    (tmp_path / "data").mkdir()
    (tmp_path / "data/policy.yaml").write_text("main: null\nfolders: []\n", encoding="utf-8")
    monkeypatch.setattr(setup_cli, "_admin_alive", lambda port: False)
    monkeypatch.setattr(setup_cli, "_wait_tailscale_ip", lambda: "127.0.0.1")
    monkeypatch.setattr(setup_cli.threading.Thread, "start", lambda self: None)

    async def serve(path):
        pass
    monkeypatch.setattr(main, "serve", serve)
    setup_cli.run_start(cfg)
    assert MESSAGE in capsys.readouterr().out


@pytest.mark.parametrize("route", ["main", "share-root"])
async def test_host_yaml_write_failure_is_a_warning_not_a_500(tmp_path, monkeypatch, route):
    host = shared_host(tmp_path)
    other = tmp_path / "other"
    other.mkdir()

    def fail(path, root):
        raise PermissionError("locked by another program")
    monkeypatch.setattr("codex_bridge.host.share._save_share_root", fail)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_local_app(host)),
                                 base_url="http://127.0.0.1") as client:
        if route == "main":
            assert (await client.post("/admin/api/folders/add", json={"path": str(other), "level": "read"},
                                      headers=H)).status_code == 200
            response = await client.post("/admin/api/folders/main", json={"path": str(other)}, headers=H)
        else:
            response = await client.put("/admin/api/share-root", json={"path": str(other)}, headers=H)
        assert response.status_code == 200, response.text
        assert "host.yaml" in response.json()["warning"]
    assert host.paths.root == other.resolve()
    assert any("host_yaml" in line for line in host.audit.path.read_text(encoding="utf-8").splitlines())


async def test_share_root_warns_when_folder_is_not_writable(tmp_path, monkeypatch):
    host = shared_host(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setattr("codex_bridge.host.share.folder_is_writable", lambda folder: False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_local_app(host)),
                                 base_url="http://127.0.0.1") as client:
        response = await client.put("/admin/api/share-root", json={"path": str(other)}, headers=H)
    assert response.status_code == 200
    assert "書き込めません" in response.json()["warning"]


def test_main_folder_vanishing_midway_gives_the_unset_message(tmp_path, monkeypatch):
    from codex_bridge.host.paths import SafePaths
    host = shared_host(tmp_path)
    share = (tmp_path / "share").resolve()
    seq = iter([share])
    monkeypatch.setattr(SafePaths, "root", property(lambda self: next(seq, None)))
    with pytest.raises(ToolError, match=MESSAGE):
        host.paths.resolve("hello.txt", "read")
    monkeypatch.setattr(SafePaths, "root", property(lambda self: None))
    assert isinstance(host.paths.search_exclusions(share), list)


async def test_command_start_with_vanished_main_is_denied_not_crashed(tmp_path, monkeypatch):
    from codex_bridge.host.paths import SafePaths
    host = shared_host(tmp_path)
    monkeypatch.setattr(SafePaths, "root", property(lambda self: None))
    with pytest.raises(ToolError, match=MESSAGE):
        await host.services.runner._execute(object(), tmp_path, 5)


async def test_stale_host_yaml_warning_does_not_repeat_on_the_next_add(tmp_path, monkeypatch):
    host = shared_host(tmp_path)
    other, third = tmp_path / "other", tmp_path / "third"
    other.mkdir()
    third.mkdir()

    def fail(path, root):
        raise PermissionError("read-only")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_local_app(host)),
                                 base_url="http://127.0.0.1") as client:
        assert (await client.post("/admin/api/folders/add", json={"path": str(other), "level": "read"},
                                  headers=H)).status_code == 200
        with monkeypatch.context() as m:
            m.setattr("codex_bridge.host.share._save_share_root", fail)
            response = await client.post("/admin/api/folders/main", json={"path": str(other)}, headers=H)
        assert "host.yaml" in response.json()["warning"]
        response = await client.post("/admin/api/folders/add", json={"path": str(third), "level": "read"},
                                     headers=H)
        assert response.status_code == 200
        assert "warning" not in response.json()
    assert host.share_warning is None


def test_main_button_and_tool_path_drop_sample_row_only_when_unset(tmp_path):
    from codex_bridge.host.share import add_shared_folder, set_main_shared_folder
    host = legacy_host(tmp_path)  # main is the sample row: unset
    chosen = tmp_path / "chosen"
    chosen.mkdir()
    add_shared_folder(host, str(chosen), "read")
    set_main_shared_folder(host, str(chosen))
    saved = yaml.safe_load(host.policy.path.read_text(encoding="utf-8"))
    assert saved["main"] == chosen.as_posix()
    assert saved["folders"] == [{"path": chosen.as_posix(), "level": "read"}]


def test_main_button_keeps_sample_row_when_a_real_main_already_exists(tmp_path):
    from codex_bridge.host.share import add_shared_folder, set_main_shared_folder
    host = legacy_host(tmp_path)
    real, chosen = tmp_path / "real", tmp_path / "chosen"
    real.mkdir()
    chosen.mkdir()
    raw = yaml.safe_load(host.policy.path.read_text(encoding="utf-8"))
    raw["main"] = real.as_posix()
    raw["folders"].append({"path": real.as_posix(), "level": "write"})
    host.policy.path.write_text(yaml.safe_dump(raw), encoding="utf-8", newline="\n")
    assert host.paths.root == real.resolve()
    add_shared_folder(host, str(chosen), "read")
    set_main_shared_folder(host, str(chosen))
    paths = [f["path"] for f in yaml.safe_load(host.policy.path.read_text(encoding="utf-8"))["folders"]]
    assert "C:/path/to/your/folder" in paths and real.as_posix() in paths
