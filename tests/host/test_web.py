import time
import json
import csv
import os
from pathlib import Path
import shutil
import subprocess
import sys
from uuid import UUID

import httpx
import pytest
import yaml

from codex_bridge.common import protocol
from codex_bridge.common.invite import parse_invite
from codex_bridge.host.app import build_host, create_local_app, create_public_app
from codex_bridge.host.config import load_host_config
from codex_bridge.host.main import ensure_config
from mcp_helpers import running

H = {"X-Codex-Bridge": "1"}
VALID_OFF = "default: read\nrules: []\nexec: {mode: \"off\"}\n"


def make_host(tmp_path):
    share = tmp_path / "share"
    (share / "raw").mkdir(parents=True)
    cfg = tmp_path / "host.yaml"
    cfg.write_text(yaml.safe_dump({"share_root": str(share), "data_dir": str(tmp_path / "data"),
                                   "public_bind": "100.64.0.1:8765", "local_port": 8770,
                                   "rg_path": str(tmp_path / "rg.exe")}), encoding="utf-8")
    ensure_config(cfg)
    return build_host(load_host_config(cfg), config_path=cfg)


@pytest.mark.parametrize("endpoint", ["policy", "policy-form"])
async def test_final_review_admin_refuses_data_parent(admin, host, endpoint):
    form = {"folders": [{"path": host.config.data_dir.parent.as_posix(), "level": "write"}]}
    kwargs = {"content": yaml.safe_dump(form)} if endpoint == "policy" else {"json": form}
    r = await admin.put(f"/admin/api/{endpoint}", headers=H, **kwargs)
    assert r.status_code == 400 and "データフォルダ" in r.text


def test_final_review_install_files_are_invisible_under_broad_policy(host):
    root = host.config_path.parent
    host.policy.path.write_text(yaml.safe_dump({"folders": [{"path": root.as_posix(), "level": "write"}]}),
                                encoding="utf-8")
    for path in (host.config_path, root / "vendor" / "rg.exe", root / "start.bat", root / "stop.bat"):
        assert host.paths.level_of(path) is None
        assert not host.paths.visible(path)
    assert host.paths.level_of(host.config.share_root / "raw") == "write"


async def test_final_review_routing_refuses_names_missing_from_tokens(host):
    from codex_bridge.host.paths import ToolError
    sent = []
    async def send(m):
        sent.append(m)
    link = host.mgr.attach("uninvited", send)
    await host.mgr.on_agent_message(link, {"type": "hello"})
    sent.clear()
    for name in (None, "uninvited"):
        with pytest.raises(ToolError, match="接続"):
            await host.mgr.start("private prompt", lender=name)
    assert not sent


def admin_client(host) -> httpx.AsyncClient:
    t = httpx.ASGITransport(app=create_local_app(host), client=("127.0.0.1", 5000))
    return httpx.AsyncClient(transport=t, base_url="http://127.0.0.1:8770")


@pytest.fixture
def host(tmp_path):
    return make_host(tmp_path)


@pytest.fixture
async def admin(host):
    async with admin_client(host) as c:
        yield c


@pytest.fixture
async def public(host):
    t = httpx.ASGITransport(app=create_public_app(host), client=("100.64.0.2", 5000))
    async with httpx.AsyncClient(transport=t, base_url="http://100.64.0.1:8765") as c:
        yield c


async def test_admin_api_not_served_on_public_bind(public):
    assert (await public.get("/admin/api/policy")).status_code == 404
    assert (await public.get("/admin/")).status_code == 404


async def test_admin_page_served(admin):
    r = await admin.get("/admin/")
    assert r.status_code == 200 and "codex-bridge" in r.text


async def test_invite_copy_feedback_is_served(admin):
    r = await admin.get("/admin/static/app.js")
    assert r.status_code == 200
    assert "コピーしました" in r.text


async def test_shared_ui_parts_are_served_next_to_the_admin_page(admin, public):
    for name in ("ui.js", "ui.css", "demo.html"):
        assert (await admin.get(f"/admin/common/{name}")).status_code == 200, name
    js = (await admin.get("/admin/common/ui.js")).text
    for part in ("meter", "stateBadge", "approvals", "taskList", "taskDetail", "choiceCards"):
        assert part in js
    assert "awaiting_lender" in js and "rejected_by_lender" in js
    page = (await admin.get("/admin/")).text
    assert "/admin/common/ui.js" in page and "/admin/common/ui.css" in page
    assert (await public.get("/admin/common/ui.js")).status_code == 404


async def test_mutation_requires_custom_header(admin):
    r = await admin.put("/admin/api/policy", content=VALID_OFF)
    assert r.status_code == 403


async def test_foreign_host_header_rejected(admin):
    r = await admin.get("/admin/api/policy", headers={"Host": "evil.example:8770"})
    assert r.status_code == 403


async def test_policy_put_validates_and_applies(admin, host):
    assert (await admin.put("/admin/api/policy", content="rules: [", headers=H)).status_code == 400
    assert (await admin.put("/admin/api/policy", content=VALID_OFF, headers=H)).status_code == 200
    assert host.policy.current().exec_mode == "off"
    assert "mode" in (await admin.get("/admin/api/policy")).json()["text"]


async def test_approve_via_api(admin, host):
    a = host.approvals.request("command", "python x.py", task_id=None)
    listed = (await admin.get("/admin/api/approvals")).json()
    assert listed[0]["id"] == a.id
    assert (await admin.post(f"/admin/api/approvals/{a.id}", json={"approve": True}, headers=H)).status_code == 200
    assert host.approvals.get(a.id).status == "approved"
    assert (await admin.post(f"/admin/api/approvals/{a.id}", json={"approve": True}, headers=H)).status_code == 409


async def test_invite_issue_and_revoke(admin, host):
    r = await admin.post("/admin/api/invites", json={"name": "friend"}, headers=H)
    inv = parse_invite(r.json()["code"])
    assert inv.url == "http://100.64.0.1:8765" and len(host.tokens.hashes()) == 1
    assert (await admin.delete("/admin/api/invites/friend", headers=H)).status_code == 200
    assert host.tokens.hashes() == set()


async def test_pause_and_thresholds_and_visibility(admin, host):
    await admin.post("/admin/api/pause", json={"paused": True}, headers=H)
    assert host.policy.current().paused is True
    await admin.put("/admin/api/thresholds", json={"five_hour": 70}, headers=H)
    assert (await admin.get("/admin/api/usage")).json()["thresholds"]["five_hour"] == 70
    assert (await admin.put("/admin/api/log-visibility", json={"value": "all"}, headers=H)).status_code == 200
    assert (await admin.put("/admin/api/log-visibility", json={"value": "x"}, headers=H)).status_code == 400


async def test_overview(admin):
    o = (await admin.get("/admin/api/overview")).json()
    assert o["agent"]["connected"] is False and o["paused"] is False and "share_root" in o


def store_bad_folder(host):
    bad = host.config.data_dir.parent
    good = host.config.share_root
    host.policy.path.write_text(yaml.safe_dump({"main": good.as_posix(), "folders": [
        {"path": bad.as_posix(), "level": "write"},
        {"path": good.as_posix(), "level": "read"}], "exec": {"mode": "ask"}}), encoding="utf-8")
    return bad.as_posix()


async def claude_call(admin, name, args):
    response = await admin.post("/claude/mcp", headers={"Accept": "application/json, text/event-stream"},
                                json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": name, "arguments": args}})
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


async def test_overview_reports_policy_problem(admin, host):
    bad = store_bad_folder(host)
    async with running(host.claude_ep):
        for expected in ([{"folder": bad, "reason": "contains_data_dir"}], []):
            assert (await admin.get("/admin/api/overview")).json()["policy_problem"] == expected
            assert (await claude_call(admin, "get_overview", {}))["policy_problem"] == expected
            if expected:
                await claude_call(admin, "remove_shared_folder", {"path": bad})


@pytest.mark.parametrize("operation", ["remove", "hidden", "inherit", "pause", "exec_mode"])
async def test_bad_folder_can_be_removed_or_hidden(admin, host, operation):
    bad = store_bad_folder(host)
    if operation == "remove":
        response = await admin.post("/admin/api/folders/remove", headers=H, json={"path": bad})
    elif operation in ("hidden", "inherit"):
        form = (await admin.get("/admin/api/policy-form")).json()
        form["folders"][0]["level"] = operation
        response = await admin.put("/admin/api/policy-form", headers=H, json=form)
    elif operation == "pause":
        response = await admin.post("/admin/api/pause", headers=H, json={"paused": True})
    else:
        response = await admin.put("/admin/api/exec-mode", headers=H, json={"mode": "off"})
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("writable, level", [(False, "write"), (True, "write"), (False, "read")])
async def test_register_unwritable_folder_warns(admin, host, tmp_path, monkeypatch, writable, level):
    from codex_bridge.host import share as share_ops
    extra = tmp_path / "extra"
    extra.mkdir()
    checked = []

    def probe(folder):
        checked.append(folder)
        return writable

    monkeypatch.setattr(share_ops, "folder_is_writable", probe)
    response = await admin.post("/admin/api/folders/add", headers=H,
                                json={"path": extra.as_posix(), "level": level})
    assert response.status_code == 200, response.text
    expected = "このフォルダは、今のユーザーの権限では書き込めません。Codex が書こうとしても Windows に断られます。"
    assert response.json().get("warning") == (expected if not writable and level == "write" else None)
    assert host.policy.current().level_for_abs(extra / "x") == level
    assert checked == ([extra] if level == "write" else [])


@pytest.mark.parametrize("denied_rights", ["W", "DE,DC"], ids=["write-denied", "delete-denied"])
def test_folder_write_probe_returns_quickly_when_acl_denies_writing(tmp_path, denied_rights):
    icacls = shutil.which("icacls")
    if icacls is None:
        pytest.skip("icacls is unavailable")
    folder = tmp_path / "write-denied"
    folder.mkdir()
    whoami = Path(os.environ["SystemRoot"]) / "System32" / "whoami.exe"
    identity = subprocess.run([str(whoami), "/user", "/fo", "csv", "/nh"],
                              capture_output=True, text=True, errors="replace", check=True, timeout=10)
    sid = next(csv.reader(identity.stdout.splitlines()))[1]
    assert sid.startswith("S-1-")
    marker = folder / "delete-denied.txt"
    try:
        denied = subprocess.run([icacls, str(folder), "/deny", f"*{sid}:(OI)(CI)({denied_rights})"],
                                capture_output=True, text=True, errors="replace", timeout=10)
        assert denied.returncode == 0, denied.stdout + denied.stderr
        # Reproduce the Windows mismatch that makes tempfile retry: os.access says
        # writable, but actually creating a file fails under the current user's ACL.
        assert os.access(folder, os.W_OK)
        if denied_rights == "W":
            with pytest.raises(PermissionError):
                with (folder / "denied.txt").open("xb"):
                    pass
        else:
            marker.write_bytes(b"delete check")
            with pytest.raises(PermissionError):
                marker.unlink()
        # A child process lets the timeout kill the old retry loop without leaving
        # a spinning thread behind, even if an assertion fails before ACL cleanup.
        result = subprocess.run(
            [sys.executable, "-c", "from pathlib import Path; import sys; "
             "from codex_bridge.host.share import folder_is_writable; "
             "print(folder_is_writable(Path(sys.argv[1])))", str(folder)],
            capture_output=True, text=True, check=True, timeout=3)
        if denied_rights == "W":
            assert result.stdout.strip() == "False"
        else:
            # A newly opened DELETE_ON_CLOSE handle can clean itself up even if
            # ordinary deletion is denied. Either result must leave no probe.
            assert result.stdout.strip() in ("True", "False")
    finally:
        restored = subprocess.run([icacls, str(folder), "/remove:d", f"*{sid}"],
                                  capture_output=True, text=True, errors="replace", timeout=10)
        assert restored.returncode == 0, restored.stdout + restored.stderr
        marker.unlink(missing_ok=True)
    # A deny ACE may also prevent listing. Inspect only after restoring the ACL;
    # restoring permissions must reveal no probe left by either failure case.
    assert list(folder.iterdir()) == []


def test_folder_write_probe_creates_and_removes_a_file(tmp_path, monkeypatch):
    from codex_bridge.host.share import folder_is_writable
    opened = []
    real_open = os.open

    def create_once(path, flags, mode):
        opened.append(Path(path))
        assert flags & os.O_CREAT and flags & os.O_EXCL and flags & os.O_WRONLY
        descriptor = real_open(path, flags, mode)
        assert Path(path).is_file()
        return descriptor

    monkeypatch.setattr("codex_bridge.host.share.os.open", create_once)
    assert folder_is_writable(tmp_path)
    assert len(opened) == 1
    assert opened[0].name.startswith(".codex-bridge-probe-")
    assert list(tmp_path.iterdir()) == []
    attempts = []

    def denied(path, *args, **kwargs):
        attempts.append(path)
        raise PermissionError(13, "Access is denied", str(tmp_path))

    monkeypatch.setattr("codex_bridge.host.share.os.open", denied)
    assert not folder_is_writable(tmp_path)
    assert len(attempts) == 1
    assert list(tmp_path.iterdir()) == []


def test_folder_write_probe_does_not_remove_an_existing_file(tmp_path, monkeypatch):
    from codex_bridge.host import share as share_ops
    monkeypatch.setattr(share_ops, "uuid4", lambda: UUID(int=0))
    existing = tmp_path / f".codex-bridge-probe-{UUID(int=0).hex}"
    existing.write_bytes(b"existing data")
    assert not share_ops.folder_is_writable(tmp_path)
    assert existing.read_bytes() == b"existing data"
    assert list(tmp_path.iterdir()) == [existing]


def test_folder_write_probe_cleans_up_after_close_error(tmp_path, monkeypatch):
    from codex_bridge.host.share import folder_is_writable
    real_close = os.close

    def failed_close(descriptor):
        real_close(descriptor)
        raise OSError("close failed")

    monkeypatch.setattr("codex_bridge.host.share.os.close", failed_close)
    assert not folder_is_writable(tmp_path)
    assert list(tmp_path.iterdir()) == []


async def test_policy_problem_and_write_warning_banners(admin):
    page = (await admin.get("/admin/")).text
    assert 'class="banner" id="policy-problem" role="alert" hidden' in page
    assert 'class="banner warn" id="folder-warning" role="status" hidden' in page
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to exercise the admin JavaScript without a browser")
    script = r'''
const fs = require("node:fs"), vm = require("node:vm"), assert = require("node:assert/strict");
const elements = new Map(), calls = [];
let confirmed = false, warning = "";
const element = (id) => {
  if (!elements.has(id)) elements.set(id, {hidden: true, textContent: "", innerHTML: "", addEventListener() {}});
  return elements.get(id);
};
const context = vm.createContext({
  document: {getElementById: element, querySelectorAll: () => []},
  localStorage: {getItem: () => null},
  UI: {esc: (s) => String(s).replace(/[&<>"']/g, (c) => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[c]))},
  confirm: () => confirmed,
  fetch: async (url, options) => { calls.push([url, JSON.parse(options.body)]); return {ok:true, json: async () => ({warning})}; },
  console, setTimeout, clearTimeout,
});
vm.runInContext(fs.readFileSync(process.argv[2], "utf8").split("// ---- start ")[0], context);
vm.runInContext(`
  permForm = {folders: []}; collectForm = () => permForm; permSaved = JSON.stringify(permForm);
  loadPermissions = async () => {}; refreshTop = async () => {};
`, context);
(async () => {
  const problems = [{folder:'C:/work/<img src=x onerror="oops">', reason:'contains_data_dir'},
                    {folder:'D:/other', reason:'contains_data_dir'}];
  context.problems = problems;
  vm.runInContext("renderPolicyProblems(problems)", context);
  const banner = element("policy-problem");
  assert.equal(banner.hidden, false);
  assert.ok(banner.innerHTML.includes("共有フォルダの設定に、codex-bridge 自身のデータ（設定と記録）を含むフォルダがあります: "));
  assert.ok(banner.innerHTML.includes("。このままでは安全のため一部の操作ができないことがあります。"));
  assert.equal((banner.innerHTML.match(/このフォルダを共有から外す/g) || []).length, 2);
  assert.ok(!banner.innerHTML.includes("<img"));
  const click = {target:{closest: () => ({dataset:{removeFolder:problems[1].folder}})}};
  await banner.onclick(click);
  assert.equal(calls.length, 0);
  confirmed = true;
  await banner.onclick(click);
  assert.equal(calls[0][0], "/admin/api/folders/remove");
  assert.equal(calls[0][1].path, problems[1].folder);
  vm.runInContext("renderPolicyProblems([])", context);
  assert.equal(banner.hidden, true);
  assert.equal(banner.innerHTML, "");
  warning = "このフォルダは、今のユーザーの権限では書き込めません。Codex が書こうとしても Windows に断られます。";
  await vm.runInContext('folderOp("add", {path:"D:/other", level:"write"})', context);
  assert.equal(element("folder-warning").hidden, false);
  assert.equal(element("folder-warning").textContent, warning);
  assert.equal(element("fd-msg").textContent, warning + ": D:/other");
  warning = "";
  await vm.runInContext('folderOp("add", {path:"D:/safe", level:"write"})', context);
  assert.equal(element("folder-warning").hidden, true);
  assert.equal(element("fd-msg").textContent, "");
})().catch((e) => { console.error(e); process.exitCode = 1; });
'''
    source = Path(__file__).resolve().parents[2] / "src/codex_bridge/host/web/static/app.js"
    result = subprocess.run([node, "-", str(source)], input=script, text=True, encoding="utf-8",
                            capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_task_detail_requester_label_preserves_host_default_and_escaping():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to exercise the shared task detail without a browser")
    script = r'''
const fs = require("node:fs"), vm = require("node:vm"), assert = require("node:assert/strict");
const element = () => {
  const parts = new Map();
  return {classList: {add() {}, toggle() {}}, innerHTML: "", textContent: "", scrollHeight: 0, clientHeight: 0,
    setAttribute() {}, querySelectorAll: () => [],
    querySelector(selector) {
      if (!parts.has(selector)) parts.set(selector, element());
      return parts.get(selector);
    }};
};
const context = vm.createContext({});
vm.runInContext(fs.readFileSync(process.argv[2], "utf8") + "; globalThis.sharedUI = UI;", context);
const el = element(), task = {task_id:"t", state:"completed", lender:'<img src=x>', prompt:'<script>remote</script>', events:[]};
const sum = () => el.querySelector('[data-td="sum"]').innerHTML;
context.sharedUI.taskDetail(el, task, {showLender:true});
assert.ok(sum().includes("<b>担当</b>"));
assert.ok(sum().includes("&lt;img src=x&gt;"));
context.sharedUI.taskDetail(el, task, {showLender:true, lenderLabel:"依頼主"});
assert.ok(sum().includes("<b>依頼主</b>"));
assert.ok(!sum().includes("<b>担当</b>"));
assert.equal(el.querySelector('[data-td="prompt"]').textContent, task.prompt);
context.sharedUI.taskDetail(el, task, {showLender:true, lenderLabel:"<script>label</script>"});
assert.ok(!sum().includes("<script>"));
'''
    source = Path(__file__).resolve().parents[2] / "src/codex_bridge/common/web/ui.js"
    result = subprocess.run([node, "-", str(source)], input=script, text=True, encoding="utf-8",
                            capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_first_run_writes_sample_configs(tmp_path):
    cfg = tmp_path / "host.yaml"
    assert ensure_config(cfg) is True
    assert cfg.exists()
    data_dir = load_host_config(cfg).data_dir
    assert (data_dir / "policy.yaml").exists()
    assert ensure_config(cfg) is False


async def test_shutdown_endpoint_sets_event(admin, host):
    assert (await admin.post("/admin/api/shutdown")).status_code == 403  # header required
    assert (await admin.post("/admin/api/shutdown", headers=H)).status_code == 200
    assert host.shutdown.is_set()


async def test_policy_form_get_and_put(admin, host):
    form = (await admin.get("/admin/api/policy-form")).json()
    root = host.config.share_root
    assert form["main"] == root.as_posix() and form["folders"] == [{"path": root.as_posix(), "level": "read"}]
    form["folders"].append({"path": (root / "raw").as_posix(), "level": "write"})
    form["exec"]["mode"] = "ask"
    assert (await admin.put("/admin/api/policy-form", json=form, headers=H)).status_code == 200
    assert host.policy.current().level_for("raw/x") == "write" and host.policy.current().exec_mode == "ask"
    form["exec"]["mode"] = "nope"
    assert (await admin.put("/admin/api/policy-form", json=form, headers=H)).status_code == 400


async def test_folders_listing_shows_levels(admin, host):
    (host.config.share_root / "raw" / "sub").mkdir()
    (host.config.share_root / "private").mkdir()
    (host.config.share_root / "file.txt").write_text("x", encoding="utf-8")
    root = host.config.share_root
    form = (await admin.get("/admin/api/policy-form")).json()
    form["folders"].append({"path": (root / "private").as_posix(), "level": "hidden"})
    assert (await admin.put("/admin/api/policy-form", json=form, headers=H)).status_code == 200
    registered = (await admin.get("/admin/api/folders")).json()
    assert [f["path"] for f in registered["folders"]] == [root.as_posix()] and registered["folders"][0]["main"]
    top = {f["name"]: f for f in (await admin.get("/admin/api/folders", params={"path": root.as_posix()})).json()["folders"]}
    assert set(top) == {"raw", "private"}  # folders only; the admin sees hidden ones too
    assert top["private"]["rule"] == "hidden" and top["private"]["effective"] == "hidden"
    assert top["raw"]["rule"] is None and top["raw"]["effective"] == "read" and top["raw"]["has_children"]
    sub = (await admin.get("/admin/api/folders", params={"path": (root / "raw").as_posix()})).json()["folders"]
    assert [f["path"] for f in sub] == [(root / "raw" / "sub").as_posix()]
    assert (await admin.get("/admin/api/folders", params={"path": "raw"})).status_code == 400
    assert (await admin.get("/admin/api/folders", params={"path": "C:/Windows"})).status_code == 400


async def test_change_share_root(admin, host, tmp_path):
    new = tmp_path / "other-share"
    (new / "sub").mkdir(parents=True)
    r = await admin.put("/admin/api/share-root", json={"path": str(new)}, headers=H)
    assert r.status_code == 200, r.text
    assert host.paths.root == new.resolve()
    assert host.policy.current().level_for_abs(new / "sub") == "write"
    assert (await admin.get("/admin/api/overview")).json()["share_root"] == str(new.resolve())


async def test_folder_add_remove_main_api(admin, host, tmp_path):
    extra = tmp_path / "extra"
    extra.mkdir()
    r = await admin.post("/admin/api/folders/add", json={"path": str(extra), "level": "read"}, headers=H)
    assert r.status_code == 200 and extra.as_posix() in {f["path"] for f in r.json()["folders"]}
    assert (await admin.post("/admin/api/folders/main", json={"path": str(extra)}, headers=H)).status_code == 200
    assert host.paths.root == extra.resolve()
    assert (await admin.post("/admin/api/folders/remove", json={"path": str(extra)}, headers=H)).status_code == 200
    assert host.policy.current().level_for_abs(extra / "x") is None
    assert (await admin.post("/admin/api/folders/add", json={"path": "rel/x", "level": "read"}, headers=H)).status_code == 400
    assert (await admin.post("/admin/api/folders/add", json={"path": str(tmp_path), "level": "read"}, headers=H)).status_code == 400


async def test_change_share_root_rejects_bad_paths(admin, host, tmp_path):
    assert (await admin.put("/admin/api/share-root", json={"path": str(tmp_path / "nope")}, headers=H)).status_code == 400
    # the data folder must stay outside the share
    assert (await admin.put("/admin/api/share-root", json={"path": str(tmp_path)}, headers=H)).status_code == 400
    assert (await admin.put("/admin/api/share-root", json={"path": "relative/dir"}, headers=H)).status_code == 400


async def test_cannot_change_share_root_during_a_task(admin, host, tmp_path):
    from codex_bridge.host.tasks import TaskRecord
    host.mgr.tasks["t1"] = TaskRecord(task_id="t1", prompt="p", created=0, state="running")
    new = tmp_path / "x"
    new.mkdir()
    r = await admin.put("/admin/api/share-root", json={"path": str(new)}, headers=H)
    assert r.status_code == 409


async def join(host, name: str, send=None, **hello) -> int:
    """A lender's agent connects and says hello (only then is it given tasks). Returns the link id."""
    async def drop(m):
        pass
    mgr = host.mgr
    if name not in {i["name"] for i in host.tokens.list()}:
        host.tokens.issue(name)
    link = mgr.attach(name, send or drop)
    await mgr.on_agent_message(link, {"type": "hello", "agent_version": "0.2.0", "codex_version": "x",
                                      "auth_mode": "chatgpt", **hello})
    return link


async def test_send_followup_and_cancel_from_admin(admin, host):
    sent = []

    async def send(m):
        sent.append(m)
    link = await join(host, "friend", send)
    r = await admin.post("/admin/api/tasks", json={"prompt": "hello"}, headers=H)
    assert r.status_code == 200
    tid = r.json()["task_id"]
    assert sent[-1]["type"] == "task_start" and sent[-1]["prompt"] == "hello"
    assert (await admin.post(f"/admin/api/tasks/{tid}/followup", json={"text": "more"}, headers=H)).status_code == 200
    assert sent[-1] == {"type": "task_followup", "task_id": tid, "text": "more"}
    assert (await admin.post(f"/admin/api/tasks/{tid}/cancel", headers=H)).status_code == 200
    assert sent[-1]["type"] == "task_cancel"
    await host.mgr.on_agent_message(link, {"type": "task_state", "seq": 5, "task_id": tid, "state": "completed",
                                           "thread_id": "th", "report": "ok"})
    r = await admin.post("/admin/api/tasks", json={"prompt": "next", "continue_task_id": tid}, headers=H)
    assert r.status_code == 200 and sent[-1]["continue_thread_id"] == "th"


@pytest.mark.parametrize("spec, model, effort", [({"model": "m1"}, "m1", None),
                                                ({"effort": "high"}, None, "high"),
                                                ({"model": "m1", "effort": "high"}, "m1", "high"),
                                                ({"model": "", "effort": ""}, None, None)])
async def test_admin_send_task_with_model(admin, host, spec, model, effort):
    sent = []

    async def send(msg):
        sent.append(msg)
    await join(host, "friend", send, protocol=3, capabilities={"model_select": True},
               models=[{"model": "m1", "efforts": ["low", "high"], "is_default": True}], max_effort="xhigh")
    r = await admin.post("/admin/api/tasks", json={"prompt": "x", **spec}, headers=H)
    assert r.status_code == 200, r.text
    rec = host.mgr.tasks[r.json()["task_id"]]
    assert rec.origin == "admin" and rec.model == model and rec.effort == effort
    assert r.json() == {"task_id": rec.task_id, "lender": "friend", "model": model, "effort": effort}
    for key, value in (("model", model), ("effort", effort)):
        if value is None:
            assert key not in sent[-1]
        else:
            assert sent[-1][key] == value


@pytest.mark.parametrize("field", ["model", "effort"])
@pytest.mark.parametrize("value", [3, None, False, [], {}])
async def test_admin_send_task_bad_model_type(admin, host, field, value):
    r = await admin.post("/admin/api/tasks", json={"prompt": "x", field: value}, headers=H)
    assert r.status_code == 400
    assert r.json()["detail"] == "モデルと推論の強さは名前で指定してください"
    assert host.mgr.tasks == {}


async def test_task_detail_has_what_the_page_shows(admin, host):
    # heading of the detail: who did it, when it started and ended (design 5); the record: plan and the rest
    link = await join(host, "alice")
    before = time.time()
    tid = (await admin.post("/admin/api/tasks", json={"prompt": "hello"}, headers=H)).json()["task_id"]
    await host.mgr.on_agent_message(link, {"type": "event", "seq": 1, "task_id": tid, "kind": "plan",
                                           "data": {"steps": [{"step": "read", "status": "completed"}]}})
    t = (await admin.get(f"/admin/api/tasks/{tid}")).json()
    assert t["lender"] == "alice" and before <= t["created"] <= time.time() and t["finished"] is None
    assert t["events"][-1]["kind"] == "plan" and t["events"][-1]["data"]["steps"][0]["step"] == "read"
    await host.mgr.on_agent_message(link, {"type": "task_state", "seq": 2, "task_id": tid, "state": "completed",
                                           "thread_id": "th", "report": "ok"})
    t = (await admin.get(f"/admin/api/tasks/{tid}")).json()
    assert t["created"] <= t["finished"] <= time.time()
    ended = t["finished"]
    await host.mgr.on_agent_message(link, {"type": "task_state", "seq": 3, "task_id": tid, "state": "completed",
                                           "thread_id": "th", "report": "ok again"})
    assert (await admin.get(f"/admin/api/tasks/{tid}")).json()["finished"] == ended  # the first end counts
    from codex_bridge.host.tasks import TaskManager
    again = TaskManager(host.config.data_dir, touched_fn=lambda t: [])  # kept across a host restart
    assert again.result(tid)["finished"] == ended and again.result(tid)["created"] == t["created"]


async def test_task_detail_carries_the_half_written_message(admin, host):
    link = await join(host, "alice")
    tid = (await admin.post("/admin/api/tasks", json={"prompt": "hello"}, headers=H)).json()["task_id"]
    assert (await admin.get(f"/admin/api/tasks/{tid}")).json()["progress"] is None
    await host.mgr.on_agent_message(link, {"type": "event", "task_id": tid, "kind": "progress",
                                           "data": {"text": "要点 3: 在庫は"}})
    t = (await admin.get(f"/admin/api/tasks/{tid}")).json()
    assert t["progress"] == "要点 3: 在庫は" and t["events"] == []


async def test_task_apis_answer_whatever_a_lender_sends_about_a_task(admin, host):
    # review I1: a NaN in a state or an event must not turn the task list or a task's detail into an error
    link = await join(host, "alice")
    tid = (await admin.post("/admin/api/tasks", json={"prompt": "hello"}, headers=H)).json()["task_id"]
    nan = float("nan")
    for text in (
        '{"type": "event", "seq": 1, "task_id": "%s", "kind": "status", "data": {"text": NaN, "n": 1e999}}',
        '{"type": "event", "seq": 2, "task_id": "%s", "kind": NaN, "data": {}}',
        '{"type": "task_state", "seq": 3, "task_id": "%s", "state": NaN}',
        '{"type": "task_state", "seq": 4, "task_id": "%s", "state": 1e999, "report": "x"}',
        '{"type": "task_state", "seq": 5, "task_id": "%s", "state": "running", "thread_id": NaN, "report": -Infinity, "resume_at": 1e999}',
    ):
        await host.mgr.on_agent_message(link, protocol.decode(text % tid))
    # the same handed over as numbers (not through the wire decoder)
    await host.mgr.on_agent_message(link, {"type": "event", "seq": 6, "task_id": tid, "kind": "status", "data": {"text": nan}})
    await host.mgr.on_agent_message(link, {"type": "task_state", "seq": 7, "task_id": tid, "state": nan, "report": nan})
    r = await admin.get("/admin/api/tasks")
    assert r.status_code == 200 and [(t["task_id"], t["state"]) for t in r.json()] == [(tid, "running")]
    r = await admin.get(f"/admin/api/tasks/{tid}")
    assert r.status_code == 200
    t = r.json()
    assert (t["state"], t["thread_id"], t["report"], t["resume_at"]) == ("running", None, None, None)
    assert [e["data"] for e in t["events"]] == [{"text": None, "n": None}, {"text": None}]
    for path in ("/admin/api/lenders", "/admin/api/overview", "/admin/api/usage"):
        assert (await admin.get(path)).status_code == 200, path


async def test_task_apis_work_again_after_a_record_was_saved_with_nan(tmp_path):
    # what a host before this check wrote to disk: the list must not stay at an error after the update
    tasks = tmp_path / "data" / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "t1.json").write_text('{"task_id": "t1", "prompt": "p", "created": 5.0, "state": NaN, "report": NaN, '
                                   '"thread_id": "th", "resume_at": NaN, "lender": "alice"}', encoding="utf-8")
    (tasks / "t1.jsonl").write_text('{"ts": 1.0, "kind": "status", "data": {"text": NaN}}\n', encoding="utf-8")
    (tasks / "t2.json").write_text('{"task_id": "t2", "prompt": "q", "created": 6.0, "state": "completed", "report": "ok"}',
                                   encoding="utf-8")
    async with admin_client(make_host(tmp_path)) as admin:
        r = await admin.get("/admin/api/tasks")
        assert r.status_code == 200 and [(t["task_id"], t["state"]) for t in r.json()] == [("t2", "completed"), ("t1", "failed")]
        r = await admin.get("/admin/api/tasks/t1")
        assert r.status_code == 200 and r.json()["events"] == [{"ts": 1.0, "kind": "status", "data": {"text": None}}]
        assert (await admin.get("/admin/api/tasks/t2")).json()["report"] == "ok"
        assert (await admin.get("/admin/api/lenders")).status_code == 200


def window(percent: int) -> dict:
    return {"used_percent": percent, "resets_at": int(time.time()) + 3600}


async def two_lenders(host) -> dict:
    """alice (little room left) and bob (lots of room, disposable sessions); returns what each was sent."""
    mgr = host.mgr
    sent = {"alice": [], "bob": []}
    for name, used, resumable in (("alice", 60, True), ("bob", 10, False)):
        async def send(m, name=name):
            sent[name].append(m)
        link = await join(host, name, send, protocol=2, capabilities={"resumable": resumable, "pre_approval": False},
                          limits={"five_hour": 70, "weekly": None, "force": True})
        await mgr.on_agent_message(link, {"type": "usage", "five_hour": window(used), "weekly": None, "ts": 0})
    return sent


async def test_lenders_api_lists_each_lender(admin, host):
    assert (await admin.get("/admin/api/lenders")).json() == []
    await two_lenders(host)
    tid = (await admin.post("/admin/api/tasks", json={"prompt": "x", "lender": "alice"}, headers=H)).json()["task_id"]
    rows = (await admin.get("/admin/api/lenders")).json()
    assert [r["name"] for r in rows] == ["alice", "bob"]
    a, b = rows
    # everything the table of lenders shows (design 3.2)
    assert a["connected"] is True and a["ready"] is True and a["current_task_id"] == tid
    assert a["usage"]["five_hour"]["used_percent"] == 60 and a["usage"]["weekly"] is None
    assert a["usage"]["thresholds"] == {"five_hour": 90, "weekly": 95} and a["usage"]["blocked_until"] is None
    assert a["limits"] == {"five_hour": 70, "weekly": None, "force": True}
    assert a["capabilities"] == {"resumable": True, "pre_approval": False, "model_select": False}
    assert (a["agent_version"], a["codex_version"], a["auth_mode"]) == ("0.2.0", "x", "chatgpt")
    assert b["current_task_id"] is None and b["capabilities"]["resumable"] is False
    assert (await admin.get("/admin/api/lenders", headers={"Host": "evil.example:8770"})).status_code == 403


async def test_lenders_api_lists_invited_names_that_have_not_connected(admin, host):
    for name in ("alice", "carol"):
        await admin.post("/admin/api/invites", json={"name": name}, headers=H)
    await two_lenders(host)  # alice and bob connect; carol has only been invited
    rows = (await admin.get("/admin/api/lenders")).json()
    assert [r["name"] for r in rows] == ["alice", "bob", "carol"]
    carol = rows[2]
    assert carol["connected"] is False and carol["usage"]["five_hour"] is None and carol["current_task_id"] is None
    assert carol["capabilities"] is None and set(carol) == set(rows[0])
    await admin.delete("/admin/api/invites/carol", headers=H)
    assert [r["name"] for r in (await admin.get("/admin/api/lenders")).json()] == ["alice", "bob"]


async def test_lenders_api_survives_numbers_that_are_not_json(admin, host):
    nan, inf = float("nan"), float("inf")
    link = await join(host, "alice", agent_version=nan, protocol=inf, capabilities={"resumable": nan, "x": inf},
                      limits={"five_hour": nan, "weekly": inf, "force": nan})
    await host.mgr.on_agent_message(link, {
        "type": "usage", "ts": nan, "friend_limits": {"five_hour": inf},
        "five_hour": {"used_percent": 10, "resets_at": inf, "extra": nan},
        "weekly": {"used_percent": 20, "resets_at": int(time.time()) + 60, "more": [nan]}})
    r = await admin.get("/admin/api/lenders")
    assert r.status_code == 200
    row = r.json()[0]
    assert row["usage"]["ts"] is None and row["usage"]["five_hour"]["used_percent"] == 10
    assert set(row["usage"]["weekly"]) == {"used_percent", "resets_at", "resets_at_local"}
    assert (await admin.get("/admin/api/overview")).status_code == 200
    assert (await admin.get("/admin/api/usage")).status_code == 200


async def test_send_task_to_a_chosen_lender(admin, host):
    sent = await two_lenders(host)
    r = await admin.post("/admin/api/tasks", json={"prompt": "one", "lender": "alice"}, headers=H)  # bob has more room
    assert r.status_code == 200 and r.json()["lender"] == "alice"
    assert sent["alice"][-1]["type"] == "task_start" and sent["alice"][-1]["task_id"] == r.json()["task_id"]
    assert all(m["type"] != "task_start" for m in sent["bob"])
    r = await admin.post("/admin/api/tasks", json={"prompt": "two", "lender": "alice"}, headers=H)
    assert r.status_code == 409 and "作業中" in r.json()["detail"]
    r = await admin.post("/admin/api/tasks", json={"prompt": "two", "lender": "carol"}, headers=H)
    assert r.status_code == 409 and "接続されていません" in r.json()["detail"]
    assert (await admin.post("/admin/api/tasks", json={"prompt": "two", "lender": 5}, headers=H)).status_code == 400
    assert len(host.mgr.tasks) == 1


@pytest.mark.parametrize("lender", [0, False, [], {}, 0.0, True, ["bob"]])
async def test_send_task_with_a_lender_that_is_not_a_name_is_refused(admin, host, lender):
    # "empty" values of another type must not be read as "automatic": only a missing, null or "" lender is
    sent = await two_lenders(host)
    r = await admin.post("/admin/api/tasks", json={"prompt": "one", "lender": lender}, headers=H)
    assert r.status_code == 400 and "送り先" in r.json()["detail"]
    assert host.mgr.tasks == {} and all(m["type"] != "task_start" for m in sent["alice"] + sent["bob"])


@pytest.mark.parametrize("body", [{}, {"lender": None}, {"lender": ""}])
async def test_send_task_without_a_lender_is_automatic(admin, host, body):
    sent = await two_lenders(host)
    r = await admin.post("/admin/api/tasks", json={"prompt": "one", **body}, headers=H)
    assert r.status_code == 200 and r.json()["lender"] == "bob"  # the one with the most room
    assert sent["bob"][-1]["task_id"] == r.json()["task_id"]


async def test_undo_refused_for_every_task_in_progress(admin, host):
    await join(host, "alice")
    await join(host, "bob")
    tids = [(await admin.post("/admin/api/tasks", json={"prompt": p}, headers=H)).json()["task_id"]
            for p in ("one", "two")]
    assert {host.mgr.tasks[t].lender for t in tids} == {"alice", "bob"}
    for tid in tids:
        assert (await admin.post(f"/admin/api/tasks/{tid}/undo", headers=H)).status_code == 409


async def test_send_task_errors_are_readable(admin, host):
    r = await admin.post("/admin/api/tasks", json={"prompt": "x"}, headers=H)
    assert r.status_code == 409 and "接続されていません" in r.json()["detail"]
    assert (await admin.post("/admin/api/tasks", json={"prompt": "  "}, headers=H)).status_code == 400
