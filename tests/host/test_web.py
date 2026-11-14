import time

import httpx
import pytest
import yaml

from codex_bridge.common.invite import parse_invite
from codex_bridge.host.app import build_host, create_local_app, create_public_app
from codex_bridge.host.config import load_host_config
from codex_bridge.host.main import ensure_config

H = {"X-Codex-Bridge": "1"}
VALID_OFF = "default: read\nrules: []\nexec: {mode: \"off\"}\n"


@pytest.fixture
def host(tmp_path):
    share = tmp_path / "share"
    (share / "raw").mkdir(parents=True)
    cfg = tmp_path / "host.yaml"
    cfg.write_text(yaml.safe_dump({"share_root": str(share), "data_dir": str(tmp_path / "data"),
                                   "public_bind": "100.64.0.1:8765", "local_port": 8770,
                                   "rg_path": str(tmp_path / "rg.exe")}), encoding="utf-8")
    ensure_config(cfg)
    return build_host(load_host_config(cfg), config_path=cfg)


@pytest.fixture
async def admin(host):
    t = httpx.ASGITransport(app=create_local_app(host), client=("127.0.0.1", 5000))
    async with httpx.AsyncClient(transport=t, base_url="http://127.0.0.1:8770") as c:
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


async def join(mgr, name: str, send=None, **hello) -> int:
    """A lender's agent connects and says hello (only then is it given tasks). Returns the link id."""
    async def drop(m):
        pass
    link = mgr.attach(name, send or drop)
    await mgr.on_agent_message(link, {"type": "hello", "agent_version": "0.2.0", "codex_version": "x",
                                      "auth_mode": "chatgpt", **hello})
    return link


async def test_send_followup_and_cancel_from_admin(admin, host):
    sent = []

    async def send(m):
        sent.append(m)
    link = await join(host.mgr, "friend", send)
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


def window(percent: int) -> dict:
    return {"used_percent": percent, "resets_at": int(time.time()) + 3600}


async def two_lenders(mgr) -> dict:
    """alice (little room left) and bob (lots of room, disposable sessions); returns what each was sent."""
    sent = {"alice": [], "bob": []}
    for name, used, resumable in (("alice", 60, True), ("bob", 10, False)):
        async def send(m, name=name):
            sent[name].append(m)
        link = await join(mgr, name, send, protocol=2, capabilities={"resumable": resumable, "pre_approval": False},
                          limits={"five_hour": 70, "weekly": None, "force": True})
        await mgr.on_agent_message(link, {"type": "usage", "five_hour": window(used), "weekly": None, "ts": 0})
    return sent


async def test_lenders_api_lists_each_lender(admin, host):
    assert (await admin.get("/admin/api/lenders")).json() == []
    await two_lenders(host.mgr)
    tid = (await admin.post("/admin/api/tasks", json={"prompt": "x", "lender": "alice"}, headers=H)).json()["task_id"]
    rows = (await admin.get("/admin/api/lenders")).json()
    assert [r["name"] for r in rows] == ["alice", "bob"]
    a, b = rows
    # everything the table of lenders shows (design 3.2)
    assert a["connected"] is True and a["ready"] is True and a["current_task_id"] == tid
    assert a["usage"]["five_hour"]["used_percent"] == 60 and a["usage"]["weekly"] is None
    assert a["usage"]["thresholds"] == {"five_hour": 90, "weekly": 95} and a["usage"]["blocked_until"] is None
    assert a["limits"] == {"five_hour": 70, "weekly": None, "force": True}
    assert a["capabilities"] == {"resumable": True, "pre_approval": False}
    assert (a["agent_version"], a["codex_version"], a["auth_mode"]) == ("0.2.0", "x", "chatgpt")
    assert b["current_task_id"] is None and b["capabilities"]["resumable"] is False
    assert (await admin.get("/admin/api/lenders", headers={"Host": "evil.example:8770"})).status_code == 403


async def test_lenders_api_lists_invited_names_that_have_not_connected(admin, host):
    for name in ("alice", "carol"):
        await admin.post("/admin/api/invites", json={"name": name}, headers=H)
    await two_lenders(host.mgr)  # alice and bob connect; carol has only been invited
    rows = (await admin.get("/admin/api/lenders")).json()
    assert [r["name"] for r in rows] == ["alice", "bob", "carol"]
    carol = rows[2]
    assert carol["connected"] is False and carol["usage"]["five_hour"] is None and carol["current_task_id"] is None
    assert carol["capabilities"] is None and set(carol) == set(rows[0])
    await admin.delete("/admin/api/invites/carol", headers=H)
    assert [r["name"] for r in (await admin.get("/admin/api/lenders")).json()] == ["alice", "bob"]


async def test_lenders_api_survives_numbers_that_are_not_json(admin, host):
    nan, inf = float("nan"), float("inf")
    link = await join(host.mgr, "alice", agent_version=nan, protocol=inf, capabilities={"resumable": nan, "x": inf},
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
    sent = await two_lenders(host.mgr)
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
    sent = await two_lenders(host.mgr)
    r = await admin.post("/admin/api/tasks", json={"prompt": "one", "lender": lender}, headers=H)
    assert r.status_code == 400 and "送り先" in r.json()["detail"]
    assert host.mgr.tasks == {} and all(m["type"] != "task_start" for m in sent["alice"] + sent["bob"])


@pytest.mark.parametrize("body", [{}, {"lender": None}, {"lender": ""}])
async def test_send_task_without_a_lender_is_automatic(admin, host, body):
    sent = await two_lenders(host.mgr)
    r = await admin.post("/admin/api/tasks", json={"prompt": "one", **body}, headers=H)
    assert r.status_code == 200 and r.json()["lender"] == "bob"  # the one with the most room
    assert sent["bob"][-1]["task_id"] == r.json()["task_id"]


async def test_undo_refused_for_every_task_in_progress(admin, host):
    await join(host.mgr, "alice")
    await join(host.mgr, "bob")
    tids = [(await admin.post("/admin/api/tasks", json={"prompt": p}, headers=H)).json()["task_id"]
            for p in ("one", "two")]
    assert {host.mgr.tasks[t].lender for t in tids} == {"alice", "bob"}
    for tid in tids:
        assert (await admin.post(f"/admin/api/tasks/{tid}/undo", headers=H)).status_code == 409


async def test_send_task_errors_are_readable(admin, host):
    r = await admin.post("/admin/api/tasks", json={"prompt": "x"}, headers=H)
    assert r.status_code == 409 and "接続されていません" in r.json()["detail"]
    assert (await admin.post("/admin/api/tasks", json={"prompt": "  "}, headers=H)).status_code == 400
